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
import os, sys, math, time
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
# Skip our own output tab — its row 1 is a status line, not column headers, so
# auditing it produces self-referential nonsense like "Pipeline Health.last checked
# 2026-08-10 17:41: 0 of 25 rows populated".
SKIP_TABS = {"Pipeline Health"}

# Every tab is read TWICE (display + unformatted), and Google allows 60 reads per
# minute per user. Line History alone is past 9,000 rows and growing by ~1,800/day, so
# a 429 is a matter of time — and a rate limit reported as "could not read tab" would
# look exactly like a broken pipeline. Retry with backoff instead of crying wolf.
def _read_tab(w, tries=4):
    for attempt in range(tries):
        try:
            return w.get_all_values(), w.get_all_values(value_render_option='UNFORMATTED_VALUE')
        except Exception as e:
            if "429" not in str(e) and "Quota exceeded" not in str(e):
                raise
            if attempt == tries - 1:
                raise
            time.sleep(20 * (attempt + 1))   # 20s, 40s, 60s
    return None, None

TABS = {}
for w in sh.worksheets():
    if w.title in SKIP_TABS:
        continue
    try:
        d, u = _read_tab(w)
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
# Whether a %-displaying column stores POINTS (5.53 -> "5.53%") or a FRACTION
# (0.0553 -> "5.53%") is a design choice, not something inferable from the cell — both
# render identically. So state it, and check that the DISPLAY matches the STORED value
# under the stated convention. That turns a guess into a real test:
#   points   : display must equal stored          ("3.20%" over 3.2   = OK)
#   fraction : display must equal stored x 100    ("58.5%" over 0.585 = OK)
# The old rule just flagged any %-display over a value >2, which called correct
# columns broken (Close CLV%) and would have kept doing so forever.
# Determined empirically 2026-08-10 by comparing display against stored across every
# %-rendering column, not by assumption — the first guess had Team Totals.Edge % as
# points when 607 of its cells store fractions.
POINTS_COLUMNS = {
    # Written as a NUMBER (1.59) behind a literal-% pattern, deliberately, so an
    # UNFORMATTED_VALUE read returns 1.59 points rather than 0.0159.
    ("Bet History", "Close CLV%"),
}
# Player Props Shadow "Edge %" was wrongly listed here on 2026-08-10 and the mistake
# was costly. analyze_edges writes it as a STRING -- f"{round(edge_pct,2)}%" -- so
# Sheets parses "40.93%" into 0.4093 with a PERCENT format. That is correct and always
# was. What triggered the false alarm were cells storing 3.2 and showing "320.00%":
# real 320% edges on longshot HR props, which are shadow-only and uncapped, not drift.
# Pinning a literal-% pattern over them then rewrote ~6,700 rows to read "0.41%" when
# the edge was 41.03%. Values were never touched; only the display was broken.
# Everything else (Confidence %, Edge %, Our Projection, Team Totals Edge %) uses
# Sheets' PERCENT type and stores a fraction: 0.946 -> "94.60%".

drift_found = False
for tab, (d, u) in TABS.items():
    if len(d) < 2 or not d[0]: continue
    hdr = d[0]
    for ci_, name in enumerate(hdr):
        if not str(name).strip(): continue
        stores_points = (tab, str(name).strip()) in POINTS_COLUMNS
        bad = 0; sample = None
        for dr, ur in zip(d[1:], u[1:]):
            if len(dr) <= ci_ or len(ur) <= ci_: continue
            disp, raw = str(dr[ci_]).strip(), ur[ci_]
            if not disp.endswith("%"): continue
            if not isinstance(raw, (int, float)): continue
            try:
                shown = float(disp[:-1].replace("+", "").replace(",", ""))
            except ValueError:
                continue
            expected = raw if stores_points else raw * 100.0
            # Display and stored must agree under the column's stated convention.
            # A mismatch means the cell's format contradicts what the writer put in it.
            if abs(shown - expected) > max(0.01, abs(expected) * 0.001):
                bad += 1
                if sample is None: sample = (disp, raw, expected)
        if bad:
            drift_found = True
            kind = "points" if stores_points else "fraction"
            add("FAIL", f"{tab}.{ascii_(name)}",
                f"{bad} cells whose display contradicts the stored value "
                f"(declared {kind}: displays {sample[0]!r} over {sample[1]!r}, "
                f"expected to show {sample[2]:g})")
if not drift_found:
    print("  (no format drift detected)")

# ── 3. columns that never populate ────────────────────────────────────────
print("\n" + "-" * 84)
print("3. DEAD COLUMNS  (a header with no data = a feature that never ran)")
print("-" * 84)
dead_found = False
for tab, (d, u) in TABS.items():
    if len(d) < 6 or not d[0]: continue
    hdr = d[0]
    # Count DATA rows, not allocated ones. Park Factor Data holds 31 venues in 1,775
    # allocated rows, so measuring fill against len(d) called every populated column
    # "1.7% filled" — nine warnings for a tab that is entirely fine.
    body = [r for r in d[1:] if r and any(str(c).strip() for c in r)]
    n = len(body)
    if n < 5: continue
    for ci_, name in enumerate(hdr):
        if not str(name).strip(): continue
        filled = sum(1 for r in body if len(r) > ci_ and str(r[ci_]).strip())
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
# Measure the ungraded RATE on each SETTLED date, not a cumulative count.
# The first version compared a running total against a fixed baseline (175 props).
# That count grows every day — roughly 4% of prop volume is players who get scratched
# and never appear in a box score, whose bets void in reality and can never grade. So
# the baseline was guaranteed to breach eventually and then stay red permanently,
# which is exactly how an alarm becomes wallpaper.
# A rate per settled date answers the real question: is grading working RIGHT NOW.
MAX_UNGRADED_RATE = {
    "Bet History":         0.05,   # real bets: should be ~0
    "Team Totals":         0.05,
    "Game Totals":         0.05,
    "ML RL":               0.05,
    "Player Props Shadow": 0.15,   # DNP/scratched players never grade; ~4% is normal
}
settled_hi = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
settled_lo = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
for tab in ("Bet History", "Team Totals", "Game Totals", "ML RL", "Player Props Shadow"):
    if tab not in TABS: continue
    d, _ = TABS[tab]
    if len(d) < 2 or "Result" not in d[0]: continue
    ri = d[0].index("Result")
    per_date = defaultdict(lambda: [0, 0])
    for r in d[1:]:
        if not r or not str(r[0]).strip(): continue
        dt = str(r[0]).strip()
        if not (settled_lo <= dt <= settled_hi): continue
        per_date[dt][0] += 1
        if len(r) <= ri or str(r[ri]).strip() in ("", "Pending"):
            per_date[dt][1] += 1
    if not per_date:
        continue
    limit = MAX_UNGRADED_RATE.get(tab, 0.05)
    # A rate needs a denominator to mean anything. 2-of-2 ungraded on a thin date is
    # not evidence of systematic failure — and on 2026-08-06 those two Game Total rows
    # are --force phantoms for a matchup that never happened on that date (TOR @ PHI,
    # when Toronto played the Cubs), so they can NEVER grade and would flag forever.
    MIN_DATE_ROWS = 5
    sized = {k: v for k, v in per_date.items() if v[0] >= MIN_DATE_ROWS}
    if not sized:
        print(f"  [ OK ] {tab}: no settled date with >= {MIN_DATE_ROWS} rows to judge")
        continue
    worst = max(sized.items(), key=lambda kv: kv[1][1] / max(kv[1][0], 1))
    dt, (n, ung) = worst
    rate = ung / max(n, 1)
    total_ung = sum(v[1] for v in sized.values())
    total_n   = sum(v[0] for v in sized.values())
    if rate > limit:
        add("FAIL", tab,
            f"{ung} of {n} rows ungraded on {dt} ({rate*100:.0f}%) — over the "
            f"{limit*100:.0f}% tolerance, so grading may be failing")
    else:
        print(f"  [ OK ] {tab}: {total_ung}/{total_n} ungraded across settled dates "
              f"({total_ung/max(total_n,1)*100:.1f}%), worst day {rate*100:.0f}%")

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

# ── 9. API credit burn ───────────────────────────────────────────────────────
# Snapshot density went to every 30 minutes on 2026-08-10, taking projected usage to
# ~72% of the 20,000/month plan once NFL is running. That is deliberate — capturing
# the closing line matters more than the credits — but it leaves less room for error
# than before, so the burn needs to be visible rather than discovered when writes
# start failing. The plan resets on the 1st, so pace is measured against day-of-month.
print("\n" + "-" * 84)
CREDIT_INFO = {}
print("9. API CREDIT BURN")
print("-" * 84)
try:
    import requests
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    _key = os.environ.get("ODDS_API_KEY", "")
    if _key:
        # /v4/sports does not count against the quota
        _r = requests.get("https://api.the-odds-api.com/v4/sports",
                          params={"apiKey": _key}, timeout=20)
        used = int(_r.headers.get("x-requests-used", 0))
        rem  = float(_r.headers.get("x-requests-remaining", 0))
        plan = used + int(rem)
        day  = datetime.now().day
        days_in_month = 31
        pace = used / max(day, 1) * days_in_month
        CREDIT_INFO.update(used=used, plan=plan, pace=pace, day=day)
        print(f"  used {used:,} of {plan:,} this period ({used/max(plan,1)*100:.1f}%)")
        print(f"  day {day} of the month — on pace for {pace:,.0f}")
        if pace > plan:
            add("FAIL", "API credits",
                f"on pace for {pace:,.0f} against a {plan:,} plan — will run out "
                f"before month end (used {used:,} by day {day})")
        elif pace > plan * 0.85:
            add("WARN", "API credits",
                f"on pace for {pace:,.0f} of {plan:,} ({pace/plan*100:.0f}%) — tight")
        else:
            print(f"  headroom fine ({pace/plan*100:.0f}% of plan at this pace)")
    else:
        print("  (ODDS_API_KEY not set — skipped)")
except Exception as e:
    # Loud on purpose. This block swallowed a NameError on 2026-08-10 and printed
    # nothing useful, hiding a broken credit check inside the very tool meant to
    # surface hidden breakage.
    print(f"  [FAILED] credit check did not run: {type(e).__name__}: {e}")
    add("WARN", "API credits", f"credit check could not run: {type(e).__name__}: {e}")


# ── known and accepted ───────────────────────────────────────────────────────
# Findings that are real but permanent, triaged 2026-08-10. Without this the tab
# reads NEEDS ATTENTION every single day for things nobody is going to act on, and
# a status that is always red is one you stop reading — the same cry-wolf failure
# that made the old format-drift rule useless.
# To un-accept something, delete its line. Anything NOT listed here counts.
ACCEPTED = {
    "Park Factor Data":
        "duplicate Venue header; nothing reads this tab programmatically",
    "MLB Odds.last_updated":
        "the odds API reports this per bookmaker, not at the level we capture",
    "ML RL.Confidence":
        "label column never written; the Confidence % beside it is populated",
    "Bet History.juice columns":
        "10 legacy June values from before DK Juice was wired",
    "Game Totals.Closing Line":
        "belongs to fetch_closing_lines.py, superseded by the six daily snapshots",
    "Game Totals.CLV":
        "same as Closing Line — superseded",
    "Bet History.Snapshots":
        "only populated from 2026-08-10; fills in going forward",
}

# Whole tabs nothing in the pipeline reads programmatically. Park Factor Data is a
# human-maintained reference sheet whose rows are not truly blank, so the sparse
# -column check keeps firing on it. Not worth tuning a heuristic for a tab no code
# depends on — better to say so plainly than leave nine warnings nobody will act on.
ACCEPTED_TABS = {"Park Factor Data"}

def _accepted(area, msg):
    if area in ACCEPTED:
        return True
    if area.split(".")[0] in ACCEPTED_TABS:
        return True
    return False

# ── summary ──────────────────────────────────────────────────────────────
print("\n" + "=" * 84)
print("SUMMARY")
print("=" * 84)
known = [f for f in findings if _accepted(f[1], f[2])]
findings = [f for f in findings if not _accepted(f[1], f[2])]
fails = [f for f in findings if f[0] == "FAIL"]
warns = [f for f in findings if f[0] == "WARN"]
if known:
    print(f"\n  ({len(known)} known/accepted finding(s) suppressed — see ACCEPTED in "
          f"pipeline_audit.py)")
print(f"\n  FAIL: {len(fails)}    WARN: {len(warns)}")
if fails:
    print("\n  Must fix:")
    for _, a, m in fails: print(f"    - {a}: {m}")
if warns:
    print("\n  Worth a look:")
    for _, a, m in warns: print(f"    - {a}: {m}")
if not fails and not warns:
    print("\n  Clean.")

# ── publish to the workbook ──────────────────────────────────────────────────
# The findings above go to the GitHub Actions log, which the owner does not read and
# has said they cannot check. Monitoring nobody sees is not monitoring, so the same
# report is written to a 'Pipeline Health' tab in the workbook they already open
# daily. Row 1 is a single status line: if it does not say OK, something needs
# looking at, and the timestamp tells you whether the check itself is still running.
#
# A STALE TIMESTAMP IS ITSELF THE ALARM. This step runs with if:always() in the
# workflow, so it reports even when an earlier step crashed. If the date in row 1 is
# not today, the morning routine did not finish — which no other signal would show,
# because a pipeline that dies just leaves yesterday's numbers sitting there looking
# perfectly normal.
HEALTH_TAB = "Pipeline Health"
try:
    try:
        wsh = sh.worksheet(HEALTH_TAB)
    except Exception:
        wsh = sh.add_worksheet(title=HEALTH_TAB, rows=200, cols=6)

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    if fails:
        status = f"NEEDS ATTENTION — {len(fails)} issue(s)"
    elif warns:
        status = f"OK (with {len(warns)} note(s))"
    else:
        status = "OK — everything checks out"

    rows = [
        ["STATUS", status, "", f"last checked {stamp}", "", ""],
    ]
    # Credit burn shown on the tab, not just in the log. The owner asked to verify
    # these against the-odds-api.com dashboard rather than trust a model — a good
    # instinct, since on 2026-08-10 the API header read 1,991 while the dashboard
    # showed 1,911 and neither of us could say which was right. Prompt lands on
    # Mondays so the check actually recurs instead of depending on remembering.
    if CREDIT_INFO:
        ci_ = CREDIT_INFO
        rows.append(["CREDITS",
                     f"{ci_['used']:,} used of {ci_['plan']:,} "
                     f"({ci_['used']/max(ci_['plan'],1)*100:.0f}%)",
                     "", f"on pace for {ci_['pace']:,.0f} by month end", "", ""])
        if datetime.now().weekday() == 0:      # Monday
            rows.append(["CHECK ME",
                         "Weekly: log in to the-odds-api.com and compare 'used' "
                         "against the number above.",
                         "", "Tell Claude if they disagree.", "", ""])
    rows += [
        ["", "", "", "", "", ""],
        ["Level", "Area", "What is wrong", "", "", ""],
    ]
    for lvl, area, msg in fails + warns:
        rows.append([lvl, ascii_(area), ascii_(msg), "", "", ""])
    if not fails and not warns:
        rows.append(["OK", "-", "Nothing new. Everything checked out.", "", "", ""])
    rows.append(["", "", "", "", "", ""])
    rows.append(["", "FAIL = provably not doing its job. WARN = worth a look, not urgent.",
                 "", "", "", ""])
    rows.append(["", "If 'last checked' is not today, the morning run did not finish.",
                 "", "", "", ""])
    if known:
        rows.append(["", "", "", "", "", ""])
        rows.append(["KNOWN — already triaged, listed so they are not forgotten",
                     "", "", "", "", ""])
        for lvl, area, msg in known:
            reason = ACCEPTED.get(area, "within expected range")
            rows.append(["known", ascii_(area), ascii_(msg), ascii_(reason), "", ""])

    wsh.clear()
    wsh.update(values=rows, range_name="A1", value_input_option="USER_ENTERED")
    wsh.format("A1:F1", {
        "textFormat": {"bold": True,
                       "foregroundColor": ({"red": 0.7} if fails else {"green": 0.45})},
    })
    wsh.format("A3:F3", {"textFormat": {"bold": True}})
    print(f"\n  wrote '{HEALTH_TAB}' tab — status: {status}")
except Exception as e:
    print(f"\n  [WARN] could not write '{HEALTH_TAB}' tab: {e}")
