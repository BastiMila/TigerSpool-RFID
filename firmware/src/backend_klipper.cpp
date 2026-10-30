#include "backend_klipper.h"
#include <WebSocketsClient.h>
#include <ArduinoJson.h>

namespace {
    const char* SLOTS[4] = { "T0", "T1", "T2", "T3" };

    // AFC reports colours as "#RRGGBB".
    bool hexRGB(const char* s, uint8_t& r, uint8_t& g, uint8_t& b) {
        if (!s) return false;
        if (*s == '#') s++;
        if (strlen(s) < 6) return false;
        auto hx = [](char c) -> int {
            if (c >= '0' && c <= '9') return c - '0';
            c |= 0x20; return (c >= 'a' && c <= 'f') ? c - 'a' + 10 : 0;
        };
        r = hx(s[0]) * 16 + hx(s[1]);
        g = hx(s[2]) * 16 + hx(s[3]);
        b = hx(s[4]) * 16 + hx(s[5]);
        return true;
    }

    // G-code arguments are separated by spaces and a quote would break the
    // value stored in save_variables, so strip both out of vendor and type.
    String clean(const String& in) {
        String o; o.reserve(in.length());
        for (size_t i = 0; i < in.length(); i++) {
            char c = in[i];
            if (c == ' ') o += '_';
            else if (c == '\'' || c == '"' || c == '#' || c == ';') continue;
            else o += c;
        }
        return o;
    }

    String unclean(const char* s) {
        String o(s ? s : "");
        o.replace('_', ' ');
        return o;
    }
}

// Rebuild what the UI shows for one slot from the pieces we keep.
void KlipperBackend::recompute(int i) {
    SlotState& s = slots_[i];
    s.type  = sub_[i].length() ? (mat_[i] + " " + sub_[i]) : mat_[i];
    s.known = mat_[i].length() > 0;
    float pct = (initW_[i] > 0) ? (weight_[i] * 100.0f / initW_[i]) : 0.0f;
    if (pct < 0) pct = 0;
    if (pct > 100) pct = 100;
    s.percent  = (uint8_t)(pct + 0.5f);
    s.selected = (i == currentLane_);
}

void KlipperBackend::applyLane(int i, JsonObjectConst lane) {
    SlotState& s = slots_[i];
    // Pushes carry only what changed, so every field is read on its own.
    if (lane["material"].is<const char*>()) mat_[i] = (const char*)lane["material"];
    if (lane["color"].is<const char*>()) {
        uint8_t r = s.r, g = s.g, b = s.b;
        if (hexRGB((const char*)lane["color"], r, g, b)) { s.r = r; s.g = g; s.b = b; }
    }
    if (lane["weight"].is<float>())         weight_[i] = lane["weight"].as<float>();
    if (lane["initial_weight"].is<float>()) initW_[i]  = lane["initial_weight"].as<float>();
    if (s.brand.length() == 0 && lane["spool_vendor"].is<const char*>())
        s.brand = (const char*)lane["spool_vendor"];

    if (pendIdx_ == i) {
        if (s.r == pendR_ && s.g == pendG_ && s.b == pendB_) {
            status_ = String("Klipper: ") + SLOTS[i] + " confirmed";
            pendIdx_ = -1;
        }
    }
}

void KlipperBackend::applyStatus(JsonObjectConst st) {
    JsonObjectConst afc = st["AFC"];
    if (!afc.isNull() && afc.containsKey("current_load")) {
        currentLane_ = -1;
        if (afc["current_load"].is<const char*>()) {
            const char* cl = (const char*)afc["current_load"];
            size_t n = strlen(cl);
            if (n > 0 && cl[n - 1] >= '0' && cl[n - 1] <= '3') currentLane_ = cl[n - 1] - '0';
        }
    }

    const char* ps = st["print_stats"]["state"] | "";
    if (*ps) printing_ = !strcmp(ps, "printing") || !strcmp(ps, "paused");

    JsonObjectConst vars = st["save_variables"]["variables"];
    for (int i = 0; i < 4; i++) {
        if (!vars.isNull()) {
            String kb = String("tt_t") + i + "_brand";
            String ks = String("tt_t") + i + "_sub";
            if (vars[kb].is<const char*>()) slots_[i].brand = unclean((const char*)vars[kb]);
            if (vars[ks].is<const char*>()) sub_[i]         = unclean((const char*)vars[ks]);
        }
        String key = String("AFC_lane extruder") + i;
        JsonObjectConst lane = st[key];
        if (!lane.isNull()) applyLane(i, lane);
        recompute(i);
    }
    if (pendIdx_ < 0) status_ = "Klipper: slots updated";
}

void KlipperBackend::onMsg(uint8_t* payload, size_t len) {
    JsonDocument d;
    if (deserializeJson(d, payload, len)) return;

    // A refused command comes back as an error object. Saying so is the only
    // way to tell "sent" from "done".
    if (!d["error"].isNull()) {
        const char* m = d["error"]["message"] | "error";
        status_ = String("Klipper: ") + m;
        Serial.printf("[klipper] error: %s\n", m);
        pendIdx_ = -1;
        return;
    }

    JsonObjectConst st = d["result"]["status"];
    if (st.isNull() && String(d["method"] | "") == "notify_status_update")
        st = d["params"][0];
    if (st.isNull()) return;
    applyStatus(st);
}

void KlipperBackend::onEvent(WStype_t type, uint8_t* payload, size_t len) {
    switch (type) {
        case WStype_CONNECTED:
            connected_ = true;  status_ = "Klipper: connected";
            Serial.println("[klipper] websocket connected");
            break;
        case WStype_DISCONNECTED:
            if (connected_) Serial.println("[klipper] websocket closed");
            connected_ = false; status_ = "Klipper: disconnected";
            break;
        case WStype_TEXT: onMsg(payload, len); break;
        case WStype_ERROR:
            Serial.printf("[klipper] websocket error: %.*s\n", (int)len,
                          payload ? (const char*)payload : "");
            break;
        default: break;
    }
}

bool KlipperBackend::sendRaw(String s) {
    if (!connected_) return false;
    Serial.printf("[klipper] -> %s\n", s.c_str());
    return ws_.sendTXT(s);
}

void KlipperBackend::begin(const PrinterCfg& cfg) {
    for (int i = 0; i < 4; i++) {
        slots_[i] = SlotState{};
        mat_[i] = ""; sub_[i] = "";
        weight_[i] = 0; initW_[i] = 0;
    }
    connected_ = false;
    currentLane_ = -1;
    printing_ = false;
    pendIdx_ = -1;
    status_ = "Klipper: connecting...";
    // Same as the Snapmaker backend: no Origin header, no subprotocol.
    // Moonraker answers 403 to the library's default "Origin: file://" and the
    // client then fires no event at all.
    ws_.setExtraHeaders("");
    ws_.begin(cfg.host, 7125, "/websocket", "");
    ws_.onEvent([this](WStype_t t, uint8_t* p, size_t l) { onEvent(t, p, l); });
    ws_.setReconnectInterval(10000);
    ws_.enableHeartbeat(15000, 3000, 2);
}

void KlipperBackend::loop() {
    ws_.loop();
    if (connected_ && millis() - lastReq_ > 5000) { lastReq_ = millis(); refresh(); }
    if (pendIdx_ >= 0 && (int32_t)(millis() - pendUntil_) > 0) {
        status_ = String("Klipper: ") + SLOTS[pendIdx_] + " sent, not confirmed";
        pendIdx_ = -1;
    }
}

void KlipperBackend::stop() {
    ws_.disconnect();
    connected_ = false;
    status_ = "Klipper: stopped";
}

bool KlipperBackend::connected() { return connected_; }
String KlipperBackend::status()  { return status_; }
const char* KlipperBackend::slotLabel(int i) { return SLOTS[(i >= 0 && i < 4) ? i : 0]; }
const SlotState& KlipperBackend::slot(int i) { return slots_[(i >= 0 && i < 4) ? i : 0]; }

void KlipperBackend::refresh() {
    JsonDocument d;
    d["jsonrpc"] = "2.0";
    d["method"]  = "printer.objects.query";
    JsonObject o = d["params"]["objects"].to<JsonObject>();
    for (int i = 0; i < 4; i++) {
        String key = String("AFC_lane extruder") + i;   // String: the key is copied
        o[key] = nullptr;
    }
    o["AFC"] = nullptr;
    o["save_variables"] = nullptr;
    o["print_stats"] = nullptr;
    d["id"] = 1001;
    String out; serializeJson(d, out);
    sendRaw(out);
}

bool KlipperBackend::assign(int idx, const TagInfo& t) {
    if (idx < 0 || idx >= 4 || !connected_) return false;
    // AFC keeps lane data per tool. Rewriting a lane while the printer is
    // working is not something this code has been checked against.
    if (printing_) { status_ = "Klipper: printer is printing"; return false; }

    // A tag carries one string, "PLA High Speed". AFC gets the family only,
    // the variant goes to save_variables as the subtype.
    String full = t.material.length() ? t.material : String("PLA");
    int sp = full.indexOf(' ');
    String mat = clean(sp > 0 ? full.substring(0, sp) : full);
    String sub = clean(sp > 0 ? full.substring(sp + 1) : String());
    String vend = clean(t.brand);

    char col[8];
    snprintf(col, sizeof(col), "%02X%02X%02X", t.r, t.g, t.b);

    String script = String("TT_SET_LANE TOOL=") + idx + " MATERIAL=" + mat + " COLOR=" + col;
    if (sub.length())  script += " SUBTYPE=" + sub;
    if (vend.length()) script += " BRAND=" + vend;

    JsonDocument d;
    d["jsonrpc"] = "2.0";
    d["method"]  = "printer.gcode.script";
    d["params"]["script"] = script;
    d["id"] = 201;
    String out; serializeJson(d, out);

    bool ok = sendRaw(out);
    if (!ok) { status_ = "Klipper: send failed"; return false; }

    pendIdx_ = idx; pendR_ = t.r; pendG_ = t.g; pendB_ = t.b;
    pendUntil_ = millis() + 12000;
    status_ = String("Klipper: sent -> ") + SLOTS[idx];
    delay(200);
    refresh();   // read it back; applyLane() confirms or the timeout reports it
    return true;
}
