"""
nfl_fetch_odds.py — Pull NFL odds from The Odds API and write to Google Sheets.
Run this each morning before nfl_analyze_edges.py.
"""

import requests
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timezone, timedelta
import os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# ── Config ────────────────────────────────────────────────────────────────────
ODDS_API_KEY   = os.environ["ODDS_API_KEY_NFL"]
NFL_SHEET_ID   = "1UPempH9iWF-DQFh5d26zjpft3-XLehp30PZPfE0tpsE"
CREDS_FILE     = os.path.join(os.path.dirname(__file__), "google_credentials.json")

BOOKS_TO_KEEP  = {"fanduel", "draftkings", "betmgm", "betrivers"}
BASE_URL       = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds"
EVENT_URL      = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events/{event_id}/odds"
COMMON_PARAMS  = {
    "apiKey":      ODDS_API_KEY,
    "regions":     "us",
    "oddsFormat":  "american",
    "dateFormat":  "iso",
}
TEAM_TOTAL_MARKETS  = "team_totals"

# Player prop markets — DO NOT enable until user confirms Odds API credits upgraded.
# The Odds API market keys for our six required prop categories + anytime TD:
#   player_pass_yds, player_pass_tds, player_rush_yds,
#   player_reception_yds, player_receptions, player_anytime_td
PLAYER_PROP_MARKETS = (
    "player_pass_yds,player_pass_tds,player_rush_yds,"
    "player_reception_yds,player_receptions,player_anytime_td"
)
FETCH_PLAYER_PROPS  = False  # flip to True only after explicit user confirmation

# NFL odds boards are posted months in advance (unlike MLB, which only ever
# returns a day or two of games). Per-event calls (team_totals, and props
# later) cost credits per game, so we only make them for games within this
# window — books haven't posted team-total markets for games further out
# anyway, and lines move a lot in the final week regardless.
EVENT_ODDS_WINDOW_DAYS = 7

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ── Google Sheets setup ───────────────────────────────────────────────────────
NFL_ODDS_HEADER = [
    "game_id", "home_team", "away_team", "commence_time",
    "sportsbook", "market_key", "name", "price", "point", "last_updated",
    "player", "direction",
]


def get_sheet(sheet_id: str, tab_name: str, header: list[str] | None = None):
    """Open a worksheet by tab name, creating it with a header row if missing."""
    creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
    gc    = gspread.authorize(creds)
    sh    = gc.open_by_key(sheet_id)
    try:
        return sh.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab_name, rows=2000, cols=max(20, len(header or []) + 2))
        if header:
            ws.update([header], value_input_option="USER_ENTERED")
        return ws


# ── Odds fetching ─────────────────────────────────────────────────────────────
def fetch_market(markets: str) -> tuple[list[dict], dict]:
    """Return (games_json, headers_dict) for one markets= request."""
    params = {**COMMON_PARAMS, "markets": markets}
    resp   = requests.get(BASE_URL, params=params, timeout=30)
    if resp.status_code == 422:
        print(f"  [skip] {markets} returned 422 — endpoint unavailable on free plan")
        return [], {}
    resp.raise_for_status()
    return resp.json(), resp.headers


def fetch_event_props(event_id: str, markets: str) -> tuple[dict, dict]:
    """Fetch per-event odds for the given markets. Returns (event_json, headers)."""
    url    = EVENT_URL.format(event_id=event_id)
    params = {**COMMON_PARAMS, "markets": markets}
    resp   = requests.get(url, params=params, timeout=30)
    if resp.status_code in (404, 422):
        return {}, {}
    resp.raise_for_status()
    return resp.json(), resp.headers


def parse_games(games: list[dict]) -> list[list]:
    """Flatten games JSON into rows matching the NFL Odds tab schema."""
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
    """Flatten per-event props/team_totals JSON into NFL Odds tab row format."""
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
    print("=" * 60)
    print("nfl_fetch_odds.py — Fantasy Six Pack NFL Odds Fetcher")
    print(f"Run time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    all_rows   = []
    last_hdrs  = {}
    all_games  = []  # keep raw game list to extract event IDs for team totals / props

    # NFL free-tier core markets. (No alternate_totals call — MLB pulls those too,
    # but NFL doesn't need alt total lines for our bet types and skipping saves credits.)
    print("\nFetching markets=h2h,spreads,totals ...")
    games, hdrs = fetch_market("h2h,spreads,totals")
    if games:
        all_rows = parse_games(games)
        last_hdrs = hdrs
        all_games = games
        print(f"  {len(games)} games -> {len(all_rows)} rows parsed")
    else:
        print("  0 games returned (normal in the off-season / bye weeks)")

    # ── Fetch per-event odds (team totals always; player props only when enabled) ─
    # Bounded to the next EVENT_ODDS_WINDOW_DAYS — see comment at top of file.
    now_utc     = datetime.now(timezone.utc)
    window_end  = now_utc + timedelta(days=EVENT_ODDS_WINDOW_DAYS)
    prop_rows   = []
    skipped     = 0
    out_of_window = 0

    markets_to_fetch = TEAM_TOTAL_MARKETS
    if FETCH_PLAYER_PROPS:
        markets_to_fetch += "," + PLAYER_PROP_MARKETS

    games_in_window = []
    for game in all_games:
        commence_raw = game.get("commence_time", "")
        try:
            commence_dt = datetime.fromisoformat(commence_raw.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            commence_dt = None
        if commence_dt and commence_dt <= now_utc:
            skipped += 1
            continue
        if commence_dt and commence_dt > window_end:
            out_of_window += 1
            continue
        games_in_window.append(game)

    if games_in_window:
        print(f"\nFetching per-event odds ({markets_to_fetch}) "
              f"for {len(games_in_window)} game(s) within {EVENT_ODDS_WINDOW_DAYS} days ...")
    if out_of_window:
        print(f"  Skipped {out_of_window} game(s) more than {EVENT_ODDS_WINDOW_DAYS} days out "
              f"(saves credits — team-total/prop markets aren't posted that early anyway)")

    for game in games_in_window:
        event_id = game.get("id", "")
        event_data, hdrs = fetch_event_props(event_id, markets_to_fetch)
        if event_data:
            rows = parse_prop_rows(event_data)
            prop_rows.extend(rows)
            last_hdrs = hdrs
            home = game.get("home_team", "")
            away = game.get("away_team", "")
            print(f"  {away} @ {home}: {len(rows)} rows")

    if skipped:
        print(f"  Skipped {skipped} game(s) already in progress")

    # ── Write everything to a single NFL Odds tab ─────────────────────────────
    print("\nConnecting to Google Sheets ...")
    ws_odds = get_sheet(NFL_SHEET_ID, "NFL Odds", header=NFL_ODDS_HEADER)
    ws_odds.clear()
    ws_odds.update([NFL_ODDS_HEADER] + all_rows + prop_rows, value_input_option="USER_ENTERED")
    print(f"  Wrote {len(all_rows)} game rows + {len(prop_rows)} prop rows to 'NFL Odds' tab")

    # ── API credit report ─────────────────────────────────────────────────────
    used      = last_hdrs.get("x-requests-used", "?")
    remaining = last_hdrs.get("x-requests-remaining", "?")
    print(f"\nAPI credits used: {used}  |  remaining: {remaining}")
    print("\nDone.")


if __name__ == "__main__":
    main()
