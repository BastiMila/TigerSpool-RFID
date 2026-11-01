## Klipper printers with AFC (experimental)

> **Status: experimental.** Written against the Snapmaker backend and run on one
> setup: FlashForge Creator 5 Pro, OpenCreator, AFC 1.2.8. Other AFC printers
> have not been tried.

TigerSpool can follow a Klipper printer that uses
[AFC](https://github.com/ArmoredTurtle/AFC-Klipper-Add-On) for its filament
slots, and write a scanned TigerTag into one of them. It talks to Moonraker over
a WebSocket (`ws://<ip>:7125/websocket`, no authentication), the same transport
the Snapmaker backend uses. The printer comes down with the account like every
other brand: Tiger Studio stores it under `printers/klipper/devices`.

### What it needs

- Klipper with Moonraker, reachable on the LAN at port 7125.
- AFC with four lanes named `extruder0` to `extruder3`, one per tool (T0 to T3).
  Other lane layouts are not handled yet.
- A `[save_variables]` section in the printer config.
- The printer added in Tiger Studio Manager as a **Klipper** printer.
- The macros in [`klipper/tigertag.cfg`](../klipper/tigertag.cfg) on the printer.

### Printer setup

1. Copy `tigertag.cfg` into the printer's config folder.
2. Add `[include tigertag.cfg]` to `printer.cfg` and restart Klipper.
3. Check that AFC accepts the commands the macro uses, one at a time in the
   console, with the printer idle:

   ```
   SET_MATERIAL LANE=extruder0 MATERIAL=PLA
   SET_COLOR LANE=extruder0 COLOR=FF0000
   SET_WEIGHT LANE=extruder0 WEIGHT=800
   AFC_SET_SPOOL_TEMP LANE=extruder0 EXTRUDER_TEMP=210 BED_TEMP=60
   ```

   `AFC_SET_SPOOL_TEMP` is the command the "Set spool" button in Fluidd's lane
   dialog sends.
4. Then try the macro itself:

   ```
   TT_SET_LANE TOOL=0 MATERIAL=PLA COLOR=FF0000 TEMP=210 BEDTEMP=60 WEIGHT=250 INITIAL=300 BRAND=Test
   ```

   The console answers with every parameter it received. Open
   `http://<printer-ip>:7125/printer/objects/query?AFC_lane%20extruder0` to see
   what the lane now holds.

### What the spool shows

One row of four slots, labelled T0 to T3, read from Moonraker every five
seconds:

| Shown | Comes from |
| --- | --- |
| Material and variant ("PLA Silk") | `AFC_lane extruderN.material`, variant from `save_variables` |
| Colour | `AFC_lane extruderN.color` |
| Brand | `save_variables` (`tt_tN_brand`), else AFC's `spool_vendor` |
| Remaining | `weight` divided by the spool's full quantity (`tt_tN_total`), else AFC's `initial_weight` |
| Active tool | `AFC.current_load` |

### What a scan writes

Assigning a tag to a slot sends one command:

```
TT_SET_LANE TOOL=n MATERIAL=PLA SUBTYPE=Silk COLOR=RRGGBB BRAND=eSUN TEMP=210 BEDTEMP=60 WEIGHT=232 INITIAL=300
```

- `MATERIAL` is the first word of the tag's material, `SUBTYPE` the rest, the
  way the Snapmaker backend splits them.
- `TEMP` and `BEDTEMP` are the middle of the tag's nozzle and bed ranges. The
  macro passes both to `AFC_SET_SPOOL_TEMP` and refuses values above the
  heaters' `max_temp`.
- `WEIGHT` and `INITIAL` come from the account, never from the chip. A chip is
  written once; what a TigerScale measures afterwards goes to the spool's
  document, `users/{uid}/inventory/{chip UID}`. The spool starts reading it the
  moment the tag is read and the send waits for it up to 8 seconds. `WEIGHT` is
  `weight_available`, `INITIAL` is `measure_gr` (else `measure`).
- When the account does not answer in time - not signed in, no such document,
  the document marked deleted, or the sync holding the session - the command
  goes out without `WEIGHT` and `INITIAL` and the weight already on the printer
  stays. The result screen says so under the spool, in orange, in the device's
  language ("Weight not updated - the printer keeps its previous value"), and
  the status line adds **(weight not updated)**.
- Afterwards the spool reads the lane back. The status line says **confirmed**
  when the colour arrived, **sent, not confirmed** after twelve seconds if it did
  not, or shows Moonraker's own error text if the command was refused.

### Limits

- A slot is not written while the printer is printing or paused.
- The weight needs the device signed in to the TigerTag account that holds the
  spool. Without that, or when the read is slow, a scan updates everything
  except the weight.
- AFC keeps `initial_weight` at 1000 g. Fluidd draws its spool graphic from it,
  so a 300 g spool looks about a quarter full there when it is full. The spool's
  own percentage uses the real full quantity and is not affected.
- Brand, variant, full quantity and tag id live in `save_variables`, not in AFC.
  AFC itself does not see them.
- Spoolman is not involved.
- Memory: charged as 5 load slots, the same as a Snapmaker. That figure is
  borrowed, not measured on this device.

### Files

| File | Change |
| --- | --- |
| `src/backend_klipper.h`, `src/backend_klipper.cpp` | new backend |
| `src/printer.h` | `PT_KLIPPER = 7` |
| `src/printer_budget.h` | load slots for `PT_KLIPPER` |
| `src/main.cpp` | backend creation, port, link cost, brand name |
| `src/tigertag_cloud.h`, `src/tigertag_cloud.cpp` | `klipper` brand in the account import; the spool's weight read from the inventory |
| `src/webcfg.cpp` | "Klipper" in the brand list of the web page |
| `klipper/tigertag.cfg` | printer-side macros (`TT_SET_LANE`, `TT_LOAD`) |
