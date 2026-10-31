#pragma once
#include "printer.h"
#include <WebSocketsClient.h>
#include <ArduinoJson.h>

// Klipper with AFC (e.g. OpenCreator on a FlashForge Creator 5 Pro) - Moonraker
// over WebSocket, same transport as the Snapmaker backend:
//   ws://<ip>:7125/websocket , JSON-RPC 2.0 , no authentication.
// 4 slots = AFC lanes extruder0..extruder3 = tools T0..T3.
// Assigning filament: printer.gcode.script -> "TT_SET_LANE TOOL=n MATERIAL=..
//   SUBTYPE=.. BRAND=.. COLOR=RRGGBB"  (macro in tigertag.cfg on the printer,
//   it calls AFC's SET_MATERIAL / SET_COLOR and stores brand + subtype in
//   save_variables).
// Slot state: objects "AFC_lane extruderN" (material, color "#RRGGBB", weight,
//   initial_weight), "AFC" (current_load), "save_variables" (tt_tN_brand,
//   tt_tN_sub) and "print_stats" (state).
class KlipperBackend : public PrinterBackend {
public:
    void begin(const PrinterCfg& cfg) override;
    void loop() override;
    void stop() override;
    bool connected() override;
    bool firstIsExternal() override { return false; }   // four tools, no external spool
    int  slotCount() override { return 4; }
    const char* slotLabel(int i) override;
    const SlotState& slot(int i) override;
    bool assign(int i, const TagInfo& t) override;
    String status() override;
    void refresh() override;
private:
    void applyStatus(JsonObjectConst st);
    void applyLane(int i, JsonObjectConst lane);
    void recompute(int i);
    void onMsg(uint8_t* payload, size_t len);
    void onEvent(WStype_t type, uint8_t* payload, size_t len);
    bool sendRaw(String s);

    WebSocketsClient ws_;
    bool      connected_ = false;
    String    status_ = "Klipper: connecting...";
    SlotState slots_[4];
    String    mat_[4], sub_[4];          // AFC material, subtype from save_variables
    float     weight_[4] = {0, 0, 0, 0};
    float     initW_[4]  = {0, 0, 0, 0};   // AFC's own initial_weight
    float     totalW_[4] = {0, 0, 0, 0};   // tag's full quantity, from save_variables
    int       currentLane_ = -1;
    bool      printing_ = false;
    uint32_t  lastReq_ = 0;

    // Read-back check after assign(): did the colour actually land?
    int       pendIdx_ = -1;
    uint8_t   pendR_ = 0, pendG_ = 0, pendB_ = 0;
    uint32_t  pendUntil_ = 0;
};
