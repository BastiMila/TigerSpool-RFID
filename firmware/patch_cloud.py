# patch_cloud.py - traegt PT_KLIPPER in src/tigertag_cloud.cpp und src/webcfg.cpp ein.
# Aufruf im Ordner firmware:  python patch_cloud.py
# Jede Stelle muss genau einmal gefunden werden, sonst bricht das Skript ab und
# schreibt NICHTS (auch nicht in die andere Datei).
import sys

def load(path):
    raw = open(path, "rb").read().decode("utf-8")
    return raw, ("\r\n" if "\r\n" in raw else "\n")

def replace_once(text, old, new, what):
    n = text.count(old)
    if n != 1:
        sys.exit("ABBRUCH: '%s' wurde %d mal gefunden (erwartet: 1). Nichts geaendert." % (what, n))
    return text.replace(old, new)

def insert_after_line(text, nl, needle, new_line, what):
    lines = text.split(nl)
    hits = [i for i, l in enumerate(lines) if needle in l]
    if len(hits) != 1:
        sys.exit("ABBRUCH: '%s' wurde %d mal gefunden (erwartet: 1). Nichts geaendert." % (what, len(hits)))
    i = hits[0]
    ind = lines[i][:len(lines[i]) - len(lines[i].lstrip())]
    lines.insert(i + 1, ind + new_line)
    return nl.join(lines)

# ---- tigertag_cloud.cpp
cp = "src/tigertag_cloud.cpp"
c, nl = load(cp)
c = replace_once(c, '"snapmaker", "elegoo", "anycubic" };',
                    '"snapmaker", "elegoo", "anycubic", "klipper" };', "BRANDS-Liste")
c = replace_once(c, 'PT_SNAPMAKER, PT_ELEGOO, PT_ANYCUBIC };',
                    'PT_SNAPMAKER, PT_ELEGOO, PT_ANYCUBIC, PT_KLIPPER };', "BRAND_TYPE-Liste")
c = insert_after_line(c, nl, 'if (brand == "snapmaker")',
                      'if (brand == "klipper")    return PT_KLIPPER;                              // Moonraker ws :7125, AFC',
                      "mapType snapmaker")

# ---- webcfg.cpp
wp = "src/webcfg.cpp"
w, wnl = load(wp)
w = replace_once(w, "'Elegoo','Anycubic'],", "'Elegoo','Anycubic','Klipper'],", "Markenliste im Web-Skript")

# Erst nach erfolgreichen Pruefungen schreiben
open(cp, "wb").write(c.encode("utf-8"))
open(wp, "wb").write(w.encode("utf-8"))
print("OK: tigertag_cloud.cpp und webcfg.cpp angepasst.")
print()
print("Zur Kontrolle, Stellen die von der Markenzahl abhaengen koennten:")
for i, l in enumerate(c.split(nl), 1):
    if "okBrands" in l or "BRAND_TYPE" in l or "BRANDS[" in l or "sizeof(BRANDS" in l:
        print("  tigertag_cloud.cpp:%d: %s" % (i, l.strip()))
