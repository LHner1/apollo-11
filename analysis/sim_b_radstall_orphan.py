#!/usr/bin/env python3
"""
Simulation B: verwaister P20-Job durch NORDSTAL (Luminary 099, P20-P25.agc).

Modell des AGC-Executive nach EXECUTIVE.agc:
  - 7 Core Sets (NO.CORES), 5 VAC-Bereiche (VAC1USE..VAC5USE)
  - FINDVAC: belegt Core Set + VAC, sonst Alarm 1201 (keine VAC) / 1202 (kein Core Set)
  - NOVAC:   belegt nur Core Set, sonst 1202
  - JOBSLEEP: Job behaelt Core Set und VAC (Prioritaet nur negiert, JOBSLP1)
  - JOBWAKE(cadr): weckt den ERSTEN Core Set (Scan ab 0) mit passender LOC (WAKETEST)
  - ENDOFJOB: gibt Core Set und VAC frei
  - Neustart (BAILOUT -> GOPROG): alle Core Sets und VACs frei (STARTSB2)

Radar-Ablauf nach P20-P25.agc (Luminary 099):
  - P20-Thread ruft RRDESSM; erster Durchlauf (RADCADR=0) -> STARTDES -> Rueckkehr
    -> RADSTALL: schlaeft, RADCADR := Weckadresse                         (Z. 892-913)
  - DESLOOP-Task alle 0,5 s: FINDVAC DODES (PRIO26)                         (Z. 2438-2473)
  - DODES: jeder 4. Durchlauf -> KILLTASK DESLOOP, BANKCALL R21LEM2       (Z. 2697-2712)
      LOS im Bereich:      STARTDES, DESRETRN: RADCADR != 0 -> ENDOFJOB    (Z. 2270-2273)
      LOS ausserhalb:      NORDSTAL: RADCADR := 0, DODES-Job laeuft als
                           neuer P20-Thread weiter (P20LEMA -> R61LEM ->
                           erneut R21 -> RRDESSM -> RADSTALL)               (Z. 2280-2299)
  - Lock-on: RGOODEND -> GOODEND -> JOBWAKE(RADCADR)                       (Z. 3133-3135)

Hintergrundlast (Annahme, parametrisierbar): kurzlebige Jobs, z. B. Anzeige- und
Extended-Verb-Jobs, die zeitweise gleichzeitig mit DODES laufen.
"""
import random, sys

RADSTALL_CADR = "P20-P25.agc:913 (RADSTALL)"

class Overflow(Exception):
    pass

class Executive:
    def __init__(self, log):
        self.core = [None] * 7     # None = frei, sonst dict(job)
        self.vac = [None] * 5
        self.log = log
        self.alarms = []

    def _core(self):
        for i, c in enumerate(self.core):
            if c is None:
                return i
        raise Overflow("1202")

    def _vac(self):
        for i, v in enumerate(self.vac):
            if v is None:
                return i
        raise Overflow("1201")

    def findvac(self, name, t):
        v = self._vac()                      # FINDVAC2 prueft VACs zuerst
        c = self._core()                     # danach NOVAC2: Core Set
        job = dict(name=name, core=c, vac=v, sleeping=False, loc=None, born=t)
        self.core[c] = job; self.vac[v] = job
        return job

    def novac(self, name, t):
        c = self._core()
        job = dict(name=name, core=c, vac=None, sleeping=False, loc=None, born=t)
        self.core[c] = job
        return job

    def sleep(self, job, loc):
        job["sleeping"] = True; job["loc"] = loc

    def wake(self, loc):
        for c in self.core:                  # Scan ab Core Set 0 (JOBWAKE2/WAKETEST)
            if c and c["sleeping"] and c["loc"] == loc:
                c["sleeping"] = False; c["loc"] = None
                return c
        return None                          # "EXIT IF SLEEPING JOB NOT FOUND"

    def end(self, job):
        self.core[job["core"]] = None
        if job["vac"] is not None:
            self.vac[job["vac"]] = None

    def restart(self):
        self.core = [None] * 7; self.vac = [None] * 5

    def usage(self):
        return sum(c is not None for c in self.core), sum(v is not None for v in self.vac)

    def orphans(self, active_ids):
        return [c for c in self.core if c and c["sleeping"] and id(c) not in active_ids]


def simulate(out_of_limits_times, background_vac_peak, duration=600.0, seed=1, verbose=True):
    rnd = random.Random(seed)
    lines = []
    log = lines.append
    ex = Executive(log)
    radcadr = 0
    # Start P20 (V37 -> FINDVAC), erster Designate-Durchlauf, schlaeft in RADSTALL
    p20 = ex.findvac("P20", 0.0)
    ex.sleep(p20, RADSTALL_CADR); radcadr = RADSTALL_CADR
    current = p20                        # der Job, den die Software fuer "den" P20-Thread haelt
    dodes_pass = 0
    events = sorted(out_of_limits_times)
    t, step = 0.0, 0.5
    background = []
    maxuse = (0, 0)
    try:
        while t < duration:
            t = round(t + step, 2)
            # Hintergrundlast: kurzlebige VAC-Jobs (Anzeige, Extended Verbs ...)
            for j in list(background):
                if j["until"] <= t:
                    ex.end(j); background.remove(j)
            want = rnd.randint(0, background_vac_peak)
            while len(background) < want:
                j = ex.findvac("Hintergrund", t); j["until"] = t + rnd.choice((0.5, 1.0, 1.5))
                background.append(j)
            # DESLOOP -> FINDVAC DODES
            dodes = ex.findvac("DODES", t)
            dodes_pass += 1
            used = ex.usage(); maxuse = (max(maxuse[0], used[0]), max(maxuse[1], used[1]))
            if dodes_pass % 4 == 0:              # LOSCOUNT abgelaufen -> R21LEM2
                if events and events[0] <= t:    # LOS ausserhalb beider RR-Modi
                    events.pop(0)
                    radcadr = 0                  # NORDSTAL: "WIPE OUT ANYONE WAITING"
                    log(f"t={t:6.1f}s  LOS ausserhalb -> NORDSTAL: RADCADR=0; "
                        f"'{current['name']}' (Core {current['core']}, VAC {current['vac']}) schlaeft weiter")
                    dodes["name"] = "P20 (aus DODES)"
                    current = dodes              # DODES-Job wird neuer P20-Thread
                    # P20LEMA -> R61LEM -> R21 -> RRDESSM (RADCADR=0: erster Durchlauf) -> RADSTALL
                    ex.sleep(current, RADSTALL_CADR); radcadr = RADSTALL_CADR
                    n_orph = len(ex.orphans({id(current)}))
                    cu, vu = ex.usage()
                    log(f"           belegt: {cu}/7 Core Sets, {vu}/5 VAC, verwaiste Jobs: {n_orph}")
                    continue
                # LOS im Bereich: DESRETRN, RADCADR != 0 -> ENDOFJOB
            ex.end(dodes)
        log(f"t={t:6.1f}s  Ende, kein Ueberlauf")
        return dict(alarm=None, t=None, lines=lines, maxuse=maxuse)
    except Overflow as e:
        cu, vu = ex.usage()
        log(f"t={t:6.1f}s  ALARM {e} (BAILOUT) -> Software-Neustart; vorher belegt {cu}/7 Core, {vu}/5 VAC")
        return dict(alarm=str(e), t=t, lines=lines, maxuse=maxuse)


def wake_order_demo():
    """Zeigt, welcher Job beim Lock-on geweckt wird, wenn zwei in RADSTALL schlafen."""
    ex = Executive(print)
    old = ex.findvac("P20 (original, verwaist)", 0)
    ex.sleep(old, RADSTALL_CADR)
    new = ex.findvac("P20 (aus DODES)", 2)
    ex.sleep(new, RADSTALL_CADR)
    woken = ex.wake(RADSTALL_CADR)
    still = [c["name"] for c in ex.core if c and c["sleeping"]]
    return woken["name"], woken["core"], still


if __name__ == "__main__":
    print("== Szenario 1: P20 im Flug, LOS faellt 3x aus dem Radarbereich (t=60, 180, 300 s),")
    print("   Hintergrundlast bis 1 gleichzeitiger VAC-Job\n")
    r = simulate([60, 180, 300], background_vac_peak=1)
    print("\n".join(r["lines"]))

    print("\n== Kontrollfall: dasselbe ohne Ausfall der Sichtlinie\n")
    r = simulate([], background_vac_peak=1)
    print("\n".join(r["lines"]), f"(max. Belegung: {r['maxuse'][0]}/7 Core, {r['maxuse'][1]}/5 VAC)")

    print("\n== Parametervariation: Anzahl Sichtlinien-Ausfaelle bis zum Alarm")
    print("   (je 50 Zufallslaeufe, Ausfaelle alle 60 s)\n")
    print("   Hintergrund-VAC-Spitze | Ausfaelle bis Alarm (min / median / max) | Alarmtyp")
    for bg in (0, 1, 2, 3):
        counts, kinds = [], set()
        for seed in range(50):
            times = [60 * (k + 1) for k in range(20)]
            r = simulate(times, background_vac_peak=bg, duration=1300, seed=seed)
            if r["alarm"]:
                n = sum(1 for l in r["lines"] if "NORDSTAL" in l)
                counts.append(n); kinds.add(r["alarm"])
        counts.sort()
        if counts:
            print(f"   {bg:23d} | {counts[0]:3d} / {counts[len(counts)//2]:3d} / {counts[-1]:3d}"
                  f"{'':24s} | {', '.join(sorted(kinds))}")
        else:
            print(f"   {bg:23d} | kein Alarm in 20 Ausfaellen")

    print("\n== Weck-Reihenfolge, wenn zwei P20-Jobs in RADSTALL schlafen (JOBWAKE scannt ab Core 0)")
    name, core, still = wake_order_demo()
    print(f"   Lock-on weckt: '{name}' (Core Set {core}); weiter schlafend: {still}")
