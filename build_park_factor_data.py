"""
build_park_factor_data.py
Daily park factor tracking and calibration engine.

Run automatically at the end of grade_bets.py each morning.
Also runnable standalone: python build_park_factor_data.py

What it does every day:
  1. Fetches all completed 2026 MLB games from the Stats API
  2. Updates the 'Park Factor Data' tab with the full season game log
  3. Loads the calibration state (games counted at last calibration per venue)
  4. Counts new games per venue since the last calibration
  5. When a venue hits 20+ new games:
       - Fetches the latest Savant 2026 park factor
       - Calculates the blended factor (current x 0.7 + Savant x 0.3)
       - If the change is 3+ points either direction: FLAGS for discussion
       - If the change is under 3 points: notes it but takes no action
  6. Updates the calibration state file

Nothing in the model changes automatically. Flags are printed in the
morning output for review and discussion before any factor is changed.
"""

import requests
import gspread
from google.oauth2.service_account import Credentials
from datetime import date, timedelta
import time
import os
import sys
import json
import re

sys.stdout.reconfigure(encoding="utf-8")


# ── Rate-limit retry wrapper ──────────────────────────────────────────────────
def sheets_call(fn, *args, retries=5, **kwargs):
    """Call a gspread function, retrying on 429 with exponential backoff."""
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            if "429" in str(e) and attempt < retries - 1:
                wait = 15 * (attempt + 1)
                print(f"  [Rate limit — waiting {wait}s, retry {attempt+1}/{retries-1}]")
                time.sleep(wait)
            else:
                raise

# ── Config ────────────────────────────────────────────────────────────────────
ODDS_SHEET_ID  = "1RaSm1ogJtNykM7WbYfQ3b9L7MUePcRBqlFMKuQfA_I4"
CREDS_FILE     = os.path.join(os.path.dirname(__file__), "google_credentials.json")
STATE_FILE     = os.path.join(os.path.dirname(__file__), "park_factor_state.json")
MLB_STATS_BASE = "https://statsapi.mlb.com/api/v1/schedule"
SAVANT_URL     = "https://baseballsavant.mlb.com/leaderboard/statcast-park-factors"
SEASON_START   = date(2026, 3, 18)
TAB_NAME       = "Park Factor Data"

CALIBRATION_GAME_THRESHOLD   = 20   # new games needed before checking
CALIBRATION_CHANGE_THRESHOLD = 3    # point change needed to flag

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ── Team name normalization ───────────────────────────────────────────────────
TEAM_NAME_MAP = {
    "Oakland Athletics": "Athletics",
    "Oakland A's":       "Athletics",
}

def normalize(name: str) -> str:
    return TEAM_NAME_MAP.get(name.strip(), name.strip())

# ── Venue → our PARK_FACTORS team key ────────────────────────────────────────
VENUE_TO_TEAM = {
    "Coors Field":                     "Colorado Rockies",
    "Daikin Park":                     "Houston Astros",
    "Nationals Park":                  "Washington Nationals",
    "Great American Ball Park":        "Cincinnati Reds",
    "PNC Park":                        "Pittsburgh Pirates",
    "Yankee Stadium":                  "New York Yankees",
    "Citizens Bank Park":              "Philadelphia Phillies",
    "Tropicana Field":                 "Tampa Bay Rays",
    "Kauffman Stadium":                "Kansas City Royals",
    "Oriole Park at Camden Yards":     "Baltimore Orioles",
    "Busch Stadium":                   "St. Louis Cardinals",
    "Rogers Centre":                   "Toronto Blue Jays",
    "Citi Field":                      "New York Mets",
    "T-Mobile Park":                   "Seattle Mariners",
    "Target Field":                    "Minnesota Twins",
    "American Family Field":           "Milwaukee Brewers",
    "Fenway Park":                     "Boston Red Sox",
    "Wrigley Field":                   "Chicago Cubs",
    "UNIQLO Field at Dodger Stadium":  "Los Angeles Dodgers",
    "Chase Field":                     "Arizona Diamondbacks",
    "Rate Field":                      "Chicago White Sox",
    "Progressive Field":               "Cleveland Guardians",
    "Oracle Park":                     "San Francisco Giants",
    "loanDepot park":                  "Miami Marlins",
    "Angel Stadium":                   "Los Angeles Angels",
    "Comerica Park":                   "Detroit Tigers",
    "Truist Park":                     "Atlanta Braves",
    "Petco Park":                      "San Diego Padres",
    "Globe Life Field":                "Texas Rangers",
    "Sutter Health Park":              "Athletics",
    "Las Vegas Ballpark":              "Athletics",
}

# ── Savant display name → our PARK_FACTORS team key ──────────────────────────
SAVANT_TO_TEAM = {
    "Rockies":    "Colorado Rockies",
    "Astros":     "Houston Astros",
    "Nationals":  "Washington Nationals",
    "Reds":       "Cincinnati Reds",
    "Pirates":    "Pittsburgh Pirates",
    "Yankees":    "New York Yankees",
    "Phillies":   "Philadelphia Phillies",
    "Rays":       "Tampa Bay Rays",
    "Royals":     "Kansas City Royals",
    "Orioles":    "Baltimore Orioles",
    "Cardinals":  "St. Louis Cardinals",
    "Blue Jays":  "Toronto Blue Jays",
    "Mets":       "New York Mets",
    "Mariners":   "Seattle Mariners",
    "Twins":      "Minnesota Twins",
    "Brewers":    "Milwaukee Brewers",
    "Red Sox":    "Boston Red Sox",
    "Cubs":       "Chicago Cubs",
    "Dodgers":    "Los Angeles Dodgers",
    "D-backs":    "Arizona Diamondbacks",
    "White Sox":  "Chicago White Sox",
    "Guardians":  "Cleveland Guardians",
    "Giants":     "San Francisco Giants",
    "Marlins":    "Miami Marlins",
    "Angels":     "Los Angeles Angels",
    "Tigers":     "Detroit Tigers",
    "Braves":     "Atlanta Braves",
    "Padres":     "San Diego Padres",
    "Rangers":    "Texas Rangers",
    "Athletics":  "Athletics",
}

# ── Current model park factors (keep in sync with analyze_edges.py) ───────────
# These are only used for comparison — never changed automatically by this script.
CURRENT_PARK_FACTORS = {
    "Colorado Rockies": 115,  "Houston Astros": 103,
    "Washington Nationals": 100, "Kansas City Royals": 100,
    "Milwaukee Brewers": 103, "Boston Red Sox": 102,
    "Chicago Cubs": 102,      "Tampa Bay Rays": 99,
    "Atlanta Braves": 99,     "Pittsburgh Pirates": 99,
    "Texas Rangers": 96,      "San Diego Padres": 94,
    "Cleveland Guardians": 96,"Detroit Tigers": 96,
    "Cincinnati Reds": 108,   "Philadelphia Phillies": 107,
    "New York Yankees": 102,  "Baltimore Orioles": 101,
    "Toronto Blue Jays": 100, "Minnesota Twins": 100,
    "Los Angeles Dodgers": 100,"Chicago White Sox": 99,
    "New York Mets": 98,      "St. Louis Cardinals": 98,
    "Miami Marlins": 97,      "Seattle Mariners": 96,
    "San Francisco Giants": 96,"Los Angeles Angels": 96,
    "Arizona Diamondbacks": 96,"Athletics": 110,
}

# Special venue overrides (not in CURRENT_PARK_FACTORS — handled separately)
SPECIAL_VENUES = {
    "Las Vegas Ballpark":  140,
    "Sutter Health Park":  122,
}

# International/neutral-site venues — exclude from park factor tracking
EXCLUDE_VENUES = {
    "estadio alfredo harp helu",   # MLB Mexico City Series (not a home park)
}


# ── Auth ──────────────────────────────────────────────────────────────────────
def auth():
    creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


# ── Fetch all 2026 games from MLB Stats API ───────────────────────────────────
def fetch_all_2026_games() -> list[dict]:
    yesterday = date.today() - timedelta(days=1)
    all_rows  = []
    chunk_start = SEASON_START

    while chunk_start <= yesterday:
        chunk_end = min(chunk_start + timedelta(days=29), yesterday)
        params = {
            "sportId":   1,
            "startDate": chunk_start.strftime("%Y-%m-%d"),
            "endDate":   chunk_end.strftime("%Y-%m-%d"),
            "gameType":  "R",
            "hydrate":   "venue,team,linescore",
        }
        try:
            resp = requests.get(MLB_STATS_BASE, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  [API error {chunk_start} - {chunk_end}: {e}]")
            chunk_start = chunk_end + timedelta(days=1)
            continue

        for date_block in data.get("dates", []):
            game_date = date_block.get("date", "")
            for game in date_block.get("games", []):
                if game.get("status", {}).get("abstractGameCode") != "F":
                    continue
                away_team = normalize(game["teams"]["away"]["team"]["name"])
                home_team = normalize(game["teams"]["home"]["team"]["name"])
                venue     = game.get("venue", {}).get("name", "Unknown")
                ls        = game.get("linescore", {})
                away_s    = ls.get("teams", {}).get("away", {}).get("runs", "")
                home_s    = ls.get("teams", {}).get("home", {}).get("runs", "")
                if away_s == "" or home_s == "":
                    continue
                if venue.lower() in EXCLUDE_VENUES:
                    continue
                all_rows.append({
                    "date": game_date, "away": away_team, "home": home_team,
                    "venue": venue, "away_score": away_s,
                    "home_score": home_s, "total": away_s + home_s,
                })

        chunk_start = chunk_end + timedelta(days=1)
        time.sleep(0.3)

    return all_rows


# ── Fetch Savant 2026 park factors ────────────────────────────────────────────
def fetch_savant_factors(year: int = 2026) -> dict:
    """
    Returns {team_key: runs_index} for all 30 teams.
    Parses the embedded JSON from the Savant leaderboard page.
    """
    params = {
        "type": "year", "year": str(year), "batSide": "",
        "stat": "index_wOBA", "condition": "All",
        "rolling": "1", "parks": "mlb",
    }
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(SAVANT_URL, params=params, headers=headers, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        print(f"  [Savant fetch error: {e}]")
        return {}

    matches = re.findall(r"var data\s*=\s*(\[.*?\]);", resp.text, re.DOTALL)
    if not matches:
        print("  [Savant: could not find embedded data]")
        return {}

    try:
        data = json.loads(matches[0])
    except Exception as e:
        print(f"  [Savant JSON parse error: {e}]")
        return {}

    factors = {}
    for row in data:
        display = row.get("name_display_club", "")
        team    = SAVANT_TO_TEAM.get(display)
        pf      = row.get("index_runs", "")
        if team and pf:
            try:
                factors[team] = int(pf)
            except (ValueError, TypeError):
                pass

    return factors


# ── State file (tracks game counts per venue at last calibration) ─────────────
def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def initialize_state(games: list[dict]) -> dict:
    """
    First-run setup: record current game counts so the counter
    starts fresh from today forward.
    """
    from collections import Counter
    venue_counts = Counter(g["venue"] for g in games)
    state = {}
    today = date.today().strftime("%Y-%m-%d")
    for venue, count in venue_counts.items():
        team = VENUE_TO_TEAM.get(venue)
        if not team:
            continue
        current_factor = SPECIAL_VENUES.get(venue) or CURRENT_PARK_FACTORS.get(team, 100)
        state[venue] = {
            "team":                    team,
            "games_at_calibration":    count,
            "last_calibration_date":   today,
            "factor_at_calibration":   current_factor,
        }
    return state


# ── Update Park Factor Data tab ───────────────────────────────────────────────
def update_sheet(gc, games: list[dict], state: dict = None):
    sh = gc.open_by_key(ODDS_SHEET_ID)
    try:
        ws = sh.worksheet(TAB_NAME)
        ws.clear()
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=TAB_NAME, rows=3000, cols=20)

    # Raw game log (A-G)
    header  = ["Date", "Away", "Home", "Venue", "Away Score", "Home Score", "Total Runs"]
    data    = [header] + [
        [g["date"], g["away"], g["home"], g["venue"],
         g["away_score"], g["home_score"], g["total"]]
        for g in sorted(games, key=lambda x: x["date"])
    ]

    # Venue summary (I-P)
    from collections import defaultdict
    venue_stats = defaultdict(lambda: {"games": 0, "total_runs": 0})
    for g in games:
        venue_stats[g["venue"]]["games"]      += 1
        venue_stats[g["venue"]]["total_runs"] += g["total"]

    league_avg = sum(g["total"] for g in games) / len(games) if games else 8.90

    summary_header = [
        "Venue", "Games", "Avg Total", "Est Park Factor", "vs League Avg",
        "Model Factor", "New Games", "Until Check",
    ]
    summary_rows = [summary_header]
    # Sort by Est Park Factor descending — most hitter-friendly at top
    for venue, s in sorted(venue_stats.items(), key=lambda x: -(x[1]["total_runs"] / x[1]["games"])):
        g      = s["games"]
        avg    = round(s["total_runs"] / g, 2)
        factor = round((avg / league_avg) * 100, 1)
        delta  = round(factor - 100, 1)
        vs     = f"+{delta}%" if delta >= 0 else f"{delta}%"

        # Calibration progress columns
        team          = VENUE_TO_TEAM.get(venue, "")
        model_factor  = SPECIAL_VENUES.get(venue) or CURRENT_PARK_FACTORS.get(team, 100)
        if state and venue not in SPECIAL_VENUES:
            venue_state    = state.get(venue, {})
            games_at_calib = venue_state.get("games_at_calibration", g)
            new_games      = max(0, g - games_at_calib)
            until_check    = max(0, CALIBRATION_GAME_THRESHOLD - new_games)
        else:
            new_games   = ""
            until_check = ""

        summary_rows.append([venue, g, avg, factor, vs, model_factor, new_games, until_check])

    # Format vs League Avg column as TEXT; bold headers
    sheets_call(ws.format, "M1:M500", {"numberFormat": {"type": "TEXT"}})
    time.sleep(3)
    sheets_call(ws.update, data, value_input_option="USER_ENTERED")
    time.sleep(3)
    sheets_call(ws.update, summary_rows, "I1", value_input_option="RAW")
    time.sleep(3)
    sheets_call(ws.format, "A1:G1", {"textFormat": {"bold": True}})
    time.sleep(1)
    sheets_call(ws.format, "I1:P1", {"textFormat": {"bold": True}})

    return venue_stats, league_avg


# ── Calibration check ─────────────────────────────────────────────────────────
def run_calibration_check(venue_stats: dict, state: dict, savant_factors: dict) -> list[dict]:
    """
    For each venue with 20+ new games since last calibration:
      - Calculate blended factor (current x 0.7 + Savant x 0.3)
      - Flag if change >= 3 points
    Returns list of flag dicts for printing.
    """
    flags   = []
    checked = []
    today   = date.today().strftime("%Y-%m-%d")

    for venue, s in venue_stats.items():
        team = VENUE_TO_TEAM.get(venue)
        if not team:
            continue

        # Special venues (Vegas/Sacramento) tracked separately — skip here
        if venue in SPECIAL_VENUES:
            continue

        current_factor = CURRENT_PARK_FACTORS.get(team, 100)
        venue_state    = state.get(venue, {})
        games_at_calib = venue_state.get("games_at_calibration", s["games"])  # default: no new games
        new_games      = s["games"] - games_at_calib

        if new_games < CALIBRATION_GAME_THRESHOLD:
            continue  # not enough new games yet

        savant_factor = savant_factors.get(team)
        if not savant_factor:
            continue

        blended = round(current_factor * 0.7 + savant_factor * 0.3)
        delta   = blended - current_factor

        checked.append({
            "venue": venue, "team": team, "new_games": new_games,
            "current": current_factor, "savant": savant_factor,
            "blended": blended, "delta": delta,
        })

        if abs(delta) >= CALIBRATION_CHANGE_THRESHOLD:
            flags.append({
                "venue": venue, "team": team, "new_games": new_games,
                "current": current_factor, "savant": savant_factor,
                "blended": blended, "delta": delta,
            })

        # Update state — reset counter for this venue
        state[venue] = {
            "team":                  team,
            "games_at_calibration":  s["games"],
            "last_calibration_date": today,
            "factor_at_calibration": current_factor,
        }

    return flags, checked


# ── Main daily check (called from grade_bets.py or standalone) ───────────────
def daily_check(gc=None, verbose=True):
    if verbose:
        print("\n" + "=" * 60)
        print("Park Factor Tracker — Daily Update")
        print("=" * 60)

    # ── 1. Fetch all 2026 games ───────────────────────────────────────────────
    if verbose:
        print("\nFetching 2026 game data from MLB Stats API ...")
    games = fetch_all_2026_games()
    if verbose:
        print(f"  {len(games)} completed games loaded")

    if not games:
        if verbose:
            print("  No game data — skipping.")
        return

    # ── 2. Load or initialize state (needed before sheet update for progress columns) ──
    state = load_state()
    if not state:
        if verbose:
            print("\n  First run — initializing calibration state from current game counts.")
        state = initialize_state(games)
        save_state(state)
        if verbose:
            print("  State saved. Calibration counters start from today.")
            print("  (No flags will appear until 20 new games accumulate per venue.)")
        # Still write the sheet even on first run (no progress data yet)
        if gc is None:
            gc = auth()
        update_sheet(gc, games, state=state)
        return

    # ── 3. Update Google Sheet ────────────────────────────────────────────────
    if gc is None:
        gc = auth()
    if verbose:
        print("  Updating Park Factor Data tab ...")
    venue_stats, league_avg = update_sheet(gc, games, state=state)
    if verbose:
        print(f"  League avg total: {league_avg:.2f} runs/game")

    # ── 4. Fetch Savant factors ───────────────────────────────────────────────
    if verbose:
        print("\nFetching Savant 2026 park factors ...")
    savant_factors = fetch_savant_factors()
    if verbose:
        print(f"  {len(savant_factors)} teams loaded from Savant")

    if not savant_factors:
        if verbose:
            print("  Could not reach Savant — skipping calibration check.")
        return

    # ── 5. Run calibration check ──────────────────────────────────────────────
    flags, checked = run_calibration_check(venue_stats, state, savant_factors)
    save_state(state)

    # ── 6. Print results ──────────────────────────────────────────────────────
    if verbose:
        # Show progress toward threshold for all venues
        print("\nPark Factor Calibration Status:")
        print(f"  (Threshold: {CALIBRATION_GAME_THRESHOLD} new games + {CALIBRATION_CHANGE_THRESHOLD}+ point change to flag)\n")

        # Show venues that were checked (hit the 20-game threshold)
        if checked:
            print(f"  {'Venue':<35} {'New Games':>9} {'Current':>7} {'Savant':>6} {'Blended':>7} {'Delta':>6}  Status")
            print("  " + "-" * 90)
            for c in sorted(checked, key=lambda x: -abs(x["delta"])):
                sign   = f"+{c['delta']}" if c['delta'] > 0 else str(c['delta'])
                status = "⚑  REVIEW NEEDED" if abs(c['delta']) >= CALIBRATION_CHANGE_THRESHOLD else "within threshold"
                print(f"  {c['venue']:<35} {c['new_games']:>9} {c['current']:>7} {c['savant']:>6} {c['blended']:>7} {sign:>6}  {status}")

        # Show progress for venues not yet at threshold
        print(f"\n  Venues still accumulating (< {CALIBRATION_GAME_THRESHOLD} new games):")
        any_progress = False
        for venue, s in sorted(venue_stats.items(), key=lambda x: x[0]):
            team = VENUE_TO_TEAM.get(venue)
            if not team or venue in SPECIAL_VENUES:
                continue
            venue_state    = state.get(venue, {})
            games_at_calib = venue_state.get("games_at_calibration", s["games"])
            new_games      = s["games"] - games_at_calib
            if new_games < CALIBRATION_GAME_THRESHOLD:
                needed = CALIBRATION_GAME_THRESHOLD - new_games
                print(f"    {venue:<35}  {new_games:>2} new games  ({needed} more needed)")
                any_progress = True
        if not any_progress:
            print("    All venues have been checked.")

        # Summary of flags
        if flags:
            print(f"\n  *** {len(flags)} venue(s) flagged for calibration review ***")
            for f in flags:
                direction = "UP" if f['delta'] > 0 else "DOWN"
                print(f"  ⚑  {f['venue']} ({f['team']})")
                print(f"     Current factor: {f['current']}  →  Suggested: {f['blended']}  ({direction} {abs(f['delta'])} pts)")
                print(f"     Based on {f['new_games']} new games | Savant 2026: {f['savant']}")
        else:
            print("\n  No calibration changes flagged today.")


# ── Standalone entry point ────────────────────────────────────────────────────
if __name__ == "__main__":
    daily_check()
