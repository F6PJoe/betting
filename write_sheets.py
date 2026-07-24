"""
write_sheets.py — Dynamic daily sheet writer.
Reads today's bets from Bet History + Team Totals tabs (already written by
analyze_edges.py) and yesterday's graded results, then writes to:
  1. MLB Betting Cheat Sheet (Sheet1)
  2. Best Bets Today (Sheet1)
  3. Tracker - Dave (finalize yesterday + enter today)
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta
import time

# ── Auth ──────────────────────────────────────────────────────────────────────
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
creds  = Credentials.from_service_account_file("google_credentials.json", scopes=SCOPES)
gc     = gspread.authorize(creds)

ODDS_SHEET_ID       = "1RaSm1ogJtNykM7WbYfQ3b9L7MUePcRBqlFMKuQfA_I4"
CHEATSHEET_SHEET_ID = "1goyJ0AM7XqalRWk6IiemHRkvgdayo7E27so86T-y0kM"
BESTBETS_SHEET_ID   = "1_PQ19dvvD51uYCZcNw6EzV37RkXdDTZpemE9xbDwgTw"

today     = datetime.now().strftime("%Y-%m-%d")
yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

# Full name → abbreviation for cheat sheet / tracker labels
_FULL_TO_ABBREV = {
    "Arizona Diamondbacks": "ARI", "Athletics": "ATH",          "Atlanta Braves": "ATL",
    "Baltimore Orioles": "BAL",    "Boston Red Sox": "BOS",      "Chicago Cubs": "CHC",
    "Chicago White Sox": "CWS",    "Cincinnati Reds": "CIN",     "Cleveland Guardians": "CLE",
    "Colorado Rockies": "COL",     "Detroit Tigers": "DET",      "Houston Astros": "HOU",
    "Kansas City Royals": "KC",    "Los Angeles Angels": "LAA",  "Los Angeles Dodgers": "LAD",
    "Miami Marlins": "MIA",        "Milwaukee Brewers": "MIL",   "Minnesota Twins": "MIN",
    "New York Mets": "NYM",        "New York Yankees": "NYY",    "Oakland Athletics": "ATH",
    "Philadelphia Phillies": "PHI","Pittsburgh Pirates": "PIT",  "San Diego Padres": "SD",
    "Seattle Mariners": "SEA",     "San Francisco Giants": "SF", "St. Louis Cardinals": "STL",
    "Tampa Bay Rays": "TB",        "Texas Rangers": "TEX",       "Toronto Blue Jays": "TOR",
    "Washington Nationals": "WSH",
}

def abbrev_team(name: str) -> str:
    return _FULL_TO_ABBREV.get(name.strip(), name.strip())

def col(header, name):
    try:    return header.index(name)
    except: return -1

def sheets_call(fn, *args, **kwargs):
    for attempt in range(5):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            if "429" in str(e) and attempt < 4:
                wait = 15 * (attempt + 1)
                print(f"  [Rate limit — waiting {wait}s]")
                time.sleep(wait)
            else:
                raise

# ── Read today's official bets from Bet History ───────────────────────────────
odds_sh   = gc.open_by_key(ODDS_SHEET_ID)
ws_hist   = odds_sh.worksheet("Bet History")
hist_rows = ws_hist.get_all_values()
hist_hdr  = hist_rows[0] if hist_rows else []
hist_data = hist_rows[1:] if len(hist_rows) > 1 else []

hc_date    = col(hist_hdr, "Date")
hc_game    = col(hist_hdr, "Game")
hc_btype   = col(hist_hdr, "Bet Type")
hc_dir     = col(hist_hdr, "Direction")
hc_beton   = col(hist_hdr, "Bet On")
hc_stars   = col(hist_hdr, "Stars")
hc_units   = col(hist_hdr, "Units Bet")
hc_juice   = col(hist_hdr, "Book Juice")
hc_dkjuice = col(hist_hdr, "DK Juice")
hc_proj    = col(hist_hdr, "Our Projection")
hc_conf    = col(hist_hdr, "Confidence %")
hc_book    = col(hist_hdr, "Book")
hc_line    = col(hist_hdr, "Book Line")
hc_result  = col(hist_hdr, "Result")
hc_units_r = col(hist_hdr, "Units Result")

def hv(row, c):
    return row[c] if c >= 0 and c < len(row) else ""

def parse_stars(s):
    return str(s).count("⭐") or (int(float(s)) if str(s).replace(".","").isdigit() else 0)

def _parse_odds_str(s):
    """Convert an odds string to int American odds. Handles plain ints and %-formatted cells."""
    s = s.strip()
    if s.endswith("%"):
        # Percentage-formatted cell: "21500.00%" means the cell holds 215, displayed as 21500%
        return int(round(float(s[:-1]) / 100))
    return int(float(s))

def odds_to_num(s):
    """Convert odds string like '+114' or '-120' to int for tracker column F.
    Column F formulas (H, K, M) require a numeric value to detect negative odds correctly."""
    try:
        return int(str(s).replace("'", "").replace('+', '').strip())
    except (ValueError, TypeError):
        return s

def fmt_odds(juice_str, dk_str=""):
    """Return best odds as plain string (negative or positive American odds)."""
    val_str = dk_str.strip() if dk_str.strip() else juice_str.strip()
    try:
        val = _parse_odds_str(val_str)
        return f"'+{val}" if val > 0 else str(val)
    except (ValueError, TypeError):
        return val_str

def _parse_proj(s):
    """Parse a projection value from a cell that may be percentage-formatted."""
    s = str(s).strip()
    if s.endswith("%"):
        return float(s[:-1]) / 100
    return float(s)

def build_notes(row):
    btype = hv(row, hc_btype)
    direc = hv(row, hc_dir)
    beton = hv(row, hc_beton)
    game  = hv(row, hc_game)
    proj  = hv(row, hc_proj)
    stars = parse_stars(hv(row, hc_stars))
    star_word = "5 star" if stars >= 5 else "4 star"

    try: proj_f = _parse_proj(proj)
    except: proj_f = None

    if btype == "Game Total":
        away, home = (game.split(" @ ") + ["", ""])[:2]
        d = "over" if direc == "Over" else "under"
        return f"My betting model shows this as a {star_word} bet as I have it projected for {proj_f} runs" if proj_f else ""
    elif btype == "Moneyline":
        # proj stored as "55.0%" string — use raw value, not parsed decimal
        pct = proj.strip() if proj.strip() else ""
        if not pct:
            return ""
        return f"My betting model shows this as a {star_word} bet as I have them projected to win {pct} of the time"
    elif btype == "Run Line":
        pct = proj.strip() if proj.strip() else ""
        if not pct:
            return ""
        return f"My betting model shows this as a {star_word} bet as I have them projected to win by 2+ runs {pct} of the time"
    return ""

def bet_label(row):
    btype = hv(row, hc_btype)
    direc = hv(row, hc_dir)
    beton = hv(row, hc_beton).replace(" -1.5", "").replace(" ML", "").strip()
    game  = hv(row, hc_game)
    line  = hv(row, hc_line)
    if btype == "Game Total":
        d = "O" if direc == "Over" else "U"
        try:
            line_fmt = str(int(float(line))) if float(line) == int(float(line)) else line
        except (ValueError, TypeError):
            line_fmt = line
        # Abbreviate team names in game label (e.g. "Cincinnati Reds @ Seattle Mariners" → "CIN @ SEA")
        parts = game.split(" @ ")
        if len(parts) == 2:
            game = f"{abbrev_team(parts[0])} @ {abbrev_team(parts[1])}"
        return f"{game} {d}{line_fmt}"
    elif btype == "Moneyline":
        return f"{abbrev_team(beton)} ML"
    elif btype == "Run Line":
        return f"{abbrev_team(beton)} -1.5"
    return beton

# Collect today's official bets sorted by units desc, confidence desc
def sort_key(row):
    try: u = float(hv(row, hc_units))
    except: u = 0.0
    try: c = float(str(hv(row, hc_conf)).replace("%",""))
    except: c = 0.0
    return (u, c)

_all_today_bets = [r for r in hist_data
                   if hv(r, hc_date) == today
                   and parse_stars(hv(r, hc_stars)) >= 4]
# Deduplicate to best bet per (game, bet type, direction) — keeps highest units
# e.g. u9.5 and u9.0 on the same game both qualify; only the better edge shows
_best_bets: dict = {}
for _r in _all_today_bets:
    _key = (hv(_r, hc_game), hv(_r, hc_btype), hv(_r, hc_dir))
    _u, _c = sort_key(_r)
    if _key not in _best_bets or (_u, _c) > _best_bets[_key][0]:
        _best_bets[_key] = ((_u, _c), _r)
today_bets = [v[1] for v in _best_bets.values()]
today_bets.sort(key=sort_key, reverse=True)

# ── Read today's team totals ──────────────────────────────────────────────────
ws_tt   = odds_sh.worksheet("Team Totals")
tt_rows = ws_tt.get_all_values()
tt_hdr  = tt_rows[0] if tt_rows else []
tt_data = tt_rows[1:] if len(tt_rows) > 1 else []

tc_date   = col(tt_hdr, "Date")
tc_game   = col(tt_hdr, "Game")
tc_team   = col(tt_hdr, "Team")
tc_dir    = col(tt_hdr, "Direction")
tc_line   = col(tt_hdr, "Book Line")
tc_juice  = col(tt_hdr, "Book Juice")
tc_proj   = col(tt_hdr, "Our Projection")
tc_stars  = col(tt_hdr, "Stars")
tc_units  = col(tt_hdr, "Units")
tc_book   = col(tt_hdr, "Best Book")

def tv(row, c):
    return row[c] if c >= 0 and c < len(row) else ""

def tt_sort_key(row):
    try: u = float(tv(row, tc_units))
    except: u = 0.0
    return u

_all_today_tt = [r for r in tt_data
                 if tv(r, tc_date) == today
                 and str(tv(r, tc_stars)).count("⭐") >= 4]
# Deduplicate to best-juice book per (team, direction) — same logic as Edges tab
def _tt_juice_val(row):
    try: return _parse_odds_str(tv(row, tc_juice))
    except: return -999
_best_tt: dict = {}
for _r in _all_today_tt:
    _key = (tv(_r, tc_game), tv(_r, tc_dir))  # same key as Edges tab: best per game+direction
    _j = _tt_juice_val(_r)
    if _key not in _best_tt or _j > _best_tt[_key][0]:
        _best_tt[_key] = (_j, _r)
today_tt = [v[1] for v in _best_tt.values()]
today_tt.sort(key=tt_sort_key, reverse=True)

def tt_label(row):
    team  = tv(row, tc_team)
    direc = tv(row, tc_dir)
    line  = tv(row, tc_line)
    d = "O" if direc == "Over" else "U"
    return f"{abbrev_team(team)} {d}{line}"

def tt_notes(row):
    team  = tv(row, tc_team)
    direc = tv(row, tc_dir)
    proj  = tv(row, tc_proj)
    stars = str(tv(row, tc_stars)).count("⭐")
    star_word = "5 star" if stars >= 5 else "4 star"
    d = "score" if direc == "Over" else "score"
    try: proj_f = float(proj)
    except: proj_f = None
    if proj_f:
        return f"My betting model shows this as a {star_word} bet as I have the {team} projected to score {proj_f} runs"
    return ""

def tt_odds(row):
    juice = tv(row, tc_juice)
    try:
        val = _parse_odds_str(juice)
        return f"'+{val}" if val > 0 else str(val)
    except: return juice

def tt_conf(row):
    """Stars-to-confidence proxy for unified sort (5 stars = 100, 4 = 80)."""
    s = str(tv(row, tc_stars))
    stars = s.count("⭐") or (int(float(s)) if s.replace(".", "").isdigit() else 0)
    return stars * 20.0

# All rows for cheat sheet — combined sort by (units desc, confidence desc)
combined = []
def _juice_ok(odds_str):
    """Return False if odds are -200 or worse — those bets are excluded from the cheat sheet."""
    try:
        return _parse_odds_str(odds_str) > -200
    except:
        return True

for row in today_bets:
    if hv(row, hc_btype) == "Team Total":
        continue  # TT bets come from Team Totals tab (avoid double-counting)
    juice_str = hv(row, hc_juice) or hv(row, hc_dkjuice)
    if not _juice_ok(juice_str):
        continue
    try: u = float(hv(row, hc_units))
    except: u = 0.0
    try: c = float(str(hv(row, hc_conf)).replace("%", ""))
    except: c = 0.0
    combined.append((u, c, bet_label(row), hv(row, hc_units),
                     fmt_odds(hv(row, hc_juice), hv(row, hc_dkjuice)), build_notes(row)))
for row in today_tt:
    if not _juice_ok(tv(row, tc_juice)):
        continue
    try: u = float(tv(row, tc_units))
    except: u = 0.0
    combined.append((u, tt_conf(row), tt_label(row), tv(row, tc_units),
                     tt_odds(row), tt_notes(row)))
combined.sort(key=lambda x: (x[0], x[1]), reverse=True)
all_cheat_rows = [(label, units, odds, notes) for _, _, label, units, odds, notes in combined]

# Best bet = first official bet (already sorted by units/confidence)
best_bet = all_cheat_rows[0] if all_cheat_rows else None

# ── 1. MLB Betting Cheat Sheet — clear today's rows then rewrite ──────────────
sh1 = gc.open_by_key(CHEATSHEET_SHEET_ID)
ws1 = sh1.worksheet("Sheet1")
# Remove any rows already written for today (prevent duplicates on re-runs)
existing_cs = ws1.get_all_values()
cs_rows_to_del = [i + 1 for i, r in enumerate(existing_cs) if r and r[4].strip() == "Dave Eddy"]
if cs_rows_to_del:
    ws1.delete_rows(min(cs_rows_to_del), max(cs_rows_to_del))
    time.sleep(1)
for label, units, odds, notes in all_cheat_rows:
    sheets_call(ws1.append_row, [label, units, odds, notes, "Dave Eddy"],
                value_input_option="USER_ENTERED")
    time.sleep(0.3)
print(f"Cheat Sheet: {len(all_cheat_rows)} rows written (cleared duplicates first).")

# ── 2. Best Bets Today ────────────────────────────────────────────────────────
sh2 = gc.open_by_key(BESTBETS_SHEET_ID)
ws2 = sh2.worksheet("Sheet1")
if best_bet:
    all_rows = ws2.get_all_values()
    # Delete any existing Dave Eddy rows (other analysts' rows stay untouched)
    for i in range(len(all_rows) - 1, 0, -1):  # iterate bottom-up so indices stay valid
        if all_rows[i][4].strip().lower() == "dave eddy":
            sheets_call(ws2.delete_rows, i + 1)
            time.sleep(0.3)
    sheets_call(ws2.append_row,
                [best_bet[0], best_bet[1], best_bet[2], best_bet[3], "Dave Eddy"],
                value_input_option="USER_ENTERED")
print("Best Bets: 1 row written.")

# ── 3. Tracker - Dave ─────────────────────────────────────────────────────────
sh3  = gc.open_by_key(CHEATSHEET_SHEET_ID)
ws3  = sh3.worksheet("Tracker - Dave")
rows = ws3.get_all_values()

# ── Retroactive repair: write M formula for any row that has a result (I=W/L/P)
# but is missing the M formula (formula col M is blank). M must be set for K to
# compute profit correctly (K = M-J on a win, J*-1 on a loss).
m_repair = []
for i, row in enumerate(rows[1:], start=2):
    if not row or not row[0].strip():
        continue
    result_i = row[8] if len(row) > 8 else ""
    m_val    = row[12] if len(row) > 12 else ""
    # Row has a result but M is blank → formula was never written
    if result_i.strip() in ("W", "L", "P") and m_val.strip() == "":
        m_repair.append({"range": f"M{i}", "values": [[f"=if(I{i}=\"W\",H{i}*J{i},J{i}*-1)"]]})
if m_repair:
    sheets_call(ws3.batch_update, m_repair, value_input_option="USER_ENTERED")
    print(f"Tracker: repaired M formula for {len(m_repair)} historical rows.")

# Finalize yesterday's rows: find Pending entries (col N has stake, col I is blank)
# Match by searching col E (bet label) against graded Bet History rows
yest_results = {}
for row in hist_data:
    if hv(row, hc_date) != yesterday:
        continue
    result = hv(row, hc_result)
    if result not in ("Win", "Loss", "Push"):
        continue
    label = bet_label(row)
    try: units_r = float(hv(row, hc_units_r))
    except: units_r = 0.0
    try: stake = abs(float(hv(row, hc_units)))
    except: stake = 0.0
    yest_results[label] = (result[0], stake, units_r)  # W/L/P, stake, units result

# Also include team totals from yesterday
ws_tt_yest = odds_sh.worksheet("Team Totals")
tt_all = ws_tt_yest.get_all_values()
tt_hdr2 = tt_all[0] if tt_all else []
for trow in tt_all[1:]:
    def tv2(c): return trow[c] if c >= 0 and c < len(trow) else ""
    date_c   = col(tt_hdr2, "Date")
    result_c = col(tt_hdr2, "Result")
    units_c  = col(tt_hdr2, "Units")
    unitsr_c = col(tt_hdr2, "Units Result")
    if tv2(date_c) != yesterday:
        continue
    result = tv2(result_c)
    if result not in ("Win", "Loss", "Push"):
        continue
    label = tt_label(trow)
    try: stake = abs(float(tv2(units_c)))
    except: stake = 0.0
    try: units_r = float(tv2(unitsr_c))
    except: units_r = 0.0
    yest_results[label] = (result[0], stake, units_r)

finalize_updates = []
unmatched = []
_yest = datetime.now() - timedelta(days=1)
yesterday_display_variants = {
    _yest.strftime("%#m/%#d/%Y"),   # 7/9/2026
    _yest.strftime("%Y-%m-%d"),     # 2026-07-09
    _yest.strftime("%m/%d/%Y"),     # 07/09/2026
}
for i, row in enumerate(rows[1:], start=2):
    if not row or not row[0]:
        continue
    date_a    = row[0].strip() if row else ""
    bet_col_e = row[4] if len(row) > 4 else ""
    pending_n = row[13] if len(row) > 13 else ""
    result_i  = row[8] if len(row) > 8 else ""
    # Only finalize YESTERDAY's rows that have a pending stake and no result yet
    if date_a not in yesterday_display_variants:
        continue
    if pending_n and pending_n not in ("", "0") and result_i == "":
        if bet_col_e in yest_results:
            wlp, stake, _ = yest_results[bet_col_e]
            finalize_updates.append({"range": f"G{i}", "values": [[stake]]})
            finalize_updates.append({"range": f"I{i}", "values": [[wlp]]})
            finalize_updates.append({"range": f"N{i}", "values": [[""]]})
        else:
            unmatched.append(f"  Row {i} ({date_a}): '{bet_col_e}' — no match")

if finalize_updates:
    sheets_call(ws3.batch_update, finalize_updates, value_input_option="USER_ENTERED")
    print(f"Tracker: finalized {len(finalize_updates)//3} yesterday rows.")
else:
    print("Tracker: no pending rows to finalize.")
if unmatched:
    print(f"Tracker: {len(unmatched)} unmatched rows (label mismatch — check manually):")
    for m in unmatched[:8]:
        print(m)

# Enter today's bets — first-run protection + auto-expand sheet if needed
tracker_data = ws3.get_all_values()

today_display = datetime.now().strftime("%#m/%#d/%Y")  # Windows: %#m/%#d/%Y strips leading zeros

# Check if today's date is already in the tracker (col A)
# Accept both "7/10/2026" (text) and "2026-07-10" (ISO) to handle format variations
today_iso = datetime.now().strftime("%Y-%m-%d")
today_padded = datetime.now().strftime("%m/%d/%Y")  # "07/10/2026" with leading zeros
today_variants = {today_display, today_iso, today_padded}
today_already = any(
    row and row[0].strip() in today_variants
    for row in tracker_data[1:]
)

if today_already:
    print(f"Tracker: '{today_display}' already in tracker — skipping entry, checking labels/results.")
    correct_labels = [label for label, _, _, _ in all_cheat_rows]
    repair_updates = []
    rows_to_delete = []

    today_rows_in_tracker = [
        (i + 2, row)
        for i, row in enumerate(tracker_data[1:])
        if row and row[0].strip() in today_variants
    ]
    print(f"  Found {len(today_rows_in_tracker)} today rows to repair.")

    # Keep only the first N rows (correct count), mark extras for deletion
    expected = len(correct_labels)
    correct_odds = [odds for _, _, odds, _ in all_cheat_rows]
    correct_units = [units for _, units, _, _ in all_cheat_rows]

    for idx, (sheet_row, trow) in enumerate(today_rows_in_tracker):
        if idx >= expected:
            rows_to_delete.append(sheet_row)
            continue
        # Fix label (col E)
        current_label = trow[4] if len(trow) > 4 else ""
        if current_label != correct_labels[idx]:
            repair_updates.append({"range": f"E{sheet_row}", "values": [[correct_labels[idx]]]})
        # Fix odds (col F) — always overwrite as numeric so H/K/M formulas work correctly
        repair_updates.append({"range": f"F{sheet_row}", "values": [[odds_to_num(correct_odds[idx])]]})
        # Fix units (col N) — ensure correct
        repair_updates.append({"range": f"N{sheet_row}", "values": [[correct_units[idx]]]})
        # Clear any wrongly-assigned result (col G stake + col I result) for today
        col_i = trow[8] if len(trow) > 8 else ""
        if col_i.strip() in ("W", "L", "P", "Win", "Loss", "Push"):
            repair_updates.append({"range": f"G{sheet_row}", "values": [[""]]})
            repair_updates.append({"range": f"I{sheet_row}", "values": [[""]]})

    if repair_updates:
        sheets_call(ws3.batch_update, repair_updates, value_input_option="RAW")
        print(f"  Repaired {min(len(today_rows_in_tracker), expected)} today rows (labels, odds, units).")

    if rows_to_delete:
        ws3.delete_rows(min(rows_to_delete), max(rows_to_delete))
        print(f"  Deleted {len(rows_to_delete)} duplicate today rows (rows {min(rows_to_delete)}-{max(rows_to_delete)}).")

    # If bet count grew (e.g. force rerun added new team totals), append missing rows
    found = len(today_rows_in_tracker)
    if expected > found:
        # Find where existing today rows end in the sheet
        if today_rows_in_tracker:
            insert_after = max(sr for sr, _ in today_rows_in_tracker)
        else:
            # Fallback: find last data row
            insert_after = max((i + 1 for i, r in enumerate(tracker_data) if r and r[0].strip()), default=1)
        new_updates = []
        formula_updates = []
        for i in range(found, expected):
            r = insert_after + 1 + (i - found)
            label, units, odds, _ = all_cheat_rows[i]
            new_updates += [
                {"range": f"A{r}", "values": [[today_display]]},
                {"range": f"E{r}", "values": [[label]]},
                {"range": f"F{r}", "values": [[odds_to_num(odds)]]},
                {"range": f"N{r}", "values": [[units]]},
            ]
            formula_updates.append({"range": f"M{r}", "values": [[f"=if(I{r}=\"W\",H{r}*J{r},J{r}*-1)"]]})
        if new_updates:
            sheets_call(ws3.batch_update, new_updates, value_input_option="RAW")
        if formula_updates:
            sheets_call(ws3.batch_update, formula_updates, value_input_option="USER_ENTERED")
            print(f"  Added {expected - found} new today rows (bets added by force rerun).")
else:
    # Find last row with data in col A (date column) — more reliable than any-cell check
    last_row = 1
    for i, row in enumerate(tracker_data):
        if row and row[0].strip():
            last_row = i + 1
    next_row = last_row + 1

    # Expand sheet if needed
    needed = next_row + len(all_cheat_rows)
    sheet_meta = ws3.spreadsheet.fetch_sheet_metadata()
    for s in sheet_meta["sheets"]:
        if s["properties"]["title"] == "Tracker - Dave":
            current_rows = s["properties"]["gridProperties"]["rowCount"]
            if needed > current_rows:
                ws3.add_rows(needed - current_rows + 50)
                print(f"  Tracker: expanded sheet to {needed + 50} rows")
            break

    today_updates = []
    formula_updates = []
    for i, (label, units, odds, notes) in enumerate(all_cheat_rows):
        r = next_row + i
        today_updates += [
            {"range": f"A{r}", "values": [[today_display]]},
            {"range": f"E{r}", "values": [[label]]},
            {"range": f"F{r}", "values": [[odds_to_num(odds)]]},
            {"range": f"N{r}", "values": [[units]]},
        ]
        formula_updates.append({"range": f"M{r}", "values": [[f"=if(I{r}=\"W\",H{r}*J{r},J{r}*-1)"]]})

    if today_updates:
        sheets_call(ws3.batch_update, today_updates, value_input_option="RAW")
    if formula_updates:
        sheets_call(ws3.batch_update, formula_updates, value_input_option="USER_ENTERED")
    print(f"Tracker: {len(all_cheat_rows)} new rows entered starting at row {next_row}.")
