"""
fetch_closing_lines.py — Closing Line Value (CLV) tracker.

Run ~30 minutes before first pitch each evening.
Fetches current odds lines, compares them to the morning lines stored in
Bet History and Game Total Shadow, then writes:
  • Closing Line   — the line at time of run
  • CLV            — "Beat" / "Lost" / "Push" vs. the closing line

Usage:
  python fetch_closing_lines.py

Schedule: run this daily around 6:30 PM ET (before most 7:05 PM ET first pitches).
"""

import gspread
from google.oauth2.service_account import Credentials
import requests
import os
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# ── Config ────────────────────────────────────────────────────────────────────
ODDS_SHEET_ID = "1RaSm1ogJtNykM7WbYfQ3b9L7MUePcRBqlFMKuQfA_I4"
ODDS_API_KEY  = os.environ["ODDS_API_KEY_NFL"]
CREDS_FILE    = os.path.join(os.path.dirname(__file__), "google_credentials.json")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

BOOKS_FOR_CLOSING = {"fanduel", "draftkings", "betmgm", "betrivers"}


# ── Auth ──────────────────────────────────────────────────────────────────────
def auth():
    creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


def get_ws(gc, tab):
    return gc.open_by_key(ODDS_SHEET_ID).worksheet(tab)


# ── Fetch current totals lines ────────────────────────────────────────────────
def fetch_current_totals() -> dict:
    """
    Returns {game_key: closing_line} where game_key = "away @ home" (lowercased).
    Uses median of available book lines to get consensus closing line.
    """
    url = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/"
    params = {
        "apiKey":    ODDS_API_KEY,
        "regions":   "us",
        "markets":   "totals",
        "oddsFormat": "american",
        "bookmakers": ",".join(BOOKS_FOR_CLOSING),
    }
    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        events = resp.json()
        remaining = resp.headers.get("x-requests-remaining", "?")
        print(f"  Odds API: {len(events)} events, {remaining} credits remaining")
    except Exception as e:
        print(f"  [Odds API error: {e}]")
        return {}

    closing = {}
    for ev in events:
        away = ev.get("away_team", "")
        home = ev.get("home_team", "")
        key  = f"{away} @ {home}".lower()

        lines = []
        for bm in ev.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt.get("key") != "totals":
                    continue
                for outcome in mkt.get("outcomes", []):
                    if outcome.get("name") == "Over":
                        try:
                            lines.append(float(outcome["point"]))
                        except (KeyError, ValueError, TypeError):
                            pass

        if lines:
            # Median closing line across books
            lines.sort()
            mid = len(lines) // 2
            closing[key] = lines[mid] if len(lines) % 2 else (lines[mid-1] + lines[mid]) / 2

    return closing


# ── CLV helpers ───────────────────────────────────────────────────────────────
def calc_clv(direction: str, morning_line: float, closing_line: float) -> str:
    """
    Returns "Beat", "Lost", or "Push".
    Beat CLV = we got better of the closing line (bettors paid MORE juice at close).
    For OVER: morning line < closing line → closing moved up → we got it cheaper = Beat
    For UNDER: morning line > closing line → closing moved down → under is "cheaper" = Beat
    """
    if abs(morning_line - closing_line) < 0.05:
        return "Push"
    direction = direction.upper()
    if direction in ("OVER", "O"):
        return "Beat" if morning_line < closing_line else "Lost"
    else:  # UNDER
        return "Beat" if morning_line > closing_line else "Lost"


# ── Update Bet History ────────────────────────────────────────────────────────
def update_bet_history_clv(ws_hist, closing_lines: dict, today: str) -> int:
    """
    Writes Closing Line + CLV into Bet History for today's rows.
    Adds the columns if they don't exist yet (appends to header).
    """
    all_vals = ws_hist.get_all_values()
    if len(all_vals) < 2:
        return 0

    header = all_vals[0]

    # Ensure CLV columns exist — append if missing
    dirty_header = False
    if "Closing Line" not in header:
        header.append("Closing Line")
        dirty_header = True
    if "CLV" not in header:
        header.append("CLV")
        dirty_header = True

    c_date    = header.index("Date")
    c_game    = header.index("Game")
    c_dir     = header.index("Direction") if "Direction" in header else -1
    c_line    = header.index("Book Line")
    c_result  = header.index("Result")
    c_closing = header.index("Closing Line")
    c_clv     = header.index("CLV")

    if dirty_header:
        ws_hist.update(values=[header], range_name="A1", value_input_option="RAW")

    updates = []
    for i, row in enumerate(all_vals[1:], start=2):
        while len(row) < len(header):
            row.append("")

        if row[c_date] != today:
            continue
        if row[c_result] not in ("", "Pending"):
            continue
        if row[c_closing]:   # already written
            continue

        game_key  = row[c_game].lower()
        direction = row[c_dir] if c_dir >= 0 else ""
        try:
            morning_line = float(row[c_line])
        except (ValueError, TypeError):
            continue

        cline = closing_lines.get(game_key)
        if cline is None:
            continue

        clv = calc_clv(direction, morning_line, cline)
        updates.append((i, c_closing + 1, cline, c_clv + 1, clv))

    for (row_i, col_cl, cline, col_clv, clv) in updates:
        ws_hist.batch_update([
            {"range": f"R{row_i}C{col_cl}",  "values": [[cline]]},
            {"range": f"R{row_i}C{col_clv}", "values": [[clv]]},
        ], value_input_option="RAW")

    return len(updates)


# ── Update Game Total Shadow ──────────────────────────────────────────────────
def update_gt_shadow_clv(ws_gt, closing_lines: dict, today: str) -> int:
    """
    Writes Closing Line + CLV into Game Total Shadow for today's rows.
    """
    all_vals = ws_gt.get_all_values()
    if len(all_vals) < 2:
        return 0

    header = all_vals[0]

    dirty_header = False
    if "Closing Line" not in header:
        header.append("Closing Line")
        dirty_header = True
    if "CLV" not in header:
        header.append("CLV")
        dirty_header = True

    if dirty_header:
        ws_gt.update(values=[header], range_name="A1", value_input_option="RAW")

    c_date    = header.index("Date")
    c_game    = header.index("Game")
    c_dir     = header.index("Direction")
    c_line    = header.index("Book Line")
    c_away_sc = header.index("Away Score") if "Away Score" in header else -1
    c_closing = header.index("Closing Line")
    c_clv     = header.index("CLV")

    updates = []
    for i, row in enumerate(all_vals[1:], start=2):
        while len(row) < len(header):
            row.append("")

        if row[c_date] != today:
            continue
        # Skip if already graded (away score present)
        if c_away_sc >= 0 and row[c_away_sc]:
            continue
        if row[c_closing]:
            continue

        game_key  = row[c_game].lower()
        direction = row[c_dir]
        try:
            morning_line = float(row[c_line])
        except (ValueError, TypeError):
            continue

        cline = closing_lines.get(game_key)
        if cline is None:
            continue

        clv = calc_clv(direction, morning_line, cline)
        updates.append((i, c_closing + 1, cline, c_clv + 1, clv))

    for (row_i, col_cl, cline, col_clv, clv) in updates:
        ws_gt.batch_update([
            {"range": f"R{row_i}C{col_cl}",  "values": [[cline]]},
            {"range": f"R{row_i}C{col_clv}", "values": [[clv]]},
        ], value_input_option="RAW")

    return len(updates)


# ── CLV Summary ───────────────────────────────────────────────────────────────
def print_clv_summary(ws_gt):
    """Print a running CLV summary from Game Total Shadow."""
    try:
        all_vals = ws_gt.get_all_values()
        if len(all_vals) < 2:
            return
        header = all_vals[0]
        if "CLV" not in header:
            return
        c_clv = header.index("CLV")
        tallies = {"Beat": 0, "Lost": 0, "Push": 0}
        for row in all_vals[1:]:
            v = row[c_clv] if len(row) > c_clv else ""
            if v in tallies:
                tallies[v] += 1
        total = tallies["Beat"] + tallies["Lost"]
        pct   = tallies["Beat"] / total * 100 if total else 0
        print(f"\n  CLV Summary (Game Total Shadow — all time):")
        print(f"    Beat closing line: {tallies['Beat']}/{total}  ({pct:.1f}%)")
        print(f"    Push:              {tallies['Push']}")
        print(f"    (Target: >50% to show consistent model edge)")
    except Exception as e:
        print(f"  [CLV summary error: {e}]")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    today = datetime.now().strftime("%Y-%m-%d")
    print("=" * 60)
    print("fetch_closing_lines.py — Fantasy Six Pack CLV Tracker")
    print(f"Run time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    print("\nFetching current odds (closing lines) ...")
    closing_lines = fetch_current_totals()
    print(f"  {len(closing_lines)} game totals fetched")

    if not closing_lines:
        print("No closing lines available — exiting.")
        return

    print("\nConnecting to Google Sheets ...")
    gc = auth()

    print("\nUpdating Bet History CLV ...")
    ws_hist = get_ws(gc, "Bet History")
    n_hist = update_bet_history_clv(ws_hist, closing_lines, today)
    print(f"  {n_hist} Bet History rows updated with closing line / CLV")

    print("\nUpdating Game Total Shadow CLV ...")
    ws_gt = get_ws(gc, "Game Total Shadow")
    n_gt = update_gt_shadow_clv(ws_gt, closing_lines, today)
    print(f"  {n_gt} Game Total Shadow rows updated with closing line / CLV")

    print_clv_summary(ws_gt)

    print("\nDone.")


if __name__ == "__main__":
    main()
