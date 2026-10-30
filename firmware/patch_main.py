# patch_main.py - traegt PT_KLIPPER in src/main.cpp ein.
# Aufruf im Ordner firmware:  python patch_main.py
# Es aendert nur src/main.cpp. Jede Stelle muss genau einmal gefunden werden,
# sonst bricht das Skript ab und schreibt NICHTS.
import sys

PATH = "src/main.cpp"
raw = open(PATH, "rb").read().decode("utf-8")
nl = "\r\n" if "\r\n" in raw else "\n"
lines = raw.split(nl)

def find_one(pred, what):
    hits = [i for i, l in enumerate(lines) if pred(l)]
    if len(hits) != 1:
        sys.exit("ABBRUCH: '%s' wurde %d mal gefunden (erwartet: 1). Nichts geaendert." % (what, len(hits)))
    return hits[0]

def indent_of(l):
    return l[:len(l) - len(l.lstrip())]

edits = []   # (index, kind, payload)

# 1. Include
i = find_one(lambda l: l.strip() == '#include "backend_snapmaker.h"', 'include backend_snapmaker.h')
edits.append((i, "after", '#include "backend_klipper.h"'))

# 2. typeTag
i = find_one(lambda l: 't == PT_SNAPMAKER ? "Snap" : "--";' in l, 'typeTag')
edits.append((i, "replace", lines[i].replace('t == PT_SNAPMAKER ? "Snap" : "--";',
                                              't == PT_SNAPMAKER ? "Snap" : t == PT_KLIPPER ? "Klip" : "--";')))

# 3..6. switch-Zeilen
def after_case(anchor, newline_text, what):
    i = find_one(lambda l: l.strip() == anchor, what)
    edits.append((i, "after", indent_of(lines[i]) + newline_text))

after_case("case PT_SNAPMAKER: return 7125;",                 "case PT_KLIPPER:   return 7125;",                 "ctrlPort")
after_case("case PT_SNAPMAKER: return new SnapmakerBackend();", "case PT_KLIPPER:   return new KlipperBackend();", "newBackend")
after_case("case PT_SNAPMAKER: return 8000;",                 "case PT_KLIPPER:   return 8000;",                 "linkCost")
after_case('case PT_SNAPMAKER: return "snapmaker";',          'case PT_KLIPPER:   return "klipper";',            "brandName")

# Anwenden von hinten nach vorn, damit die Indizes stimmen
for i, kind, payload in sorted(edits, key=lambda e: -e[0]):
    if kind == "after":
        lines.insert(i + 1, payload)
    else:
        lines[i] = payload

open(PATH, "wb").write(nl.join(lines).encode("utf-8"))
print("OK: %d Stellen in %s angepasst (Zeilenende: %s)" % (len(edits), PATH, "CRLF" if nl == "\r\n" else "LF"))
