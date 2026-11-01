#!/usr/bin/env python3
"""
Simulation A: Neustart waehrend des DPS-Throttle-up (P63), Luminary 099 vs. 116.

Kein AGC-Emulator, sondern ein Modell, das direkt auf dem echten Quellcode arbeitet:
  1. Das Phasenwort hinter TC 2PHSCHNG in TIG-0 wird nach dem Format aus
     PHASE_TABLE_MAINTENANCE.agc ("TL0 00P PPP PPP GGG") dekodiert.
  2. Der passende Eintrag G.PSPOT wird in RESTART_TABLES.agc gesucht.
  3. Das Restart-Ziel (Symbol) wird ueber Labels und "="-Gleichsetzungen aufgeloest.
  4. Ab dem Ziel wird der Code instruktionsweise symbolisch abgelaufen
     (TCF/TC-Spruenge, IBNKCALL-Aufrufe, CHECKMM, Speicherzugriffe), bis TASKOVER.
  5. Zum Vergleich wird der normale (nicht unterbrochene) Pfad ebenso abgelaufen.

Aufruf: python3 sim_a_zoom_restart.py <Pfad Luminary099> [<Pfad Luminary116>]
"""
import glob, os, re, sys

def load(srcdir):
    lines = []
    for f in sorted(glob.glob(os.path.join(srcdir, "*.agc"))):
        with open(f, errors="replace") as fh:
            for n, raw in enumerate(fh, 1):
                code = raw.split("#", 1)[0].rstrip("\n")
                if not code.strip():
                    continue
                parts = re.split(r"\s+", code.strip()) if not code[0].isspace() else [""] + code.split()
                lab, rest = parts[0], parts[1:]
                lines.append(dict(file=os.path.basename(f), line=n, label=lab,
                                  op=rest[0] if rest else "", args=rest[1:], raw=raw.rstrip()))
    return lines

def index(lines):
    labels, equates = {}, {}
    for i, l in enumerate(lines):
        if l["label"]:
            if l["op"] in ("=", "EQUALS") and l["args"]:
                equates[l["label"]] = l["args"][0]
            else:
                labels.setdefault(l["label"], i)
    return labels, equates

def resolve(sym, labels, equates):
    seen = []
    while sym in equates and sym not in labels:
        seen.append(f"{sym} = {equates[sym]}")
        sym = equates[sym]
    return sym, seen

def decode_typeA(word):
    v = int(word, 8)
    return dict(T=(v >> 14) & 1, L=(v >> 13) & 1, phase=(v >> 3) & 0o177, group=v & 7)

def restart_target(lines, labels, equates, group, phase):
    spot = f"{group}.{phase}SPOT"
    i = labels[spot]
    entry = [lines[i + k] for k in range(4)]
    target = next(l["args"][0] for l in entry if l["op"].endswith("2CADR"))
    kind = "WAITLIST-Task" if any(l["op"].startswith("-2CADR") for l in entry) else "Job"
    delay = next((l["op"] + " " + " ".join(l["args"]) for l in entry if "GENADR" in l["op"] or l["op"] == "DEC"), "?")
    return spot, lines[i], target, kind, delay

def trace(lines, labels, equates, start, modreg, maxsteps=200):
    """Symbolischer Ablauf ab Label 'start'. Liefert Ereignisliste."""
    ev, sym = [], start
    real, eqs = resolve(sym, labels, equates)
    for e in eqs:
        ev.append(("EQUATE", e))
    i = labels[real]
    ev.append(("ENTRY", f"{real}  ({lines[i]['file']}:{lines[i]['line']})"))
    for _ in range(maxsteps):
        l = lines[i]; op, a = l["op"], l["args"]
        if l["label"] and l["label"] != real and i != labels[real]:
            ev.append(("LABEL", l["label"]))
        if op == "TC" and a and a[0] == "CHECKMM":
            mm = int(lines[i + 1]["args"][0])
            ev.append(("CHECKMM", f"MODREG={modreg} == {mm}? {'ja' if modreg == mm else 'nein'}"))
            i = i + 3 if modreg == mm else i + 2     # ja: Aufrufer+3, nein: Aufrufer+2
            continue
        if op == "TC" and a and a[0] in ("IBNKCALL", "BANKCALL"):
            ev.append(("CALL", lines[i + 1]["args"][0])); i += 2; continue
        if op == "TC" and a and a[0] in ("PHASCHNG", "UPFLAG", "DOWNFLAG"):
            ev.append((a[0], lines[i + 1]["args"][0] if lines[i + 1]["args"] else lines[i + 1]["op"]))
            i += 2; continue
        if op in ("DXCH", "TS", "XCH") and a and a[0] in ("AVEGEXIT", "THRUST"):
            ev.append(("WRITE", a[0]))
        if op == "WOR" and a and a[0] == "CHAN14":
            ev.append(("WRITE", "CHAN14 (Schubpulse ausgeben)"))
        if op in ("TCF", "TC") and a:
            if a[0] in ("TASKOVER", "ENDOFJOB"):
                ev.append(("END", a[0])); return ev
            tgt, eqs = resolve(a[0], labels, equates)
            if tgt in labels:
                i = labels[tgt]; ev.append(("JUMP", tgt)); continue
        i += 1
    ev.append(("ABBRUCH", "maxsteps")); return ev

def summarize(ev):
    calls = [x for k, x in ev if k == "CALL"]
    writes = [x for k, x in ev if k == "WRITE"]
    flags = [x for k, x in ev if k == "UPFLAG"]
    return dict(flatout="FLATOUT" in calls,
                guidance=("AVEGEXIT" in writes) or ("ZOOMFLAG" in flags),
                thrust_cmd=any("THRUST" in w or "CHAN14" in w for w in writes) or "FLATOUT" in calls)

def run(srcdir, name):
    lines = load(srcdir); labels, equates = index(lines)
    print(f"\n================ {name} ================")
    # Phasenwort hinter "TC 2PHSCHNG" im Task TIG-0 finden
    i = labels["TIG-0"]
    while not (lines[i]["op"] == "TC" and lines[i]["args"][:1] == ["2PHSCHNG"]):
        i += 1
    word = lines[i + 1]["args"][0]
    d = decode_typeA(word)
    print(f"TIG-0 setzt Phase: OCT {word}  ->  Gruppe {d['group']}, Phase {d['phase']}, TBASE={d['T']}"
          f"   ({lines[i+1]['file']}:{lines[i+1]['line']})")
    # geplanter Task im Normalablauf
    j = labels["TIG-0"]
    while lines[j]["op"] != "2CADR":
        j += 1
    normal = lines[j]["args"][0]
    print(f"Normalablauf: WAITLIST plant Task '{normal}'   ({lines[j]['file']}:{lines[j]['line']})")
    spot, l, tgt, kind, delay = restart_target(lines, labels, equates, d["group"], d["phase"])
    print(f"Neustart:     {spot} -> {kind} '{tgt}', Verzoegerung {delay}   ({l['file']}:{l['line']})")
    res = {}
    for label, start in (("NORMAL", normal), ("NEUSTART", tgt)):
        ev = trace(lines, labels, equates, start, modreg=63)
        print(f"\n  Ablauf {label}:")
        for k, x in ev:
            print(f"    {k:9s} {x}")
        res[label] = summarize(ev)
    print("\n  Ergebnis            Normal   Neustart")
    for key, txt in (("flatout", "FLATOUT (Vollschub)"), ("guidance", "Landelenkung aktiv"),
                     ("thrust_cmd", "Schubbefehl ausgegeben")):
        print(f"  {txt:22s} {'ja' if res['NORMAL'][key] else 'NEIN':7s}  {'ja' if res['NEUSTART'][key] else 'NEIN'}")
    return res

def timeline(res, name, zoomtime=26.0):
    print(f"\n  Zeitachse {name} (Neustart t s nach TIG, Throttle-up bei {zoomtime:.0f} s):")
    for t in (2, 10, 20, 25.9, 26.1, 60):
        if t >= zoomtime:
            out = "kein Effekt (Zoom schon erfolgt, Gruppe 3 bereits aus)"
        else:
            r = res["NEUSTART"]
            out = ("Throttle-up + Lenkung OK" if r["flatout"] and r["guidance"]
                   else "Triebwerk bleibt bei ~10 %, keine Landelenkung")
        print(f"    t = {t:5.1f} s  ->  {out}")

if __name__ == "__main__":
    for p in sys.argv[1:]:
        name = os.path.basename(os.path.normpath(p))
        r = run(p, name)
        timeline(r, name)
