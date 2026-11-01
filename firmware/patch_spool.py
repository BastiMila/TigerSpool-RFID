# patch_spool.py - the spool's weight from the account, for the Klipper backend.
# Run in the firmware folder:  python patch_spool.py
# Edits src/tigertag_cloud.h, src/tigertag_cloud.cpp and src/main.cpp. Every
# anchor must be found exactly once, otherwise nothing is written at all.
import sys

HDR, CPP, MAIN = "src/tigertag_cloud.h", "src/tigertag_cloud.cpp", "src/main.cpp"

def load(p):
    raw = open(p, "rb").read().decode("utf-8")
    return raw, ("\r\n" if "\r\n" in raw else "\n")

def die(msg):
    sys.exit("ABORT: " + msg + " Nothing was changed.")

def replace_once(text, old, new, what):
    n = text.count(old)
    if n != 1:
        die("'%s' found %d times (expected 1)." % (what, n))
    return text.replace(old, new)

def insert_after(text, nl, anchor_stripped, new_lines, what, indent_from=0):
    lines = text.split(nl)
    hits = [i for i, l in enumerate(lines) if l.strip() == anchor_stripped]
    if len(hits) != 1:
        die("'%s' found %d times (expected 1)." % (what, len(hits)))
    i = hits[0]
    ref = lines[i + indent_from]
    ind = ref[:len(ref) - len(ref.lstrip())]
    lines[i + 1:i + 1] = [(ind + l) if l else l for l in new_lines]
    return nl.join(lines)

HEADER_BLOCK = """
// The spool's own document in the account, users/{uid}/inventory/{chip uid}.
//
// A chip is written once. The weight a TigerScale measures afterwards goes to
// that document, not back onto the chip, so a slot that should show what is left
// on the spool has to ask the account. Same shape as product_api: the fetch
// starts the moment the tag is read (the slot is chosen before), on a task of
// its own; a send waits for it only inside SPOOL_BUDGET_MS, and an answer that
// arrives later is kept for the next send rather than sent as a second one.
//
// The Klipper backend takes the weight from here and from nowhere else, so a
// missed answer means no weight is written. 8 s is three times the 2.6 s one
// Firestore read took on this device, TLS handshake included (the account
// import logs it).
struct SpoolStock {
    int netGrams   = -1;   // weight_available, filament only; -1 = not in the document
    int totalGrams = -1;   // measure_gr, the spool's full quantity; -1 = not in the document
};
constexpr uint32_t SPOOL_BUDGET_MS = 8000;

// Starts the read for this chip. False when nothing was started: no account, a
// read already running, a fresh answer already held, the sync or the presence
// beat running (they share the token), or no room for a handshake.
bool requestSpool(const String& chipUid);
// A read for THIS chip is running and still inside its budget - the one
// condition under which a send should wait.
bool spoolWaiting(const String& chipUid);
// The last answer, if it was about this chip and is not stale. A document that
// does not exist answers with both figures at -1.
bool spoolCached(const String& chipUid, SpoolStock& out);
"""

CPP_BLOCK = """

// ---- the spool's stock, from the account -----------------------------------
//
// See SpoolStock in tigertag_cloud.h. The session token is shared with the
// account sync and the presence beat, so the three never overlap: this task does
// not start while either runs, and neither starts while this one does.
namespace {
    // One answer is kept, for one chip. The requester sets the uid before the
    // task starts and does not touch it while g_spoolBusy is set, so the task and
    // the loop never write the same String at once.
    String              g_stockUid;
    ttcloud::SpoolStock g_stock;
    uint32_t            g_stockStart = 0;    // when the running read started
    uint32_t            g_stockAt    = 0;    // when the held answer arrived
    volatile bool       g_stockDone  = false;
    // A spool can be weighed again. An answer older than this is a wrong one.
    const uint32_t      STOCK_TTL_MS = 30UL * 1000;

    String stockKey(const String& chipUid) {
        String k = chipUid; k.trim(); k.toUpperCase();   // the document id is uppercase hex
        return k;
    }

    void stockTaskFn(void*) {
        ttcloud::SpoolStock r;
        const String uid = g_stockUid;
        const uint32_t t0 = millis();
        int code = -3;
        if (ensureToken()) {
            // Only the fields this reads: the document carries links, colours and
            // a rack position that nothing here has any use for.
            const String url = String("https://firestore.googleapis.com/v1/projects/") + PROJECT +
                               "/databases/(default)/documents/users/" + g_uid + "/inventory/" + uid +
                               "?mask.fieldPaths=weight_available&mask.fieldPaths=measure_gr"
                               "&mask.fieldPaths=measure&mask.fieldPaths=deleted";
            String resp;
            code = httpsGET(url, resp, g_idToken.c_str());
            if (code == 200) {
                JsonDocument d;
                if (!deserializeJson(d, resp)) {
                    JsonObjectConst f = d["fields"];
                    if (fsBool(f, "deleted") != 1) {
                        const String w = fsStr(f, "weight_available");
                        String m = fsStr(f, "measure_gr");
                        if (!m.length()) m = fsStr(f, "measure");
                        if (w.length()) r.netGrams   = (int)w.toFloat();
                        if (m.length()) r.totalGrams = (int)m.toFloat();
                    }
                }
            }
        }
        Serial.printf("[spool] inventory/%s -> http=%d net=%d total=%d in %lu ms\\n",
                      uid.c_str(), code, r.netGrams, r.totalGrams, (unsigned long)(millis() - t0));
        g_stock = r;
        g_stockAt = millis();
        g_stockDone = true;
        g_spoolBusy = false;       // last, so a reader that sees "not busy" sees the answer
        vTaskDelete(nullptr);
    }
}  // namespace

bool ttcloud::requestSpool(const String& chipUid) {
    const String uid = stockKey(chipUid);
    if (uid.isEmpty() || g_uid.isEmpty() || g_spoolBusy) return false;
    if (g_stockDone && uid == g_stockUid && millis() - g_stockAt < STOCK_TTL_MS) return false;
    if (g_asyncBusy || g_hbBusy) return false;
    // A handshake needs one large block, the same figure the sync asks for.
    if (ESP.getMaxAllocHeap() < SYNC_STACK + 2048) {
        Serial.printf("[spool] no room for the read: largest block %u\\n",
                      (unsigned)ESP.getMaxAllocHeap());
        return false;
    }
    g_stockUid = uid;
    g_stockDone = false;
    g_stockStart = millis();
    g_spoolBusy = true;
    if (xTaskCreatePinnedToCore(stockTaskFn, "ttStock", SYNC_STACK, nullptr, 1, nullptr, 0) != pdPASS) {
        g_spoolBusy = false;
        return false;
    }
    return true;
}

bool ttcloud::spoolWaiting(const String& chipUid) {
    if (!g_spoolBusy) return false;
    return stockKey(chipUid) == g_stockUid && millis() - g_stockStart < SPOOL_BUDGET_MS;
}

bool ttcloud::spoolCached(const String& chipUid, SpoolStock& out) {
    if (!g_stockDone || g_spoolBusy) return false;
    if (stockKey(chipUid) != g_stockUid || millis() - g_stockAt >= STOCK_TTL_MS) return false;
    out = g_stock;
    return true;
}
"""

# ---- read everything first
h, hnl = load(HDR)
c, cnl = load(CPP)
m, mnl = load(MAIN)

# ---- header
h = insert_after(h, hnl, "bool takeHeartbeat(bool& ok, bool& full, String& err);",
                 [""] + HEADER_BLOCK.strip("\n").split("\n"), "takeHeartbeat declaration (header)")

# ---- cpp
c = insert_after(c, cnl, "static volatile bool g_asyncBusy = false;",
                 ["// A read of the spool's stock from the account; see requestSpool() at the foot of the file.",
                  "static volatile bool g_spoolBusy = false;"],
                 "g_asyncBusy declaration")
c = replace_once(c, "if (g_asyncBusy || ttcloud::heartbeatBusy()) return false;",
                    "if (g_asyncBusy || g_spoolBusy || ttcloud::heartbeatBusy()) return false;",
                 "startAsyncSync busy check")
c = replace_once(c, "if (g_hbBusy || g_hbDone || g_asyncBusy) return false;",
                    "if (g_hbBusy || g_hbDone || g_asyncBusy || g_spoolBusy) return false;",
                 "startAsyncHeartbeat busy check")
c = c.rstrip("\r\n") + cnl + CPP_BLOCK.replace("\n", cnl) + cnl

# ---- main.cpp
m = insert_after(m, mnl, "product_api::request(tag);",
                 ["if (to.type == PT_KLIPPER) ttcloud::requestSpool(tag.uid);"],
                 "product_api::request(tag)", indent_from=-1)
m = replace_once(m, "if (sendWaiting && !product_api::waiting(tag.idProduct)) {",
                    "if (sendWaiting && !product_api::waiting(tag.idProduct) && !ttcloud::spoolWaiting(tag.uid)) {",
                 "send waiting condition")

open(HDR, "wb").write(h.encode("utf-8"))
open(CPP, "wb").write(c.encode("utf-8"))
open(MAIN, "wb").write(m.encode("utf-8"))
print("OK: tigertag_cloud.h, tigertag_cloud.cpp and main.cpp patched.")
