"""
Pipeline audit — hunt for SILENT failures.

Written 2026-08-09 after three format-drift bugs in two days, two of which were
completely invisible:
  - CLV%       : 0 of 535 rows ever written, no error anywhere
  - DK Juice   : 108 rows rendering -113 as "-11300.00%"
  - Edges Z1   : stale duplicate header shadowing the real timestamp column

The common thread is that nothing threw. A blank column looks like "no data yet",
a bad format looks like a value, and a swallowed exception looks like success. This
checks the things that fail QUIETLY.

    python pipeline_audit.py

Every finding is prefixed FAIL / WARN / INFO. FAIL means something is provably not
doing its job. Read those first.
"""
import os, sys, math
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gspread
from datetime import datetime, timedelta
from collections import Counter, defaultdict
from google.oauth2.service_account import Credentials

import analyze_edges as AE

SCOPES = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = Credentials.from_service_account_file('google_credentials.json', scopes=SCOPES)
gc = gspread.authorize(creds)
sh = gc.open_by_key(AE.ODDS_SHEET_ID)

TODAY = datetime.now().strftime("%Y-%m-%d")
YEST  = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
findings = []
def add(level, area, msg):
    findings.append((level, area, msg))
    print(f"  [{level}] {area}: {msg}")

def ascii_(s): return ''.join(c if ord(c) < 128 else '*' for c in str(s))

print("=" * 84)
print(f"PIPELINE AUDIT — {datetime.now():%Y-%m-%d %H:%M}")
print("=" * 84)

# ── load every tab once, both render modes ────────────────────────────────
TABS = {}
for w in sh.worksheets():
    try:
        d = w.get_all_values()
        u = w.get_all_values(value_render_option='UNFORMATTED_VALUE')
        TABS[w.title] = (d, u)
    except Exception as e:
        add("FAIL", w.title, f"could not read tab: {e}")

# ── 1. header integrity ───────────────────────────────────────────────────
print("\n" + "-" * 84)
print("1. HEADER INTEGRITY  (duplicates shadow real columns and break lookups)")
print("-" * 84)
EXPECT = {
    "Bet History": AE.HISTORY_HEADER,
    "Edges":       AE.EDGES_HEADER,
    "Team Totals": getattr(AE, "TEAM_TOTAL_HEADER", None),
    "Line History": getattr(AE, "LINE_HISTORY_HEADER", None),
}
for tab, (d, u) in TABS.items():
    if not d or not d[0]:
        add("WARN", tab, "empty or missing header row"); continue
    hdr = d[0]
    dupes = {k: v for k, v in Counter(h for h in hdr if str(h).strip()).items() if v > 1}
    if dupes:
        add("FAIL", tab, f"duplicate header names {dupes} — dict lookups silently take the LAST one")
    exp = EXPECT.get(tab)
    if exp:
        if len(hdr) > len(exp) and any(str(c).strip() for c in hdr[len(exp):]):
            add("FAIL", tab, f"extra header cells beyond code constant: {hdr[len(exp):]!r}")
        for i, (a, b) in enumerate(zip(exp, hdr)):
            if a != b:
                add("FAIL", tab, f"header idx {i} mismatch: code={a!r} sheet={b!r}")
                break
if not any(f[0] == "FAIL" for f in findings):
    print("  (no header problems)")

# ── 2. format drift ───────────────────────────────────────────────────────
print("\n" + "-" * 84)
print("2. FORMAT DRIFT  (value correct, DISPLAY wrong — this is what bit CLV/DK Juice)")
print("-" * 84)
drift_found = False
for tab, (d, u) in TABS.items():
    if len(d) < 2 or not d[0]: continue
    hdr = d[0]
    for ci_, name in enumerate(hdr):
        if not str(name).strip(): continue
        bad = 0; sample = None
        for dr, ur in zip(d[1:], u[1:]):
            if len(dr) <= ci_ or len(ur) <= ci_: continue
            disp, raw = str(dr[ci_]).strip(), ur[ci_]
            if not disp.endswith("%"): continue
            if not isinstance(raw, (int, float)): continue
            # a legitimate percent cell stores a FRACTION (0.585 -> "58.5%").
            # storing 14.25 or -113 and showing "%" means the format drifted.
            if abs(raw) > 2:
                bad += 1
                if sample is None: sample = (disp, raw)
        if bad:
            drift_found = True
            add("FAIL", f"{tab}.{ascii_(name)}",
                f"{bad} cells percent-formatted over a non-fraction "
                f"(e.g. displays {sample[0]!r} but stores {sample[1]!r})")
if not drift_found:
    print("  (no format drift detected)")

# ── 3. columns that never populate ────────────────────────────────────────
print("\n" + "-" * 84)
print("3. DEAD COLUMNS  (a header with no data = a feature that never ran)")
print("-" * 84)
dead_found = False
for tab, (d, u) in TABS.items():
    if len(d) < 6 or not d[0]: continue
    hdr = d[0]; n = len(d) - 1
    for ci_, name in enumerate(hdr):
        if not str(name).strip(): continue
        filled = sum(1 for r in d[1:] if len(r) > ci_ and str(r[ci_]).strip())
        if filled == 0:
            dead_found = True
            add("FAIL", f"{tab}.{ascii_(name)}", f"0 of {n} rows populated — never written")
        elif filled < n * 0.02 and n > 100:
            add("WARN", f"{tab}.{ascii_(name)}", f"only {filled} of {n} rows populated ({filled/n*100:.1f}%)")
if not dead_found:
    print("  (no fully dead columns)")

# ── 4. grading gaps ───────────────────────────────────────────────────────
print("\n" + "-" * 84)
print("4. GRADING GAPS  (settled games still marked Pending = grader missed them)")
print("-" * 84)
cutoff = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
for tab in ("Bet History", "Team Totals", "Game Totals", "ML RL", "Player Props Shadow"):
    if tab not in TABS: continue
    d, _ = TABS[tab]
    if len(d) < 2: continue
    hdr = d[0]
    if "Result" not in hdr: continue
    ri = hdr.index("Result")
    stale = [r for r in d[1:] if r and r[0].strip() and r[0].strip() < cutoff
             and (len(r) <= ri or str(r[ri]).strip() in ("", "Pending"))]
    if stale:
        dates = sorted({r[0].strip() for r in stale})
        add("FAIL", tab, f"{len(stale)} rows older than {cutoff} still ungraded "
                         f"(dates {dates[0]}..{dates[-1]})")
    else:
        print(f"  [ OK ] {tab}: no stale ungraded rows")

# ── 5. freshness ──────────────────────────────────────────────────────────
print("\n" + "-" * 84)
print("5. FRESHNESS  (is each tab still being written?)")
print("-" * 84)
for tab, (d, u) in TABS.items():
    if len(d) < 2: continue
    dates = [r[0].strip() for r in d[1:] if r and len(r) > 0 and str(r[0]).strip()[:2] == "20"]
    if not dates: continue
    newest = max(dates)
    age = (datetime.now() - datetime.strptime(newest, "%Y-%m-%d")).days if len(newest) == 10 else None
    if age is None: continue
    if age >= 3:
        add("FAIL", tab, f"newest row is {newest} ({age} days old) — writer may be broken")
    elif age == 2:
        add("WARN", tab, f"newest row is {newest} ({age} days old)")
    else:
        print(f"  [ OK ] {tab}: newest {newest}")

# ── 6. value sanity ───────────────────────────────────────────────────────
print("\n" + "-" * 84)
print("6. VALUE SANITY  (impossible numbers that no exception would catch)")
print("-" * 84)
sane = True
if "Bet History" in TABS:
    d, u = TABS["Bet History"]
    hdr = d[0]; H = {h: i for i, h in enumerate(hdr)}
    def col(r, k):
        i = H.get(k); return r[i] if i is not None and len(r) > i else ""
    bad_conf = bad_juice = bad_units = 0
    for r in d[1:]:
        if not r or not str(r[0]).strip(): continue
        c = str(col(r, "Confidence %")).replace("%", "").strip()
        if c:
            try:
                cv = float(c)
                if not (0 <= cv <= 100): bad_conf += 1
            except ValueError: pass
        for jk in ("Book Juice", "DK Juice"):
            j = str(col(r, jk)).strip()
            if j:
                v = AE.american_to_implied.__module__ and None
                try:
                    jv = float(j.replace("%", "").replace("+", ""))
                    if j.endswith("%") or 0 < abs(jv) < 100 or abs(jv) > 5000: bad_juice += 1
                except ValueError: bad_juice += 1
        uu = str(col(r, "Units Bet")).strip()
        if uu:
            try:
                if not (0 < float(uu) <= 2.0): bad_units += 1
            except ValueError: bad_units += 1
    for label, cnt, why in (("Confidence %", bad_conf, "outside 0-100"),
                            ("juice columns", bad_juice, "not plausible American odds"),
                            ("Units Bet", bad_units, "outside (0, 2.0]")):
        if cnt:
            sane = False
            add("FAIL", f"Bet History.{label}", f"{cnt} values {why}")
if sane:
    print("  (no impossible values found)")

# ── 7. cross-check: does the cheatsheet match what analyze wrote? ─────────
print("\n" + "-" * 84)
print("7. CROSS-TAB CONSISTENCY")
print("-" * 84)
if "Bet History" in TABS:
    d, _ = TABS["Bet History"]
    H = {h: i for i, h in enumerate(d[0])}
    todays = [r for r in d[1:] if r and str(r[0]).strip() == TODAY]
    yests  = [r for r in d[1:] if r and str(r[0]).strip() == YEST]
    print(f"  Bet History rows: today {len(todays)}, yesterday {len(yests)}")
    if not todays and datetime.now().hour >= 10:
        add("WARN", "Bet History", f"no rows for {TODAY} despite it being past 10am")
    bt = Counter(str(r[H["Bet Type"]]).strip() for r in todays if len(r) > H["Bet Type"])
    print(f"  today's mix: {dict(bt)}")
    if "Game Total" in bt:
        gu = {str(r[H["Units Bet"]]).strip() for r in todays
              if len(r) > H["Units Bet"] and str(r[H["Bet Type"]]).strip() == "Game Total"}
        if gu and gu != {str(AE.GT_FLAT_UNITS)}:
            add("WARN", "Bet History", f"GT should be flat {AE.GT_FLAT_UNITS}u, found stakes {gu}")

# ── 8. Line History integrity ────────────────────────────────────────────
print("\n" + "-" * 84)
print("8. LINE HISTORY  (the market-edge dataset — must not have gaps)")
print("-" * 84)
if "Line History" in TABS:
    d, _ = TABS["Line History"]
    if len(d) > 1:
        L = {h: i for i, h in enumerate(d[0])}
        runs = defaultdict(set)
        for r in d[1:]:
            if len(r) <= L["Run At"]: continue
            runs[str(r[L["Run At"]]).strip()[:10]].add(str(r[L["Run At"]]).strip())
        for day in sorted(runs)[-4:]:
            n = len(runs[day])
            flag = "" if n >= 3 else "   <-- expected 3 (8:00/12:30/6:30)"
            print(f"  {day}: {n} snapshot(s){flag}")
            if n < 3 and day < TODAY:
                add("WARN", "Line History", f"{day} has only {n} snapshots, expected 3")
        books = Counter(str(r[L["Book"]]).strip() for r in d[1:] if len(r) > L["Book"])
        print(f"  books captured: {dict(books)}")
        if len(books) < 4:
            add("WARN", "Line History", f"only {len(books)} books captured, expected 4")
else:
    add("FAIL", "Line History", "tab missing entirely")

# ── summary ──────────────────────────────────────────────────────────────
print("\n" + "=" * 84)
print("SUMMARY")
print("=" * 84)
fails = [f for f in findings if f[0] == "FAIL"]
warns = [f for f in findings if f[0] == "WARN"]
print(f"\n  FAIL: {len(fails)}    WARN: {len(warns)}")
if fails:
    print("\n  Must fix:")
    for _, a, m in fails: print(f"    - {a}: {m}")
if warns:
    print("\n  Worth a look:")
    for _, a, m in warns: print(f"    - {a}: {m}")
if not fails and not warns:
    print("\n  Clean.")
