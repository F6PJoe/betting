"""
backfill_venues.py — One-time script to fill in Venue column for all
existing graded bets in Bet History and ML RL Shadow that are missing it.
Uses MLB Stats API historical schedule data to look up venues by date + teams.
"""

import requests
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import time
import os

# ── Config ────────────────────────────────────────────────────────────────────
ODDS_SHEET_ID = "1RaSm1ogJtNykM7WbYfQ3b9L7MUePcRBqlFMKuQfA_I4"
CREDS_FILE    = os.path.join(os.path.dirname(__file__), "google_credentials.json")
MLB_STATS_BASE = "https://statsapi.mlb.com/api/v1/schedule"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Team name variations the API might return vs what we store
TEAM_NAME_MAP = {
    "Oakland Athletics": "Athletics",
    "Oakland A's":       "Athletics",
}

# Values that look like old Confidence column data sitting in Venue column
# (happens when Venue was inserted before Confidence in the column order)
NON_VENUE_VALUES = {"normal", "high", "medium", "standard", "low", ""}

def normalize(name: str) -> str:
    return TEAM_NAME_MAP.get(name.strip(), name.strip())


# ── Auth ──────────────────────────────────────────────────────────────────────
def auth():
    creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


# ── Fetch venues for a given date ─────────────────────────────────────────────
def fetch_venues_for_date(date_str: str) -> dict:
    """
    Returns {(away_team, home_team): venue_name} for all games on date_str.
    """
    params = {"sportId": 1, "date": date_str, "gameType": "R", "hydrate": "venue,team"}
    try:
        resp = requests.get(MLB_STATS_BASE, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [API error for {date_str}: {e}]")
        return {}

    venue_map = {}
    for date_block in data.get("dates", []):
        for game in date_block.get("games", []):
            away  = normalize(game["teams"]["away"]["team"]["name"])
            home  = normalize(game["teams"]["home"]["team"]["name"])
            venue = game.get("venue", {}).get("name", "Unknown")
            venue_map[(away, home)] = venue
    return venue_map


# ── Parse game label → (away, home) ──────────────────────────────────────────
def parse_game(game_label: str):
    """Parse 'Away @ Home' into (away, home) tuple."""
    parts = game_label.split(" @ ")
    if len(parts) == 2:
        return normalize(parts[0].strip()), normalize(parts[1].strip())
    return None, None


# ── Backfill Bet History ──────────────────────────────────────────────────────
def backfill_history(ws_hist, venue_cache: dict) -> int:
    rows   = ws_hist.get_all_values()
    if len(rows) < 2:
        return 0

    header = rows[0]

    # Find column indices
    try:
        c_date  = header.index("Date")
        c_game  = header.index("Game")
        c_venue = header.index("Venue")
    except ValueError as e:
        print(f"  Bet History missing column: {e}")
        return 0

    updates = []
    dates_to_fetch = set()

    # First pass — find which dates we need
    for i, row in enumerate(rows[1:], start=2):
        while len(row) < len(header):
            row.append("")
        date  = row[c_date]
        venue = row[c_venue]
        if date and venue.lower() in NON_VENUE_VALUES:
            dates_to_fetch.add(date)

    # Fetch venue maps for all needed dates
    for date in sorted(dates_to_fetch):
        if date not in venue_cache:
            print(f"  Fetching venues for {date} ...")
            venue_cache[date] = fetch_venues_for_date(date)
            time.sleep(0.3)  # be gentle with the API

    # Second pass — match and queue updates
    for i, row in enumerate(rows[1:], start=2):
        while len(row) < len(header):
            row.append("")
        date  = row[c_date]
        venue = row[c_venue]
        game  = row[c_game]
        if not date or venue.lower() not in NON_VENUE_VALUES:
            continue
        away, home = parse_game(game)
        if not away:
            continue
        vmap  = venue_cache.get(date, {})
        found = vmap.get((away, home), "")
        if found:
            updates.append({"row": i, "col": c_venue + 1, "value": found})

    # Batch update
    for u in updates:
        ws_hist.update_cell(u["row"], u["col"], u["value"])
        time.sleep(0.1)

    return len(updates)


# ── Backfill ML RL Shadow ─────────────────────────────────────────────────────
def backfill_shadow(ws_shadow, venue_cache: dict) -> int:
    rows = ws_shadow.get_all_values()
    if len(rows) < 2:
        return 0

    header = rows[0]

    try:
        c_date  = header.index("Date")
        c_game  = header.index("Game")
        c_venue = header.index("Venue")
    except ValueError as e:
        print(f"  ML RL Shadow missing column: {e}")
        return 0

    updates       = []
    dates_needed  = set()

    for i, row in enumerate(rows[1:], start=2):
        while len(row) < len(header):
            row.append("")
        date  = row[c_date]
        venue = row[c_venue]
        if date and venue.lower() in NON_VENUE_VALUES:
            dates_needed.add(date)

    for date in sorted(dates_needed):
        if date not in venue_cache:
            print(f"  Fetching venues for {date} ...")
            venue_cache[date] = fetch_venues_for_date(date)
            time.sleep(0.3)

    for i, row in enumerate(rows[1:], start=2):
        while len(row) < len(header):
            row.append("")
        date  = row[c_date]
        venue = row[c_venue]
        game  = row[c_game]
        if not date or venue.lower() not in NON_VENUE_VALUES:
            continue
        away, home = parse_game(game)
        if not away:
            continue
        vmap  = venue_cache.get(date, {})
        found = vmap.get((away, home), "")
        if found:
            updates.append({"row": i, "col": c_venue + 1, "value": found})

    for u in updates:
        ws_shadow.update_cell(u["row"], u["col"], u["value"])
        time.sleep(0.1)

    return len(updates)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("backfill_venues.py — Venue Backfill")
    print("=" * 60)

    print("\nConnecting to Google Sheets ...")
    gc       = auth()
    sh       = gc.open_by_key(ODDS_SHEET_ID)
    ws_hist  = sh.worksheet("Bet History")
    ws_shadow = sh.worksheet("ML RL Shadow")

    venue_cache = {}  # shared cache so we don't double-fetch same dates

    print("\nBackfilling Bet History ...")
    hist_count = backfill_history(ws_hist, venue_cache)
    print(f"  {hist_count} rows updated")

    print("\nBackfilling ML RL Shadow ...")
    shadow_count = backfill_shadow(ws_shadow, venue_cache)
    print(f"  {shadow_count} rows updated")

    print(f"\nDone. {hist_count + shadow_count} total rows updated across both tabs.")


if __name__ == "__main__":
    main()
