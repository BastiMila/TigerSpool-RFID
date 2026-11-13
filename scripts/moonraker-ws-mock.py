#!/usr/bin/env python3
"""A fake Klipper printer for the Klipper backend, over Moonraker's WebSocket.

Tiger Studio has an HTTP mock (scripts/moonraker-mock.mjs in its repository);
the device talks JSON-RPC over the WebSocket instead, so this is that half.
Python standard library only.

  python3 scripts/moonraker-ws-mock.py                       # plain printer, :7125
  python3 scripts/moonraker-ws-mock.py --profile happyhare   # Happy Hare, 4 gates
  python3 scripts/moonraker-ws-mock.py --profile afc         # AFC, 4 lanes
  python3 scripts/moonraker-ws-mock.py --port 7126 --api-key secret

Open http://<this machine's IP>:<port>/ in a browser for a page that shows the
lanes, what TigerSpool stored with them, the print state, and lets you start or
cancel a print, set a lane's weight, and load or unload a lane. Unloading
clears the lane's material and colour, as AFC does when a spool is ejected;
POST /mock/lane?name=<lane>&loaded=0|1 does the same from a script.

A print can be simulated, so the end-of-print path can be tested without a
printer: POST /printer/print/start sets print_stats to "printing" for
--print-seconds (30) and draws --print-grams (20) off every loaded AFC lane over
that time, then reports "complete". POST /printer/print/cancel ends it early as
"cancelled". The same two calls Moonraker itself answers:

  curl -X POST http://localhost:7125/printer/print/start
  curl -X POST http://localhost:7125/printer/print/cancel

Then add a Klipper printer in Tiger Studio at <this machine's IP>[:port] and let
the device sync. Studio itself talks HTTP to Moonraker, so the few HTTP
requests its add flow and its card make are answered too (server/printer/
machine info, the object list and query, an empty webcam list). What it answers: server.connection.identify,
printer.objects.list, printer.objects.query and printer.gcode.script with
MMU_GATE_MAP, AFC's SET_MATERIAL / SET_COLOR / SET_WEIGHT / AFC_SET_SPOOL_TEMP
and Klipper's SAVE_VARIABLE parsed into the state - so a
write is read back exactly as it would be from a printer. It also pushes a
notify_proc_stat_update every second, as Moonraker does to every client.

The AFC profile publishes its lanes twice, as `AFC_stepper` and `AFC_lane`,
the way an OpenCentauri CC1 does: the backend must list each lane once.
"""

import ast
import argparse
import base64
import hashlib
import json
import socket
import struct
import sys
import threading
import urllib.parse
import time

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def build_state(profile):
    s = {"toolhead": {"extruder": "extruder"}, "extruder": {}, "heater_bed": {},
         "print_stats": {"state": "standby"}, "webhooks": {"state": "ready"}}
    if profile == "happyhare":
        s["mmu"] = {"num_gates": 4, "gate": 1,
                    "gate_status": [1, 1, 0, -1],
                    "gate_material": ["PLA", "PETG", "", "ASA"],
                    "gate_color": ["FF0000", "00FF00", "", "red"]}
    elif profile == "afc":
        lanes = ["lane1", "lane2", "lane3", "lane4"]
        s["AFC"] = {"lanes": lanes, "current_lane": "lane2"}
        s["save_variables"] = {"variables": {}}
        for i, lane in enumerate(lanes):
            obj = {"map": f"T{i}", "material": ["PLA", "PETG", "", "TPU"][i],
                   "color": ["#FF8800", "#0055FF", "#000000", "#FFFFFF"][i],
                   "load": i != 2, "prep": i != 2, "tool_loaded": False, "weight": 850,
                   "initial_weight": 1000, "extruder_temp": 210, "bed_temp": 60}
            s[f"AFC_stepper {lane}"] = obj
            s[f"AFC_lane {lane}"] = obj          # the same lane, published twice
    else:
        s["extruder1"] = {}
    return s


def params_of(line):
    out = {}
    for tok in line.split()[1:]:
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k.upper()] = v
    return out


def run_script(state, script):
    for line in script.split("\n"):
        line = line.strip()
        if not line:
            continue
        cmd, p = line.split()[0].upper(), params_of(line)
        if cmd == "MMU_GATE_MAP" and "mmu" in state:
            g = int(p.get("GATE", -1))
            m = state["mmu"]
            if not 0 <= g < m["num_gates"]:
                raise ValueError(f"Invalid gate {p.get('GATE')}")
            if "MATERIAL" in p:
                m["gate_material"][g] = p["MATERIAL"]
            if "COLOR" in p:
                m["gate_color"][g] = p["COLOR"].lstrip("#")
            m["gate_status"][g] = 1
        elif cmd == "SAVE_VARIABLE" and "save_variables" in state:
            # Klipper lowercases the name and reads the value as a Python literal.
            v = p.get("VALUE", "")
            if len(v) >= 2 and v[0] == v[-1] == '"':
                v = v[1:-1]
            state["save_variables"]["variables"][p.get("VARIABLE", "").lower()] = ast.literal_eval(v)
        elif cmd in ("SET_MATERIAL", "SET_COLOR", "SET_WEIGHT", "AFC_SET_SPOOL_TEMP") and "AFC" in state:
            lane = state.get(f"AFC_lane {p.get('LANE', '')}")
            if lane is None:
                raise ValueError(f"Unknown lane {p.get('LANE')}")
            if cmd == "SET_MATERIAL":
                lane["material"] = p.get("MATERIAL", "")
            elif cmd == "SET_WEIGHT":
                lane["weight"] = float(p.get("WEIGHT", 0))
            elif cmd == "AFC_SET_SPOOL_TEMP":
                lane["extruder_temp"] = float(p.get("EXTRUDER_TEMP", lane["extruder_temp"]))
                lane["bed_temp"] = float(p.get("BED_TEMP", lane["bed_temp"]))
            else:
                lane["color"] = "#" + p.get("COLOR", "").lstrip("#")
        else:
            raise ValueError(f"Unknown command \"{cmd}\"")


def recv_frame(conn):
    head = conn.recv(2)
    if len(head) < 2:
        return None, None
    op, n = head[0] & 0x0F, head[1] & 0x7F
    if n == 126:
        n = struct.unpack(">H", conn.recv(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", conn.recv(8))[0]
    mask = conn.recv(4) if head[1] & 0x80 else b"\0\0\0\0"
    data = b""
    while len(data) < n:
        chunk = conn.recv(n - len(data))
        if not chunk:
            return None, None
        data += chunk
    return op, bytes(b ^ mask[i % 4] for i, b in enumerate(data))


def send_frame(conn, payload, op=1):
    n = len(payload)
    head = bytes([0x80 | op])
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    conn.sendall(head + payload)


# The simulated print: how long, how much it uses, and whether one is running.
PRINT = {"seconds": 30.0, "grams": 20.0, "running": False, "cancel": False}


def run_print(state, lock):
    with lock:
        lanes = {k: v for k, v in state.items() if k.startswith("AFC_lane ")}
        start = {k: float(v.get("weight", 0)) for k, v in lanes.items() if v.get("load")}
        state["print_stats"]["state"] = "printing"
    t0, dur, grams = time.time(), PRINT["seconds"], PRINT["grams"]
    print(f"print started: {dur:.0f} s, {grams:.0f} g off {len(start)} lane(s)")
    end = "complete"
    while True:
        time.sleep(1)
        frac = min(1.0, (time.time() - t0) / dur)
        with lock:
            for k, w0 in start.items():
                state[k]["weight"] = round(max(0.0, w0 - grams * frac), 1)
        if PRINT["cancel"]:
            end = "cancelled"
            break
        if frac >= 1.0:
            break
    with lock:
        state["print_stats"]["state"] = end
        left = {k[9:]: state[k]["weight"] for k in start}
    PRINT["running"] = PRINT["cancel"] = False
    print(f"print {end}: {left}")

# A page to watch and drive the mock from a browser, at http://<host>:<port>/.
# One file, no dependencies, polling once a second. With --api-key, open it as
# /?key=<the key>: its requests carry the key, as the device's do.
PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Moonraker mock</title>
<style>
:root{--bg:#f3f4f6;--card:#fff;--ink:#16191d;--dim:#5f6b7a;--line:#dde1e6;--soft:#eef0f3;
 --acc:#2f6fed;--ok:#12a150;--warn:#d97706;--bad:#d92d20}
@media (prefers-color-scheme:dark){:root{--bg:#0f1113;--card:#1a1d21;--ink:#e9ebee;--dim:#97a1ad;
 --line:#2c3138;--soft:#23272d;--acc:#5b8def;--ok:#3ccb7f;--warn:#f5a524;--bad:#f97066}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:980px;margin:0 auto;padding:20px 16px 32px}
header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:6px}
h1{font-size:20px;margin:0;margin-right:auto}
.pill{display:inline-flex;align-items:center;gap:6px;font-weight:600;font-size:13px;padding:4px 10px;
 border-radius:99px;background:var(--soft);color:var(--dim)}
.pill::before{content:"";width:8px;height:8px;border-radius:50%;background:currentColor}
.pill.printing{color:var(--acc)} .pill.complete{color:var(--ok)} .pill.cancelled{color:var(--warn)}
.sub{color:var(--dim);font-size:13px;margin:0 0 16px}
button{font:inherit;font-size:14px;padding:7px 12px;border:1px solid var(--line);border-radius:8px;
 background:var(--card);color:var(--ink);cursor:pointer;white-space:nowrap}
button:hover:not(:disabled){border-color:var(--acc)}
button:disabled{opacity:.45;cursor:default}
button.primary{background:var(--acc);border-color:var(--acc);color:#fff}
#err{color:var(--bad);font-size:14px;min-height:1.45em;margin-bottom:8px}
.lanes{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:14px}
.lane{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;display:flex;
 flex-direction:column;gap:12px;min-width:0}
.top{display:flex;align-items:center;gap:8px}
.name{font-weight:700}
.map{font-size:12px;color:var(--dim);background:var(--soft);border-radius:6px;padding:1px 6px}
.tool{margin-left:auto;font-size:12px;font-weight:600;color:var(--acc)}
.spool{position:relative;height:92px;border-radius:10px;overflow:hidden;border:1px solid var(--line)}
.spool .fill{position:absolute;left:0;right:0;bottom:0}
.spool .txt{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;
 justify-content:center;text-align:center;padding:0 8px}
.spool .mat{font-weight:700;font-size:15px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%}
.spool .g{font-size:13px;font-variant-numeric:tabular-nums}
.spool.empty{border:2px dashed var(--line);background:transparent}
.spool.empty .txt{color:var(--dim);font-size:14px}
dl{display:grid;grid-template-columns:auto 1fr;gap:3px 10px;margin:0;font-size:13px}
dt{color:var(--dim)} dd{margin:0;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
dd.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px}
dd.none{color:var(--dim)}
.ctl{display:flex;gap:6px}
.ctl input{flex:1;min-width:0;font:inherit;font-size:14px;padding:6px 8px;border:1px solid var(--line);
 border-radius:8px;background:var(--bg);color:var(--ink)}
.ld{width:100%}
</style></head><body><main>
<header><h1>Moonraker mock</h1><span id="st" class="pill">-</span>
<button id="start" class="primary">Start print</button><button id="cancel">Cancel print</button></header>
<p class="sub">A fake Klipper printer with AFC lanes, for testing TigerSpool without a real one.</p>
<div id="err"></div>
<div id="lanes" class="lanes"></div>
</main><script>
const key = new URLSearchParams(location.search).get('key') || '';
const hdr = key ? {'x-api-key': key} : {};
let lanes = null;
async function rpc(path, opt) {
  const r = await fetch(path, Object.assign({headers: Object.assign({'Content-Type': 'application/json'}, hdr)}, opt));
  const j = await r.json();
  if (!r.ok) throw new Error((j.error && (j.error.message || j.error.code)) || r.status);
  return j.result;
}
async function act(path) { try { await rpc(path, {method: 'POST'}); tick(); } catch (e) { err(e); } }
function err(e) { document.getElementById('err').textContent = e ? String(e.message || e) : ''; }
function hex(c) {
  const m = /^#?([0-9a-f]{6})/i.exec(c || '');
  return m ? '#' + m[1] : '#888888';
}
function ink(c) {
  const n = parseInt(hex(c).slice(1), 16), r = n >> 16, g = (n >> 8) & 255, b = n & 255;
  return (r * 299 + g * 587 + b * 114) / 1000 > 150 ? '#111' : '#fff';
}
document.getElementById('start').onclick = () => act('/printer/print/start');
document.getElementById('cancel').onclick = () => act('/printer/print/cancel');

function build(i, lane) {
  const el = document.createElement('div');
  el.className = 'lane'; el.id = 'lane' + i;
  el.innerHTML =
    '<div class="top"><span class="name"></span><span class="map"></span><span class="tool"></span></div>' +
    '<div class="spool"><div class="fill"></div><div class="txt"><div class="mat"></div><div class="g"></div></div></div>' +
    '<dl><dt>Brand</dt><dd class="brand"></dd><dt>Temps</dt><dd class="temps"></dd>' +
    '<dt>Chip</dt><dd class="chip mono"></dd></dl>' +
    '<div class="ctl"><input type="number" min="0" placeholder="Weight in g"><button class="set">Set</button></div>' +
    '<button class="ld"></button>';
  const input = el.querySelector('input');
  const send = () => {
    if (input.value === '') return;
    act('/printer/gcode/script?script=' + encodeURIComponent('SET_WEIGHT LANE=' + lane + ' WEIGHT=' + input.value));
    input.value = '';
  };
  el.querySelector('.set').onclick = send;
  input.addEventListener('keydown', e => { if (e.key === 'Enter') send(); });
  document.getElementById('lanes').appendChild(el);
  return el;
}

async function tick() {
  try {
    if (!lanes) lanes = (await rpc('/printer/objects/list')).objects.filter(o => o.startsWith('AFC_lane '));
    const objects = {print_stats: null, save_variables: null, AFC: null};
    lanes.forEach(l => objects[l] = null);
    const st = (await rpc('/printer/objects/query', {method: 'POST', body: JSON.stringify({objects})})).status;
    const s = st.print_stats.state, printing = s === 'printing';
    const pill = document.getElementById('st'); pill.textContent = s; pill.className = 'pill ' + s;
    document.getElementById('start').disabled = printing;
    document.getElementById('cancel').disabled = !printing;
    const sv = (st.save_variables || {}).variables || {};
    const cur = (st.AFC || {}).current_lane || '';
    if (!lanes.length) document.getElementById('lanes').textContent = 'No AFC lanes - start the mock with --profile afc.';
    lanes.forEach((name, i) => {
      const l = st[name], lane = name.slice(9), v = k => sv['tt_' + lane.toLowerCase() + '_' + k];
      const el = document.getElementById('lane' + i) || build(i, lane);
      const empty = !l.load && !l.prep;
      const total = Number(v('total')) || Number(l.initial_weight) || 0;
      const pct = total > 0 ? Math.max(0, Math.min(100, l.weight * 100 / total)) : 0;
      const col = hex(l.color);

      el.querySelector('.name').textContent = lane;
      el.querySelector('.map').textContent = Array.isArray(l.map) ? l.map.join(',') : (l.map || '');
      el.querySelector('.tool').textContent = (l.tool_loaded || lane === cur) ? 'in tool' : '';

      const sp = el.querySelector('.spool');
      sp.classList.toggle('empty', empty);
      const fill = el.querySelector('.fill');
      if (empty) {
        sp.style.background = ''; fill.style.height = '0';
        el.querySelector('.mat').textContent = 'Empty';
        el.querySelector('.g').textContent = '';
      } else {
        sp.style.background = 'color-mix(in srgb, ' + col + ' 28%, var(--card))';
        fill.style.background = col; fill.style.height = pct + '%';
        const txt = el.querySelector('.txt');
        txt.style.color = pct >= 50 ? ink(col) : 'var(--ink)';
        el.querySelector('.mat').textContent = (l.material || '?').replace(/_/g, ' ');
        el.querySelector('.g').textContent = Math.round(l.weight) + ' g of ' + (total || '?') + ' g (' + Math.round(pct) + ' %)';
      }

      const brand = el.querySelector('.brand'), chip = el.querySelector('.chip');
      brand.textContent = v('brand') ? String(v('brand')).replace(/_/g, ' ') : 'none';
      brand.classList.toggle('none', !v('brand'));
      el.querySelector('.temps').textContent = (l.extruder_temp || '-') + ' / ' + (l.bed_temp || '-') + ' °C';
      chip.textContent = v('uid') || 'not from TigerSpool';
      chip.title = v('uid') || '';
      chip.classList.toggle('none', !v('uid'));

      const ld = el.querySelector('.ld');
      ld.textContent = empty ? 'Load lane' : 'Unload lane';
      ld.onclick = () => act('/mock/lane?name=' + encodeURIComponent(lane) + '&loaded=' + (empty ? 1 : 0));
    });
    err('');
  } catch (e) { err(e); }
}
tick(); setInterval(tick, 1000);
</script></body></html>
"""


def serve_http(conn, addr, req_line, headers, body, state, api_key, lock):
    method, target = (req_line.split(" ") + ["", ""])[:2]
    path, _, query = target.partition("?")
    if path == "/" and method == "GET":
        page = PAGE.encode()
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                     + f"Content-Length: {len(page)}\r\nConnection: close\r\n\r\n".encode() + page)
        print(f"[{addr[0]}] http GET / -> 200")
        conn.close()
        return
    if api_key and headers.get("x-api-key") != api_key:
        result, code = None, 401
    else:
        code = 200
        with lock:
            if path == "/server/info":
                result = {"klippy_connected": True, "klippy_state": "ready", "components": []}
            elif path == "/printer/info":
                result = {"state": "ready", "state_message": "Printer is ready",
                          "hostname": "moonraker-mock", "software_version": "v0.12.0-mock"}
            elif path == "/machine/system_info":
                result = {"system_info": {"cpu_info": {"serial_number": "MOCK0001"}}}
            elif path == "/printer/objects/list":
                result = {"objects": list(state.keys())}
            elif path == "/server/webcams/list":
                result = {"webcams": []}
            elif path == "/printer/print/start" and method == "POST":
                if not PRINT["running"]:
                    PRINT["running"], PRINT["cancel"] = True, False
                    threading.Thread(target=run_print, args=(state, lock), daemon=True).start()
                result = "ok"
            elif path == "/printer/print/cancel" and method == "POST":
                PRINT["cancel"] = PRINT["running"]
                result = "ok"
            elif path == "/mock/lane" and method == "POST":
                # Not Moonraker: the mock's own stand-in for putting a spool in
                # a lane or taking it out.
                q = urllib.parse.parse_qs(query)
                lane = state.get("AFC_lane " + q.get("name", [""])[0])
                if lane is None:
                    result, code = "Unknown lane", 400
                else:
                    loaded = q.get("loaded", ["1"])[0] == "1"
                    lane["load"] = lane["prep"] = loaded
                    if not loaded:
                        lane["material"], lane["color"], lane["tool_loaded"] = "", "#000000", False
                    result = "ok"
            elif path == "/printer/gcode/script" and method == "POST":
                script = urllib.parse.parse_qs(query).get("script", [""])[0]
                try:
                    run_script(state, script)
                    result = "ok"
                except ValueError as e:
                    result, code = str(e), 400
            elif path == "/printer/objects/query":
                wanted = {}
                if method == "POST" and body:
                    wanted = (json.loads(body or "{}").get("objects") or {})
                else:
                    wanted = {urllib.parse.unquote(k): None for k in query.split("&") if k}
                out = {}
                for name, fields in wanted.items():
                    if name in state:
                        obj = state[name]
                        out[name] = {f: obj[f] for f in (fields or obj) if f in obj}
                result = {"eventtime": time.time(), "status": out}
            else:
                result, code = None, 404
    err = {"code": code}
    if code == 400:
        err["message"] = result
    payload = json.dumps({"result": result} if code == 200 else {"error": err}).encode()
    reason = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found"}[code]
    conn.sendall(f"HTTP/1.1 {code} {reason}\r\nContent-Type: application/json\r\n"
                 f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode() + payload)
    print(f"[{addr[0]}] http {method} {path} -> {code}")
    conn.close()


def serve(conn, addr, state, api_key, lock):
    req = b""
    while b"\r\n\r\n" not in req:
        chunk = conn.recv(4096)
        if not chunk:
            return
        req += chunk
    head, _, body = req.partition(b"\r\n\r\n")
    lines = head.decode(errors="replace").split("\r\n")
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    if headers.get("upgrade", "").lower() != "websocket":
        need = int(headers.get("content-length", "0") or 0)
        while len(body) < need:
            chunk = conn.recv(4096)
            if not chunk:
                break
            body += chunk
        serve_http(conn, addr, lines[0], headers, body.decode(errors="replace"), state, api_key, lock)
        return
    if "origin" in headers:
        # Moonraker refuses an origin outside cors_domains; the device must send none.
        conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        print(f"[{addr[0]}] refused: Origin header {headers['origin']!r}")
        return
    accept = base64.b64encode(hashlib.sha1((headers.get("sec-websocket-key", "") + GUID)
                                           .encode()).digest()).decode()
    conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                  f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode())
    print(f"[{addr[0]}] websocket open")
    authed = {"ok": not api_key}
    alive = {"on": True}
    send_lock = threading.Lock()

    def push():
        while alive["on"]:
            time.sleep(1)
            msg = {"jsonrpc": "2.0", "method": "notify_proc_stat_update",
                   "params": [{"cpu_temp": 45.0, "moonraker_stats": {"cpu_usage": 2.1}}]}
            try:
                with send_lock:
                    send_frame(conn, json.dumps(msg).encode())
            except OSError:
                return
    threading.Thread(target=push, daemon=True).start()

    def reply(rid, result=None, error=None):
        msg = {"jsonrpc": "2.0", "id": rid}
        if error:
            msg["error"] = {"code": 400, "message": error}
        else:
            msg["result"] = result
        with send_lock:
            send_frame(conn, json.dumps(msg).encode())

    try:
        while True:
            op, data = recv_frame(conn)
            if op is None or op == 8:
                break
            if op == 9:
                with send_lock:
                    send_frame(conn, data, op=10)
                continue
            if op != 1:
                continue
            msg = json.loads(data)
            method, rid, p = msg.get("method"), msg.get("id"), msg.get("params") or {}
            shown = {k: ("***" if k == "api_key" else v) for k, v in p.items()}
            print(f"[{addr[0]}] <- {method} {json.dumps(shown)[:160]}")
            if method == "server.connection.identify":
                if api_key and p.get("api_key") != api_key:
                    reply(rid, error="Unauthorized")
                else:
                    authed["ok"] = True
                    reply(rid, {"connection_id": 1})
                continue
            if not authed["ok"]:
                reply(rid, error="Unauthorized")
                continue
            with lock:
                if method == "printer.objects.list":
                    reply(rid, {"objects": list(state.keys())})
                elif method == "printer.objects.query":
                    out = {}
                    for name, fields in (p.get("objects") or {}).items():
                        if name in state:
                            obj = state[name]
                            out[name] = {f: obj[f] for f in (fields or obj) if f in obj}
                    reply(rid, {"eventtime": time.time(), "status": out})
                elif method == "printer.gcode.script":
                    try:
                        run_script(state, p.get("script", ""))
                        reply(rid, "ok")
                    except ValueError as e:
                        reply(rid, error=str(e))
                else:
                    reply(rid, error=f"Method not found: {method}")
    except (OSError, ValueError) as e:
        print(f"[{addr[0]}] closed: {e}")
    finally:
        alive["on"] = False
        conn.close()
        print(f"[{addr[0]}] websocket closed")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--profile", choices=["plain", "happyhare", "afc"], default="plain")
    ap.add_argument("--port", type=int, default=7125)
    ap.add_argument("--api-key", default="")
    ap.add_argument("--print-seconds", type=float, default=30.0)
    ap.add_argument("--print-grams", type=float, default=20.0)
    a = ap.parse_args()
    PRINT["seconds"], PRINT["grams"] = max(1.0, a.print_seconds), a.print_grams
    sys.stdout.reconfigure(line_buffering=True)      # each line as it happens, even into a file
    state, lock = build_state(a.profile), threading.Lock()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", a.port))
    srv.listen(8)
    print(f"Moonraker WebSocket mock, profile {a.profile}, port {a.port}"
          f"{', API key required' if a.api_key else ''}")
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=serve, args=(conn, addr, state, a.api_key, lock), daemon=True).start()


if __name__ == "__main__":
    main()
