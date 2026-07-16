"""
nfl_grade_bets.py — Grade yesterday's NFL bets and rebuild the Performance tab.
Run this each morning before nfl_fetch_odds.py / nfl_analyze_edges.py.

STATUS: Framework only (Step 2 of the build). Grading functions are stubs
until Step 3 wires up actual final scores (via nfl_data_py schedules/scores).
Running this now will create the Performance and Calibration Tracker tabs
with headers but no graded rows — that's expected.
"""

import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta
import os

# ── Config ────────────────────────────────────────────────────────────────────
NFL_SHEET_ID = "1UPempH9iWF-DQFh5d26zjpft3-XLehp30PZPfE0tpsE"
CREDS_FILE   = os.path.join(os.path.dirname(__file__), "google_credentials.json")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

PERFORMANCE_HEADER = [
    "Date", "Bet Type", "Stars", "Wins", "Losses", "Pushes",
    "Win %", "Units Won", "ROI %",
]

CALIBRATION_HEADER = [
    "Date", "Bet Type", "Sample Size", "Predicted Win %", "Actual Win %",
    "Calibration Error", "Notes",
]


def get_client():
    creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


def get_ws(gc, tab: str):
    return gc.open_by_key(NFL_SHEET_ID).worksheet(tab)


def get_or_create_ws(gc, tab: str, header: list[str], rows=2000, cols=20):
    sh = gc.open_by_key(NFL_SHEET_ID)
    try:
        w = sh.worksheet(tab)
        if not w.get_all_values():
            w.update([header], value_input_option="USER_ENTERED")
        return w
    except gspread.exceptions.WorksheetNotFound:
        w = sh.add_worksheet(title=tab, rows=rows, cols=max(cols, len(header) + 2))
        w.update([header], value_input_option="USER_ENTERED")
        return w


# ── TODO (Step 3): actual grading logic ───────────────────────────────────────
def grade_bet_history(gc, yesterday: str) -> int:
    """TODO Step 3: pull final scores (nfl_data_py schedules) and fill in
    Away Score / Home Score / Result / Units Result for yesterday's rows in
    Bet History. Returns count of rows graded."""
    return 0


def grade_game_totals_shadow(gc, yesterday: str) -> int:
    """TODO Step 3: same as grade_bet_history but for the 'Game Totals' shadow tab."""
    return 0


def grade_ml_spread_shadow(gc, yesterday: str) -> int:
    """TODO Step 3: same as grade_bet_history but for the 'ML Spread' shadow tab."""
    return 0


def grade_team_totals(gc, yesterday: str) -> int:
    """TODO Step 3: same as grade_bet_history but for the 'Team Totals' tab."""
    return 0


def rebuild_performance(gc):
    """TODO Step 3: aggregate graded rows from Bet History / Game Totals /
    ML Spread / Team Totals into by-date/by-type/by-star summary rows,
    matching the MLB Performance tab convention."""
    w = get_or_create_ws(gc, "Performance", PERFORMANCE_HEADER)
    w.clear()
    w.update([PERFORMANCE_HEADER], value_input_option="USER_ENTERED")
    print("Performance tab rebuilt (0 rows — no graded bets yet)")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("nfl_grade_bets.py — Fantasy Six Pack NFL Bet Grader")
    print(f"Run time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    gc = get_client()
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    print(f"\nGrading bets for {yesterday} (stub — no results wired up yet) ...")
    n_hist    = grade_bet_history(gc, yesterday)
    n_gt      = grade_game_totals_shadow(gc, yesterday)
    n_ml      = grade_ml_spread_shadow(gc, yesterday)
    n_tt      = grade_team_totals(gc, yesterday)
    print(f"  Bet History: {n_hist} graded")
    print(f"  Game Totals shadow: {n_gt} graded")
    print(f"  ML Spread shadow: {n_ml} graded")
    print(f"  Team Totals: {n_tt} graded")

    print("\nRebuilding Performance tab ...")
    rebuild_performance(gc)

    print("\nEnsuring Calibration Tracker tab exists ...")
    get_or_create_ws(gc, "Calibration Tracker", CALIBRATION_HEADER)

    print("\nDone. (Framework run — grading logic lands in Step 3.)")


if __name__ == "__main__":
    main()
