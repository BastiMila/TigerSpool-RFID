# How a TigerSpool declares itself to the account

So that Tiger Studio can list this device, show whether it is online, and tell
which printers it stands in front of.

The shape is **the TigerScale's**, deliberately. A device list, an online dot
and a battery icon should not have to know which product they are looking at,
so the identity, liveness and power fields carry the same names a TigerScale
writes. Only the state block differs, because a spool writer and a scale do
different work.

Reference: `HOW-TIGERSCALE-DECLARES-ITSELF.md`, written from the shipping
TigerScale V3 firmware.

---

## Where it writes

```
users/{uid}/tigerspools/{mac}
```

The collection is **plural**, like every other collection of things under an
account - `scales`, `printers`, `racks`. A user can own several TigerSpools and
each writes its own document; nothing is shared between them.

`{mac}` is the Wi-Fi MAC, lowercase hex, no separators, twelve characters -
`ttcloud::deviceId()`. **The format is frozen.** It is the document id, so
changing it orphans every device already registered under the old one, silently
and with no way to notice from the device.

The device lives under its owner's account. That is the whole access-control
story: a signed-in user may write below their own `users/{uid}`, and no device
allow-list or service account is involved.

### What Firebase has to allow

Until the rules name this path, every beat comes back `403 PERMISSION_DENIED`
and the device is invisible in Studio:

```
match /users/{uid}/tigerspools/{deviceId} {
  allow read, write: if request.auth != null && request.auth.uid == uid;
}
```

The device also declares itself to `pairStart` as `kind: "tigerspool"`,
`model: "TigerSpool"`, with its real firmware version and its mDNS name. It
used to say `bridge` / `TigerTag Bridge` / `cfs_ui`, which was the prototype's
name and a version string that was never a version.

---

## What it writes

One `documents:commit` per beat, with an `updateMask`, so the document is
created on the first beat and updated after - no separate registration step -
and fields outside the mask are left alone. Studio owns `display_name`; a
heartbeat must never trample it.

**Identity, on full beats only**

| field | type |
|---|---|
| `mac` | string, same as the document id |
| `fw_version` | string |
| `mdns_hostname` | string, `tigerspool-xxxx.local` |
| `display_name` | string, `TigerSpool-XXXX` by default |
| `hardware_revision` | null on this board |

**Liveness and power, on every beat**

| field | type |
|---|---|
| `last_heartbeat_at` | **server timestamp**, `REQUEST_TIME` |
| `wifi_signal_dbm` | int, or null when not connected |
| `ip_address` | string |
| `power_source` | `"usb"` or `"battery"` |
| `power_state` | `"active"` or `"screen_off"` |
| `battery_present` | bool |
| `battery_percent` | int, or null with no cell |
| `is_charging` | bool, or null with no cell |

**State, what a TigerSpool is that a scale is not**

| field | type |
|---|---|
| `printers_active` | int - how many printers the user has switched on here |
| `printer_ids` | array of strings - those printers' document ids in the account |
| `last_used_at` | server timestamp, written on the beat after a spool is sent |
| `printers_online` | array of strings - the subset of `printer_ids` this box can reach right now |

`printers_online` is what the box SEES, by the same rule as the dots on its own
home screen: a live link to that printer, or an answer to the reachability probe
within the last 25 s. Studio draws it as a green or grey dot in front of each
machine, and deliberately not from its own connection to the printer - the two
can differ (a printer reachable over the cloud from Studio and not from the
box's network, or the reverse). Sent when it moves, always as an array, empty
included: an omitted field would leave the last printer that went down green.

| `slots` | map - which spool this box put in which printer slot; see below |

### `slots`: which spool is in which slot

The printer reports a slot's material and a colour rounded to its own palette,
never which spool it is. Only this box knows that, because it read the chip
before setting the slot. `slots` publishes it, for a slicer plugin that wants
to pick the right spool for each slot:

```
slots: {                                   // key: a printer id from printer_ids
  "<printerId>": {
    "1": {"uid": "5301E4BA640001", "material": "PLA",
          "color": "#5A6D30", "set_at": 1794577283000},
    "3": null                              // emptied, or changed at the printer
  }
}
```

- **The slot key is the printer's own number** (`PrinterBackend::slotKey`):
  FlashForge's `slotId` (1-4), an AFC lane's number (from 1), a Happy Hare
  gate (from 0). Other brands publish nothing until their numbering is agreed.
- **`uid`** is the inventory document id, upper-case hex - one of the two chips
  of a twin-tagged spool.
- **`material` and `color` are what the printer reported** a few seconds after
  the send, not what was sent: a reader compares them with what the printer
  reports now, and they only match if both come from the printer.
- **`set_at`** is ms since epoch, taken from the server's `commitTime` of an
  earlier beat (this board has no clock). Left out until one beat has gone
  through.
- **A slot that is emptied, or reported with another material or colour, turns
  into an explicit `null`** - judged only once the link has been up for 15 s,
  because a backend reports its slots as unknown straight after a reconnect. So
  is a slot that never reports after a send (30 s), and a slot while a new send
  is still waiting for the printer.
- A spool swapped for one of the **same material and colour** without a scan is
  not noticed. Nothing the printer reports tells the two apart.
- **The whole map is written when it changes**, with `slots` in the mask. It is
  read back on the first full beat after boot and carried on; until that read has
  answered it is not written at all, so a booting box never wipes it.
- The book lives in RAM, at most 32 slots across all printers.

### `load_requests`: what a slicer wants loaded

The one field in this document the device **reads and never writes**. A slicer
plugin, signed in as the same user, puts a job's spools here so that the box in
front of the printer can say which spool goes into which slot and where it is
stored. It lives in this document, not a collection of its own, because this
path is already allowed by the account's rules; a new collection would need a
rule of its own first.

```
load_requests: {                           // key: a printer id from printer_ids
  "<printerId>": {
    "source": "orcaslicer",
    "requested_at": 1794577283000,         // ms since epoch
    "job": "Plate 1",
    "slots": {
      "2": {"uids": ["5301E4BA640001", "<twin>"], "uid": "5301E4BA640001",
            "brand": "Sunlu", "material": "PLA", "color": "#5A6D30", "grams_needed": 34.5,
            "rack": {"id": "...", "name": "Karton Sunlu 250G", "level": 1, "position": 4}},
      "3": null                            // nothing asked for this slot
    }
  }
}
```

- **Written by the plugin** with `updateMask: load_requests.<printerId>`, so one
  printer's request replaces only itself. The beat's own mask never names
  `load_requests`, so the device does not overwrite it.
- **The slot keys are the ones in `slots`.** `uids` names both chips of a
  twin-tagged spool, so a scan of either one matches; a single `uid` is accepted
  when `uids` is missing.
- **`rack.level` and `rack.position` count from 0**, and the place is drawn as
  Tiger Studio draws it: the name, then the level as a letter and the position
  from 1 - level 1, position 4 is "B5". A name the panel's font cannot draw is
  left out and the place is shown as "B5" alone, which is why the name comes
  apart from the numbers rather than as finished text.
- **Read with the beat, and only while a printer's slots are on screen** (the
  slot grid, the scan, the send and its result - the only places a request is
  shown): one GET with `mask.fieldPaths=load_requests` on the session the beat
  already holds, every 30 s there. Elsewhere it is not read at all, because on
  a weak network every round trip is seconds. A new request therefore shows
  up within 30 s of opening the printer.
- **A slot's request is done** when the slot book holds, confirmed by the
  printer, one of the spools it names. Done, or `requested_at` older than 24 h,
  it is no longer shown. Nothing is written back: the slicer compares its
  request with `slots`.
- **A spool with no chip yet** - an inventory id that is not hex, such as
  `TIGERDATA_...` - can never be matched by a scan. Its slot is shown with "No
  TigerTag set yet", and no spool sent there is called the wrong one.
- On the device: the slot grid outlines the wanted slots, puts their number on
  orange and says how many; the scan screen for a wanted slot shows, in orange,
  a swatch of `color`, `brand` and `material`, and the place; a send of another
  spool to that slot goes through, and the result screen says it was not the
  spool the slicer asked for. `job`, `grams_needed` and `place` are not read.
- At most 32 requested slots across all printers, in RAM.

`printer_ids` is what lets Studio put the right TigerSpool next to the right
printer. The ids come from the account import and are held **in RAM only**, so
a freshly booted device publishes an empty array until its first sync of the
boot - a minute or two.

They are not in NVS because they no longer fit. The partition is 20 KB, frozen,
and cannot grow over the air; it sits at 500 of 630 entries, and `putString`
refused a 361-byte value outright - NVS wants a blob's entries contiguous
inside one page, and there is no run that long left. Per-printer keys would fit
today and cost forty of the hundred and thirty entries that remain. The account
can always resend the ids; the flash cannot be given back.

### Three rules that are not decoration

**The timestamp is the server's.** This board has no RTC and no NTP, so any
time it wrote itself would be a guess presented as a fact. `REQUEST_TIME` makes
presence decidable by the server's own clock: online is
`now - last_heartbeat_at < 2 * interval`, whatever the device believes the time
to be.

**Nulls are explicit.** An absent battery writes
`"battery_percent": {"nullValue": "NULL_VALUE"}`. Omitting the field would keep
whatever was there before, so a box that once had a cell would read as still
having one for ever.

**Full beat, then deltas.** The first beat after boot writes everything; after
that only what moved. The full flag is cleared **only on a successful commit**,
so a failed full beat is retried as a full beat rather than leaving Studio with
half a document.

---

## When it beats

| | |
|---|---|
| Awake | every 30 s |
| Screen off | every 5 min |
| A spool written, a cable moved, the screen sleeping or waking, the printer count changing | at once |

The forced beats are the ones a person performs and then watches for. Waiting
five minutes for them reads as an app that has lost the device.

Three refusals in a row hold the next beat for five minutes. A misconfigured
account then costs a handshake now and then, rather than one every thirty
seconds for ever.

## What it costs, and the two traps

A beat is one HTTPS round trip, about a second, run from the main loop. That is
survivable only because the interface has a task of its own: the panel keeps
drawing while the loop is inside the handshake. It is **not** free of the
memory problem - mbedTLS wants a contiguous block of internal RAM - so a beat
is skipped when the largest free block is under 24 KB, and skipped entirely
while the account sync is running. One TLS session at a time.

`battery_present` is what the OWNER declared in Settings, not a measurement.
The board cannot detect a cell - see `battery.cpp` in CODEMAP.md for the two
signals that were tried and how each of them lied - so the device asks instead
of guessing. Until someone answers, it reports no battery and `battery_percent`
and `is_charging` are null.

`power_source` is inferred from that declaration and the charging state, not
read from a pin. Treat it as a hint.

---

## Not done yet

The command queue - `users/{uid}/tigerspools/{mac}/commands`, polled, with
`pending → ack → in_progress → done | error` - is deliberately left for later.
Presence is what Studio is built around; commands are additive and can land
without changing anything written here.
