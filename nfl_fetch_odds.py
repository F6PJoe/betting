"""
nfl_fetch_odds.py — Pull NFL odds from The Odds API and write to Google Sheets.
Run this each morning before nfl_analyze_edges.py.
"""

import requests
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timezone, timedelta
import os
import sys
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# ── Config ────────────────────────────────────────────────────────────────────
# Credits are pooled per ACCOUNT, not per sport — the 20,000/month plan is spendable
# on any sport. ODDS_API_KEY_NFL was a separate FREE 500-credit signup that predates
# the paid upgrade, and 500 will not cover a single NFL week once props are built
# (16 games x ~6 markets = ~96 credits per pass). Falls back to the old key only if
# the paid one is somehow absent.
ODDS_API_KEY   = os.environ.get("ODDS_API_KEY") or os.environ["ODDS_API_KEY_NFL"]
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

# ── Week scoping ─────────────────────────────────────────────────────────────
# The Odds API returns the ENTIRE regular season in one call — measured
# 2026-08-12: 272 events, Week 1 through Week 18, for a flat 3 credits.
# Without scoping, the model would track bets on January games in August, where
# the line still has five months of movement left in it. Useless as a bet and
# noise in the calibration set.
#
# We therefore scope to ONE WEEK, defaulting to the next week that hasn't
# kicked off yet. Weeks come from the official nflverse schedule rather than
# date arithmetic, because NFL "weeks" straddle Thu->Mon and a Monday-night
# kickoff lands on Tuesday in UTC — date maths gets that wrong at the boundary.
#
# Per-event calls (team_totals now, props later) are scoped to the same set, so
# cost is predictable: 3 flat + 1/game team totals = ~19 credits per pass, or
# ~115 with props. Override with --week N.
DEFAULT_WEEK_MODE = "upcoming"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Odds API full team names -> nflverse abbreviations. Kept here rather than
# imported so this script stays runnable on its own.
TEAM_NAME_TO_ABBR = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL",
    "Buffalo Bills": "BUF", "Carolina Panthers": "CAR", "Chicago Bears": "CHI",
    "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE", "Dallas Cowboys": "DAL",
    "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX",
    "Kansas City Chiefs": "KC", "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC",
    "Los Angeles Rams": "LA", "Miami Dolphins": "MIA", "Minnesota Vikings": "MIN",
    "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT",
    "San Francisco 49ers": "SF", "Seattle Seahawks": "SEA", "Tampa Bay Buccaneers": "TB",
    "Tennessee Titans": "TEN", "Washington Commanders": "WAS",
}


def build_week_lookup(season: int = 2026) -> dict:
    """{(home_abbr, away_abbr): week} from the official schedule."""
    import nfl_data_py as nfl_data
    sched = nfl_data.import_schedules([season])
    sched = sched[sched["game_type"] == "REG"]
    return {(r["home_team"], r["away_team"]): int(r["week"])
            for _, r in sched.iterrows()}


def resolve_target_week(games: list[dict], week_lookup: dict, requested=None) -> int | None:
    """
    Which week to fetch. An explicit --week wins; otherwise the lowest week
    that still has a game yet to kick off.
    """
    if requested is not None:
        return requested
    now = datetime.now(timezone.utc)
    upcoming = []
    for g in games:
        wk = week_lookup.get((TEAM_NAME_TO_ABBR.get(g.get("home_team")),
                              TEAM_NAME_TO_ABBR.get(g.get("away_team"))))
        if wk is None:
            continue
        try:
            kick = datetime.fromisoformat(g["commence_time"].replace("Z", "+00:00"))
        except (ValueError, TypeError, KeyError):
            continue
        if kick > now:
            upcoming.append(wk)
    return min(upcoming) if upcoming else None


def games_in_week(games: list[dict], week_lookup: dict, week: int) -> list[dict]:
    """Games belonging to the target week, per the official schedule.

    Doubles as the preseason/non-regular-season guard: anything absent from the
    schedule has no week and is dropped.
    """
    out = []
    for g in games:
        key = (TEAM_NAME_TO_ABBR.get(g.get("home_team")),
               TEAM_NAME_TO_ABBR.get(g.get("away_team")))
        if week_lookup.get(key) == week:
            out.append(g)
    return out

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

    # --week N to pin a week; --lines-only to skip per-event calls entirely
    # (the cheap game-day snapshot used purely to capture a closing line).
    requested_week = None
    for i, a in enumerate(sys.argv):
        if a == "--week" and i + 1 < len(sys.argv):
            requested_week = int(sys.argv[i + 1])
    lines_only = "--lines-only" in sys.argv

    last_hdrs = {}

    # One flat 3-credit call returns the whole season; we scope afterwards.
    print("\nFetching markets=h2h,spreads,totals ...")
    games, hdrs = fetch_market("h2h,spreads,totals")
    if not games:
        print("  0 games returned (normal in the off-season)")
        return
    last_hdrs = hdrs
    print(f"  {len(games)} games on the board (full season)")

    print("Loading official schedule for week mapping ...")
    week_lookup = build_week_lookup()
    target_week = resolve_target_week(games, week_lookup, requested_week)
    if target_week is None:
        print("  No upcoming scheduled games found — nothing to do.")
        return

    week_games = games_in_week(games, week_lookup, target_week)
    print(f"  Target: WEEK {target_week} — {len(week_games)} game(s)"
          + (f" (--week {requested_week} requested)" if requested_week else " (next un-played week)"))
    if not week_games:
        print("  No games matched that week.")
        return

    all_rows = parse_games(week_games)
    print(f"  {len(all_rows)} line rows parsed for week {target_week}")

    # ── Per-event odds, scoped to the same week ──────────────────────────────
    now_utc   = datetime.now(timezone.utc)
    prop_rows = []
    skipped   = 0

    markets_to_fetch = TEAM_TOTAL_MARKETS
    if FETCH_PLAYER_PROPS:
        markets_to_fetch += "," + PLAYER_PROP_MARKETS

    if lines_only:
        print("\n--lines-only: skipping per-event calls (closing-line snapshot mode)")
    else:
        to_fetch = []
        for game in week_games:
            try:
                kick = datetime.fromisoformat(game["commence_time"].replace("Z", "+00:00"))
            except (ValueError, TypeError, KeyError):
                kick = None
            if kick and kick <= now_utc:
                skipped += 1   # already started; last snapshot stands as its close
                continue
            to_fetch.append(game)

        print(f"\nFetching per-event odds ({markets_to_fetch}) for {len(to_fetch)} game(s) "
              f"~{len(to_fetch) * (1 + (6 if FETCH_PLAYER_PROPS else 0))} credits ...")
        for game in to_fetch:
            event_data, hdrs = fetch_event_props(game.get("id", ""), markets_to_fetch)
            if event_data:
                rows = parse_prop_rows(event_data)
                prop_rows.extend(rows)
                last_hdrs = hdrs
                print(f"  {game.get('away_team')} @ {game.get('home_team')}: {len(rows)} rows")
        if skipped:
            print(f"  Skipped {skipped} game(s) already in progress")

    # ── Write to the NFL Odds tab (working table for one week, not an archive —
    # the Line Log in nfl_bet_tracking.py is what preserves history) ──────────
    print("\nConnecting to Google Sheets ...")
    ws_odds = get_sheet(NFL_SHEET_ID, "NFL Odds", header=NFL_ODDS_HEADER)
    ws_odds.clear()
    ws_odds.update([NFL_ODDS_HEADER] + all_rows + prop_rows, value_input_option="USER_ENTERED")
    print(f"  Wrote {len(all_rows)} game rows + {len(prop_rows)} prop rows "
          f"for WEEK {target_week} to 'NFL Odds' tab")

    # ── API credit report ─────────────────────────────────────────────────────
    used      = last_hdrs.get("x-requests-used", "?")
    remaining = last_hdrs.get("x-requests-remaining", "?")
    print(f"\nAPI credits used: {used}  |  remaining: {remaining}")
    print("\nDone.")


if __name__ == "__main__":
    main()
