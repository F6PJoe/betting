"""
fetch_odds.py — Pull MLB odds from The Odds API and write to Google Sheets.
Run this each morning before analyze_edges.py.
"""

import requests
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timezone
import sys
import os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# ── Config ────────────────────────────────────────────────────────────────────
ODDS_API_KEY   = os.environ.get("ODDS_API_KEY", "")
if not ODDS_API_KEY:
    raise EnvironmentError("ODDS_API_KEY not set — check .env file in Betting Models folder")

LOG_FILE = os.path.join(os.path.dirname(__file__), "run_log.txt")
_log_fh  = None

def log(msg: str):
    print(msg)
    if _log_fh:
        _log_fh.write(msg + "\n")
        _log_fh.flush()
ODDS_SHEET_ID  = "1RaSm1ogJtNykM7WbYfQ3b9L7MUePcRBqlFMKuQfA_I4"
CREDS_FILE     = os.path.join(os.path.dirname(__file__), "google_credentials.json")

BOOKS_TO_KEEP  = {"fanduel", "draftkings", "betmgm", "betrivers"}
BASE_URL       = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
EVENT_URL      = "https://api.the-odds-api.com/v4/sports/baseball_mlb/events/{event_id}/odds"
COMMON_PARAMS  = {
    "apiKey":      ODDS_API_KEY,
    "regions":     "us",
    "oddsFormat":  "american",
    "dateFormat":  "iso",
}
TEAM_TOTAL_MARKETS  = "team_totals"
# Player prop markets — DO NOT enable until upgraded to paid API credits plan
# PLAYER_PROP_MARKETS = "pitcher_strikeouts,batter_total_bases,batter_home_runs,batter_hits_runs_rbis"
FETCH_PLAYER_PROPS  = True   # enabled 2026-07-09 after API upgrade to paid plan

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ── Google Sheets setup ───────────────────────────────────────────────────────
def get_sheet(sheet_id: str, tab_name: str):
    creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
    gc    = gspread.authorize(creds)
    return gc.open_by_key(sheet_id).worksheet(tab_name)


# ── Odds fetching ─────────────────────────────────────────────────────────────
def fetch_market(markets: str) -> tuple[list[dict], dict]:
    """Return (games_json, headers_dict) for one markets= request."""
    params = {**COMMON_PARAMS, "markets": markets}
    try:
        resp = requests.get(BASE_URL, params=params, timeout=30)
        if resp.status_code == 422:
            log(f"  [skip] {markets} returned 422 — endpoint unavailable on free plan")
            return [], {}
        resp.raise_for_status()
        return resp.json(), resp.headers
    except Exception as e:
        log(f"  [ERROR] fetch_market({markets}) failed: {e}")
        return [], {}


def fetch_event_props(event_id: str, markets: str) -> tuple[dict, dict]:
    """Fetch per-event odds for the given markets. Returns (event_json, headers)."""
    url    = EVENT_URL.format(event_id=event_id)
    params = {**COMMON_PARAMS, "markets": markets}
    try:
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code in (404, 422):
            return {}, {}
        resp.raise_for_status()
        return resp.json(), resp.headers
    except Exception as e:
        log(f"  [ERROR] fetch_event_props({event_id}) failed: {e}")
        return {}, {}


def parse_games(games: list[dict]) -> list[list]:
    """Flatten games JSON into rows matching the MLB Odds tab schema."""
    rows = []
    for game in games:
        game_id      = game.get("id", "")
        home_team    = game.get("home_team", "")
        away_team    = game.get("away_team", "")
        commence     = game.get("commence_time", "")
        last_updated = game.get("last_update", "")

        for book in game.get("bookmakers", []):
            book_key = book.get("key", "")
            if book_key not in BOOKS_TO_KEEP:
                continue
            for market in book.get("markets", []):
                market_key = market.get("key", "")
                for outcome in market.get("outcomes", []):
                    name      = outcome.get("name", "")
                    price     = outcome.get("price", "")
                    point     = outcome.get("point", "")
                    player    = outcome.get("description", "")  # player/team name for props
                    direction = ""  # Over/Under only relevant for total markets
                    rows.append([
                        game_id, home_team, away_team, commence,
                        book_key, market_key, name, price, point, last_updated,
                        player, direction,
                    ])
    return rows


def parse_prop_rows(event: dict) -> list[list]:
    """Flatten per-event props/team_totals JSON into MLB Odds tab row format."""
    rows = []
    game_id   = event.get("id", "")
    home_team = event.get("home_team", "")
    away_team = event.get("away_team", "")
    commence  = event.get("commence_time", "")
    for book in event.get("bookmakers", []):
        book_key = book.get("key", "")
        if book_key not in BOOKS_TO_KEEP:
            continue
        for market in book.get("markets", []):
            market_key = market.get("key", "")
            for outcome in market.get("outcomes", []):
                direction = outcome.get("name", "")         # "Over" / "Under"
                player    = outcome.get("description", "")  # player/team name
                price     = outcome.get("price", "")
                point     = outcome.get("point", "")
                rows.append([
                    game_id, home_team, away_team, commence,
                    book_key, market_key, player, price, point, "",
                    player, direction,
                ])
    return rows


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    global _log_fh
    run_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _log_fh = open(LOG_FILE, "a", encoding="utf-8")
    log("")
    log("=" * 60)
    log(f"fetch_odds.py run at {run_ts}")
    log("=" * 60)

    all_rows   = []
    last_hdrs  = {}
    all_games  = []  # keep raw game list to extract event IDs for props

    for markets in ["h2h,spreads,totals", "alternate_totals"]:
        log(f"\nFetching markets={markets} ...")
        games, hdrs = fetch_market(markets)
        if games:
            parsed = parse_games(games)
            all_rows.extend(parsed)
            last_hdrs = hdrs
            if not all_games:
                all_games = games  # save first batch for event IDs
            log(f"  {len(games)} games -> {len(parsed)} rows parsed")

    # ── Fetch per-event odds (team totals always; player props only when enabled) ─
    # Done BEFORE the Sheets write so we can merge everything into one tab write
    now_utc   = datetime.now(timezone.utc)
    prop_rows = []
    skipped   = 0

    markets_to_fetch = TEAM_TOTAL_MARKETS
    if FETCH_PLAYER_PROPS:
        markets_to_fetch += ",pitcher_strikeouts,batter_total_bases,batter_home_runs,batter_hits_runs_rbis"

    log(f"\nFetching per-event odds ({markets_to_fetch}) ...")
    for game in all_games:
        commence_raw = game.get("commence_time", "")
        try:
            commence_dt = datetime.fromisoformat(commence_raw.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            commence_dt = None
        if commence_dt and commence_dt <= now_utc:
            skipped += 1
            continue
        event_id = game.get("id", "")
        event_data, hdrs = fetch_event_props(event_id, markets_to_fetch)
        if event_data:
            rows = parse_prop_rows(event_data)
            prop_rows.extend(rows)
            last_hdrs = hdrs
            home = game.get("home_team", "")
            away = game.get("away_team", "")
            log(f"  {away} @ {home}: {len(rows)} rows")

    if skipped:
        log(f"  Skipped {skipped} game(s) already in progress")

    # ── Write everything to a single MLB Odds tab ─────────────────────────────
    log("\nConnecting to Google Sheets ...")
    ws_odds = get_sheet(ODDS_SHEET_ID, "MLB Odds")

    game_header = [
        "game_id", "home_team", "away_team", "commence_time",
        "sportsbook", "market_key", "name", "price", "point", "last_updated",
        "player", "direction",
    ]

    total_rows = len(all_rows) + len(prop_rows)
    if total_rows == 0:
        log("  [WARNING] No rows fetched from API — sheet NOT cleared to preserve existing data")
    else:
        try:
            ws_odds.clear()
            ws_odds.update([game_header] + all_rows + prop_rows, value_input_option="USER_ENTERED")
            log(f"  Wrote {len(all_rows)} game rows + {len(prop_rows)} prop rows to 'MLB Odds' tab")
        except Exception as e:
            log(f"  [ERROR] Sheet write failed: {e} — existing data preserved (clear may have already run)")

    # ── API credit report ─────────────────────────────────────────────────────
    used      = last_hdrs.get("x-requests-used", "?")
    remaining = last_hdrs.get("x-requests-remaining", "?")
    log(f"\nAPI credits used: {used}  |  remaining: {remaining}")
    log("\nDone.")
    if _log_fh:
        _log_fh.close()


if __name__ == "__main__":
    main()
