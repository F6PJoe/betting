"""
nfl_analyze_edges.py — NFL edge analysis model.
Reads odds from the NFL Odds tab, projects games/props, and writes
Edges / Bet History / Game Totals / ML Spread / Team Totals tabs.

STATUS: Step 3 (game-level projection math) is live — game totals, spreads,
moneylines, and team totals are all real. Player props + anytime TD are
still stubs (Task 5 / Phase 2), and gated behind FETCH_PLAYER_PROPS anyway.
"""

import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta, timezone
import math
import os
import sys

import nfl_data_py as nfl_data

# ── Config ────────────────────────────────────────────────────────────────────
NFL_SHEET_ID   = "1UPempH9iWF-DQFh5d26zjpft3-XLehp30PZPfE0tpsE"
# Joe Bond's organic "Draft Fantasy Football Projections" sheet — read-only
# (service account only has Viewer access here, which is all we need since
# we never write to it). Confirmed 2026-07-04 to refresh all season, not just
# preseason, so it's a valid ongoing input for prop projections in Step 3.
ORGANIC_SHEET_ID = "1HoxQZOsM0LFzHxEqCGv5yQJKa_ifdzasZoEHkMGVItQ"
CREDS_FILE     = os.path.join(os.path.dirname(__file__), "google_credentials.json")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

force = "--force" in sys.argv

# Lightweight mode for game-day CLV snapshots. Writes the Line Log and captures
# closing lines, and does NOTHING else: no team ratings, no projections, no
# nflverse play-by-play pull, no Edges rewrite, no Bet History upsert.
#
# Without this a Sunday's ~18 snapshots would each re-download a season of
# play-by-play and re-run the whole projection engine purely to append a few
# line quotes — slow, and it would also freeze new Bet History entry lines at
# arbitrary mid-afternoon moments rather than at the morning read.
snapshot_only = "--snapshot-only" in sys.argv

# Only record a CLV snapshot when a game is within this many hours of kickoff.
# Matches --only-if-kickoff-within on the fetch side; see the snapshot_only
# branch in main() for why both ends need it.
SNAPSHOT_KICKOFF_WINDOW_H = 6


# ── Google Sheets helpers ──────────────────────────────────────────────────────
def get_client():
    creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


def ws(gc, sheet_id: str, tab: str, header: list[str] | None = None):
    """Open a worksheet by tab name, creating it with a header row if missing."""
    sh = gc.open_by_key(sheet_id)
    try:
        return sh.worksheet(tab)
    except gspread.exceptions.WorksheetNotFound:
        w = sh.add_worksheet(title=tab, rows=2000, cols=max(20, len(header or []) + 2))
        if header:
            w.update([header], value_input_option="USER_ENTERED")
        return w


def sheet_to_dicts(worksheet) -> list[dict]:
    """
    Read a worksheet into a list of dicts keyed by its header row.

    UNFORMATTED_VALUE is deliberate: the default render returns DISPLAY
    strings, which carry whatever number format the cell happens to have
    drifted into. In the MLB model a percent-formatted cell rendered -113 as
    "-11300.00%" and int() threw — silently, on the CLV path. Anything we
    parse must come back as the underlying value, not its formatting.
    """
    values = worksheet.get_all_values(
        value_render_option=gspread.utils.ValueRenderOption.unformatted
    )
    if not values:
        return []
    header, rows = values[0], values[1:]
    return [dict(zip(header, row)) for row in rows]


# ── Static reference data: stadium roof / surface / location ─────────────────
# Built once from public team/venue info — doesn't change season to season,
# so this lives in code rather than a sheet tab per user's preference.
# roof: "dome" (fixed roof) | "retractable" | "outdoor"
# surface: "grass" | "turf"
# lat/lon: approximate stadium coordinates, used for NWS weather lookups
# (only meaningful for "outdoor"/"retractable" games — domes are weather-proof)
#
# NOTE: verify at start of season — a couple of stadiums are in flux:
#   BUF: new Highmark Stadium opened 2026 (outdoor, grass)
#   TEN: Titans still at old Nissan Stadium through 2026; new dome opens 2027
NFL_STADIUMS = {
    "BUF": {"name": "Highmark Stadium",             "roof": "outdoor",     "surface": "grass", "lat": 42.7738, "lon": -78.7870, "tz": "America/New_York"},
    "MIA": {"name": "Hard Rock Stadium",             "roof": "outdoor",     "surface": "grass", "lat": 25.9580, "lon": -80.2389, "tz": "America/New_York"},
    "NE":  {"name": "Gillette Stadium",              "roof": "outdoor",     "surface": "turf",  "lat": 42.0909, "lon": -71.2643, "tz": "America/New_York"},
    "NYJ": {"name": "MetLife Stadium",               "roof": "outdoor",     "surface": "turf",  "lat": 40.8135, "lon": -74.0745, "tz": "America/New_York"},
    "BAL": {"name": "M&T Bank Stadium",              "roof": "outdoor",     "surface": "grass", "lat": 39.2780, "lon": -76.6227, "tz": "America/New_York"},
    "CIN": {"name": "Paycor Stadium",                "roof": "outdoor",     "surface": "turf",  "lat": 39.0955, "lon": -84.5160, "tz": "America/New_York"},
    "CLE": {"name": "Huntington Bank Field",         "roof": "outdoor",     "surface": "grass", "lat": 41.5061, "lon": -81.6995, "tz": "America/New_York"},
    "PIT": {"name": "Acrisure Stadium",              "roof": "outdoor",     "surface": "grass", "lat": 40.4468, "lon": -80.0158, "tz": "America/New_York"},
    "HOU": {"name": "NRG Stadium",                   "roof": "retractable", "surface": "turf",  "lat": 29.6847, "lon": -95.4107, "tz": "America/Chicago"},
    "IND": {"name": "Lucas Oil Stadium",             "roof": "retractable", "surface": "turf",  "lat": 39.7601, "lon": -86.1639, "tz": "America/Indiana/Indianapolis"},
    "JAX": {"name": "EverBank Stadium",              "roof": "outdoor",     "surface": "grass", "lat": 30.3239, "lon": -81.6373, "tz": "America/New_York"},
    "TEN": {"name": "Nissan Stadium",                "roof": "outdoor",     "surface": "grass", "lat": 36.1665, "lon": -86.7713, "tz": "America/Chicago"},
    "DEN": {"name": "Empower Field at Mile High",    "roof": "outdoor",     "surface": "grass", "lat": 39.7439, "lon": -105.0201, "tz": "America/Denver"},
    "KC":  {"name": "GEHA Field at Arrowhead Stadium","roof": "outdoor",    "surface": "grass", "lat": 39.0489, "lon": -94.4839, "tz": "America/Chicago"},
    "LV":  {"name": "Allegiant Stadium",             "roof": "dome",        "surface": "turf",  "lat": 36.0909, "lon": -115.1833, "tz": "America/Los_Angeles"},
    "LAC": {"name": "SoFi Stadium",                  "roof": "dome",        "surface": "turf",  "lat": 33.9535, "lon": -118.3392, "tz": "America/Los_Angeles"},
    "DAL": {"name": "AT&T Stadium",                  "roof": "retractable", "surface": "turf",  "lat": 32.7473, "lon": -97.0945, "tz": "America/Chicago"},
    "NYG": {"name": "MetLife Stadium",               "roof": "outdoor",     "surface": "turf",  "lat": 40.8135, "lon": -74.0745, "tz": "America/New_York"},
    "PHI": {"name": "Lincoln Financial Field",       "roof": "outdoor",     "surface": "grass", "lat": 39.9008, "lon": -75.1675, "tz": "America/New_York"},
    "WAS": {"name": "Northwest Stadium",             "roof": "outdoor",     "surface": "grass", "lat": 38.9077, "lon": -76.8645, "tz": "America/New_York"},
    "CHI": {"name": "Soldier Field",                 "roof": "outdoor",     "surface": "grass", "lat": 41.8623, "lon": -87.6167, "tz": "America/Chicago"},
    "DET": {"name": "Ford Field",                    "roof": "dome",        "surface": "turf",  "lat": 42.3400, "lon": -83.0456, "tz": "America/Detroit"},
    "GB":  {"name": "Lambeau Field",                 "roof": "outdoor",     "surface": "grass", "lat": 44.5013, "lon": -88.0622, "tz": "America/Chicago"},
    "MIN": {"name": "U.S. Bank Stadium",             "roof": "dome",        "surface": "turf",  "lat": 44.9738, "lon": -93.2575, "tz": "America/Chicago"},
    "ATL": {"name": "Mercedes-Benz Stadium",         "roof": "retractable", "surface": "turf",  "lat": 33.7554, "lon": -84.4008, "tz": "America/New_York"},
    "CAR": {"name": "Bank of America Stadium",       "roof": "outdoor",     "surface": "grass", "lat": 35.2258, "lon": -80.8528, "tz": "America/New_York"},
    "NO":  {"name": "Caesars Superdome",             "roof": "dome",        "surface": "turf",  "lat": 29.9511, "lon": -90.0812, "tz": "America/Chicago"},
    "TB":  {"name": "Raymond James Stadium",         "roof": "outdoor",     "surface": "grass", "lat": 27.9759, "lon": -82.5033, "tz": "America/New_York"},
    "ARI": {"name": "State Farm Stadium",            "roof": "retractable", "surface": "grass", "lat": 33.5276, "lon": -112.2626, "tz": "America/Phoenix"},
    "LA":  {"name": "SoFi Stadium",                  "roof": "dome",        "surface": "turf",  "lat": 33.9535, "lon": -118.3392, "tz": "America/Los_Angeles"},
    "SF":  {"name": "Levi's Stadium",                "roof": "outdoor",     "surface": "grass", "lat": 37.4030, "lon": -121.9700, "tz": "America/Los_Angeles"},
    "SEA": {"name": "Lumen Field",                   "roof": "outdoor",     "surface": "turf",  "lat": 47.5952, "lon": -122.3316, "tz": "America/Los_Angeles"},
}


def weather_relevant(team_abbr: str) -> bool:
    """Domes and fixed/retractable-closed roofs don't need a weather pull."""
    info = NFL_STADIUMS.get(team_abbr, {})
    return info.get("roof") == "outdoor"


# Odds API team names -> nflverse team abbreviations (nflverse's own
# canonical list, pulled via import_team_desc() — note nflverse uses "LA"
# for the Rams, not "LAR").
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


# ── Core model constants (all Year-1 "best current guess" — see comments) ────
# Home field advantage, in points added to the home team's projected score.
# Modern-NFL research (2015-2025 data) shows HFA has shrunk from the historic
# ~2.5-3 pts to roughly 1-1.5 pts league-wide (crowd noise/travel effects
# matter less than they used to). We use the conservative end of that range.
HOME_FIELD_ADV = 1.5

# Standard deviation of final score margin (home - away), used to convert a
# projected margin into a win probability via the normal CDF. ~13.5 points is
# a widely cited figure in point-spread market efficiency literature (e.g.
# it implies roughly 3% win-prob shift per point near a pick'em game, which
# matches how sportsbooks price short spreads).
MARGIN_STD_DEV = 13.5

# ── Prior-season regression (shrinkage toward league mean) ───────────────────
# With only one full season (2025) as a prior and zero 2026 games played yet,
# team PPG scored/allowed is a noisy estimate of "true" team strength. We
# shrink each team's rate toward the league average using a pseudo-games
# constant: blended = (n*team_avg + K*league_avg) / (n + K).
# K=6 means a full 17-game season is trusted at 17/(17+6) = 74% weight vs the
# league mean — a moderate shrink, larger than a typical in-season update
# would need, because a full offseason of roster/scheme change (draft, free
# agency, coaching hires) isn't captured by 2025 stats at all. This is a
# beta-season starting point, not a validated constant.
PRIOR_SEASON_SHRINK_K = 6

# How many "pseudo-games" of trust the 2025 prior keeps once 2026 games start
# accumulating. Blended team rate = (games_2026*current + PRIOR_WEIGHT*prior)
# / (games_2026 + PRIOR_WEIGHT). PRIOR_WEIGHT=8 means the prior carries equal
# weight to 8 games of fresh 2026 data — i.e. by roughly Week 8 the current
# season has caught up to the prior, and by season's end (17 games) current-
# season data dominates (17/25 = 68%). This mirrors how public power-ranking
# systems fade a preseason prior over the course of a season. Flagged for
# recalibration off the Performance tab once real 2026 results exist.
CURRENT_SEASON_PRIOR_WEIGHT = 8

# Rest-day adjustments (points added/subtracted from a team's projected
# score). Effect sizes are small and debated in modern analytics, so these
# are conservative placeholders pending real-season calibration.
SHORT_WEEK_PENALTY = -1.0   # rest < 6 days (e.g. Thursday game off a Sunday)
BYE_WEEK_BONUS     = 1.0    # rest >= 13 days (coming off a bye)

# Weather: outdoor games only (see weather_relevant()). Wind hurts passing/
# kicking efficiency; below ~10mph the effect is negligible in most public
# research, so we only start penalizing the projected total above that.
WIND_PENALTY_PER_MPH = -0.1   # per mph of wind above the threshold, on total
WIND_THRESHOLD_MPH   = 10.0
WIND_MAX_PENALTY     = -3.0   # cap so one extreme reading doesn't dominate

# ── Star-rating unit scales (Year 1 beta — NOT yet performance-validated) ────
# Same architecture as the MLB model: unit_scale() linearly interpolates
# units (0.3-1.0) between anchor points, stars_from_units() buckets units
# into 3/4/5 stars. Edges below the first anchor point aren't shown at all
# (not a 1-2 star tier — just not actionable). Thresholds below are initial
# estimates based on typical NFL market efficiency (spreads are the sharpest/
# most efficient market, totals a bit looser, team totals looser still) —
# recalibrate off the Performance tab once ~30+ graded bets per type exist,
# same as the MLB TOTAL_SCALE recalibration on 2026-06-14.
GAME_TOTAL_SCALE = [
    (3.0, 0.3), (4.0, 0.4), (5.0, 0.5), (5.5, 0.6),
    (6.0, 0.7), (6.5, 0.8), (7.0, 0.9), (8.0, 1.0),
]
SPREAD_SCALE = [
    (2.0, 0.3), (2.75, 0.4), (3.5, 0.5), (4.0, 0.6),
    (4.5, 0.7), (5.0, 0.8), (5.5, 0.9), (6.5, 1.0),
]
ML_SCALE = [
    (4.0, 0.3), (6.0, 0.4), (8.0, 0.5), (10.0, 0.6),
    (12.0, 0.7), (14.0, 0.8), (16.0, 0.9), (20.0, 1.0),
]
TEAM_TOTAL_SCALE_NFL = [
    (2.0, 0.3), (2.75, 0.4), (3.5, 0.5), (4.0, 0.6),
    (4.5, 0.7), (5.0, 0.8), (5.5, 0.9), (6.5, 1.0),
]


def american_to_implied(price: float) -> float:
    """Convert American odds to implied probability (0-1)."""
    price = float(price)
    if price >= 100:
        return 100 / (price + 100)
    return abs(price) / (abs(price) + 100)


def normal_cdf(x: float, mean: float = 0.0, sd: float = 1.0) -> float:
    """Standard normal CDF via math.erf (matches the MLB model's approach —
    no scipy dependency needed)."""
    z = (x - mean) / (sd * math.sqrt(2))
    return 0.5 * (1 + math.erf(z))


def unit_scale(edge: float, scale_points: list[tuple]) -> float:
    """Interpolate units from a list of (edge_threshold, units) pairs."""
    scale_points = sorted(scale_points, key=lambda x: x[0])
    if edge <= scale_points[0][0]:
        return scale_points[0][1]
    if edge >= scale_points[-1][0]:
        return scale_points[-1][1]
    for i in range(len(scale_points) - 1):
        lo_e, lo_u = scale_points[i]
        hi_e, hi_u = scale_points[i + 1]
        if lo_e <= edge <= hi_e:
            t = (edge - lo_e) / (hi_e - lo_e)
            return round(lo_u + t * (hi_u - lo_u), 1)


def stars_from_units(units: float) -> int:
    if units >= 0.7:
        return 5
    if units >= 0.5:
        return 4
    if units >= 0.3:
        return 3
    return 0


def stars_emoji(n: int) -> str:
    return "⭐" * n


# ── Organic sheet reader (Joe Bond's consensus fantasy projections) ──────────
def load_organic_projections(gc) -> dict:
    """
    Read the blended consensus season projections from Joe's
    'Draft Fantasy Football Projections' sheet (LIVE PROJECTIONS tabs).
    Returns {player_name: {stat_dict}} per position group.

    Step 3 will convert these season totals to per-game baselines (÷ ~17
    games) and blend them with in-season nflverse actuals as the season
    progresses (recency-weighted, same philosophy as MLB park factors).
    """
    sh = gc.open_by_key(ORGANIC_SHEET_ID)
    projections = {}
    for pos, tab in [
        ("QB", "LIVE PROJECTIONS QB"),
        ("RB", "LIVE PROJECTIONS RB"),
        ("WR", "LIVE PROJECTIONS WR"),
        ("TE", "LIVE PROJECTIONS TE"),
    ]:
        try:
            rows = sheet_to_dicts(sh.worksheet(tab))
        except gspread.exceptions.WorksheetNotFound:
            print(f"  [warn] '{tab}' not found in organic sheet — skipping {pos}")
            continue
        for row in rows:
            name = row.get(pos, "").strip()
            if name:
                projections[name] = {"position": pos, **row}
    return projections


def _shrink(team_avg: float, league_avg: float, n_games: int, k: float) -> float:
    """Shrinkage toward league mean: (n*team_avg + k*league_avg) / (n+k)."""
    if n_games <= 0:
        return league_avg
    return (n_games * team_avg + k * league_avg) / (n_games + k)


# How many passes of the opponent adjustment to run. Each pass re-rates every
# team against the CURRENT estimate of its opponents, so the ratings settle on
# a mutually consistent solution. 10 is well past convergence for a 32-team,
# 17-game schedule (movement is <0.01 pts by pass 6) and costs nothing.
OPPONENT_ADJUST_PASSES = 10


def opponent_adjust(stats: dict, league_avg: float,
                    passes: int = OPPONENT_ADJUST_PASSES) -> tuple[dict, dict]:
    """
    Convert raw points-scored / points-allowed into OPPONENT-ADJUSTED ratings.

    WHY THIS EXISTS: raw points-allowed punishes a team for its schedule. The
    2025 Jets allowed 27.87 per game against a 23.01 league average, which the
    additive model read as "bad defense" full stop — but a chunk of that was
    facing good offenses. Playing a weak Titans offence, they will not allow
    27.87. Unadjusted, that inflated the projected Titans score to 23.86
    against their own 18.35 baseline, which became a 1.21 game-script factor,
    which lifted EVERY Titans player prop by 21% — putting Cam Ward's pass
    yards 70 above the book's line and NYJ@TEN into the top three plays on the
    board. One biased team rating contaminated an entire roster.

    Method (standard iterative / SRS-style):
        adj_off[t] = mean over t's games of (points scored + (opp adj_def - league_avg))
        adj_def[t] = mean over t's games of (points allowed + (opp adj_off - league_avg))

    Read the sign as: scoring 20 against a defence that normally allows 17 is
    worth MORE than scoring 20 against one that allows 30. Both series are
    seeded with the raw rates and refined together until they agree.

    Returns (adj_off, adj_def), still in points-per-game so everything
    downstream (projections, script factor) is unchanged in units.
    """
    teams = list(stats)
    adj_off = {t: (sum(stats[t]["scored"]) / len(stats[t]["scored"])
                   if stats[t]["scored"] else league_avg) for t in teams}
    adj_def = {t: (sum(stats[t]["allowed"]) / len(stats[t]["allowed"])
                   if stats[t]["allowed"] else league_avg) for t in teams}

    for _ in range(passes):
        new_off, new_def = {}, {}
        for t in teams:
            d = stats[t]
            if not d["scored"]:
                new_off[t], new_def[t] = league_avg, league_avg
                continue
            off_vals, def_vals = [], []
            for scored, allowed, opp in zip(d["scored"], d["allowed"], d["opp"]):
                # credit for the defence faced / debit for the offence faced
                off_vals.append(scored + (adj_def.get(opp, league_avg) - league_avg))
                def_vals.append(allowed + (adj_off.get(opp, league_avg) - league_avg))
            new_off[t] = sum(off_vals) / len(off_vals)
            new_def[t] = sum(def_vals) / len(def_vals)
        adj_off, adj_def = new_off, new_def

    return adj_off, adj_def


def load_team_stats(prior_season: int = 2025, current_season: int = 2026) -> dict:
    """
    Team offense/defense power ratings, expressed in points per game.

    Baseline: prior_season (2025) regular-season points scored/allowed per
    team, shrunk toward the league mean (PRIOR_SEASON_SHRINK_K — see comment
    above) since a full offseason of roster/scheme turnover isn't reflected
    in last year's stats.

    Once current_season games exist (none yet — 2026 season hasn't started),
    this blends them in via CURRENT_SEASON_PRIOR_WEIGHT so the model shifts
    from the 2025 prior toward real 2026 results as the season progresses.

    Returns {team_abbr: {"off_rating": pts/gm, "def_rating": pts allowed/gm,
                          "games_prior": n, "games_current": n}}.
    """
    sched = nfl_data.import_schedules([prior_season, current_season])
    sched = sched[sched["game_type"] == "REG"]

    prior = sched[sched["season"] == prior_season]
    current = sched[(sched["season"] == current_season) & (sched["home_score"].notna())]

    def _team_pgstats(df):
        """{team: {"scored": [...], "allowed": [...], "opp": [...]}}.

        `opp` is parallel to the score lists so each game can be adjusted for
        WHO it was played against — see opponent_adjust().
        """
        out = {}
        for _, g in df.iterrows():
            home, away = g["home_team"], g["away_team"]
            hs, as_ = g["home_score"], g["away_score"]
            for t, own, opp_pts, opp in ((home, hs, as_, away), (away, as_, hs, home)):
                out.setdefault(t, {"scored": [], "allowed": [], "opp": []})
                out[t]["scored"].append(own)
                out[t]["allowed"].append(opp_pts)
                out[t]["opp"].append(opp)
        return out

    prior_stats = _team_pgstats(prior)
    current_stats = _team_pgstats(current) if len(current) else {}

    league_avg_prior = prior["home_score"].mean() * 0.5 + prior["away_score"].mean() * 0.5

    # OPPONENT-ADJUST BEFORE SHRINKING. Order matters: shrinking first would
    # pull a team toward the mean using a schedule-contaminated number, and the
    # adjustment would then be working on an already-distorted rate.
    prior_adj_off, prior_adj_def = opponent_adjust(prior_stats, league_avg_prior)
    if current_stats:
        cur_avg = (current["home_score"].mean() * 0.5 + current["away_score"].mean() * 0.5)
        cur_adj_off, cur_adj_def = opponent_adjust(current_stats, cur_avg)
    else:
        cur_adj_off, cur_adj_def = {}, {}

    ratings = {}
    for abbr in TEAM_NAME_TO_ABBR.values():
        p = prior_stats.get(abbr, {"scored": [], "allowed": []})
        n_prior = len(p["scored"])
        prior_off = prior_adj_off.get(abbr, league_avg_prior)
        prior_def = prior_adj_def.get(abbr, league_avg_prior)
        prior_off = _shrink(prior_off, league_avg_prior, n_prior, PRIOR_SEASON_SHRINK_K)
        prior_def = _shrink(prior_def, league_avg_prior, n_prior, PRIOR_SEASON_SHRINK_K)

        c = current_stats.get(abbr, {"scored": [], "allowed": []})
        n_current = len(c["scored"])
        if n_current:
            current_off = cur_adj_off.get(abbr, league_avg_prior)
            current_def = cur_adj_def.get(abbr, league_avg_prior)
            off_rating = (n_current * current_off + CURRENT_SEASON_PRIOR_WEIGHT * prior_off) / \
                         (n_current + CURRENT_SEASON_PRIOR_WEIGHT)
            def_rating = (n_current * current_def + CURRENT_SEASON_PRIOR_WEIGHT * prior_def) / \
                         (n_current + CURRENT_SEASON_PRIOR_WEIGHT)
        else:
            off_rating, def_rating = prior_off, prior_def

        ratings[abbr] = {
            "off_rating": round(off_rating, 2),
            "def_rating": round(def_rating, 2),
            "games_prior": n_prior,
            "games_current": n_current,
        }

    ratings["_league_avg"] = round(league_avg_prior, 2)
    return ratings


def load_qb_depth_charts() -> dict:
    """TODO Phase 2 (player props): nfl_data_py import_depth_charts()/
    import_weekly_rosters() — confirm starting QB per team each week.
    Not needed for the game-level model (totals/spread/ML/team totals use
    team-level power ratings, not QB-specific splits)."""
    return {}


def load_injuries() -> dict:
    """TODO Phase 2 (player props): nfl_data_py import_injuries() —
    practice/game status reports. Treat 'Questionable' as a projection-risk
    flag, not a hard out."""
    return {}


def fetch_nws_forecast(lat: float, lon: float) -> dict | None:
    """One-shot NWS forecast lookup for a stadium. Two calls: points ->
    gridpoint, then the hourly forecast for that gridpoint. No API key
    needed, but NWS requires an identifying User-Agent header."""
    import requests
    headers = {"User-Agent": "FantasySixPack (f6pprojects@gmail.com)"}
    try:
        pt = requests.get(f"https://api.weather.gov/points/{lat},{lon}", headers=headers, timeout=10)
        pt.raise_for_status()
        hourly_url = pt.json()["properties"]["forecastHourly"]
        fc = requests.get(hourly_url, headers=headers, timeout=10)
        fc.raise_for_status()
        return fc.json()["properties"]["periods"]
    except Exception as e:
        print(f"  [weather] NWS lookup failed for ({lat},{lon}): {e}")
        return None


def load_weather(games_by_id: dict, window_days: int = 7) -> dict:
    """
    Weather forecast for outdoor-stadium games within window_days (NWS
    forecasts aren't reliable further out, and it matches the same window
    nfl_fetch_odds.py uses for per-event odds calls).

    Returns {game_id: {"wind_mph": x, "temp_f": y}}. Retractable-roof
    stadiums are treated as weather-neutral in v1 — operators overwhelmingly
    close the roof in bad weather, so assuming no effect is a reasonable
    simplification rather than trying to predict roof status ourselves.
    """
    now_utc = datetime.now(timezone.utc)
    window_end = now_utc + timedelta(days=window_days)
    out = {}
    for game_id, g in games_by_id.items():
        home_abbr = TEAM_NAME_TO_ABBR.get(g["home_team"])
        if not home_abbr or not weather_relevant(home_abbr):
            continue
        try:
            commence_dt = datetime.fromisoformat(g["commence_time"].replace("Z", "+00:00"))
        except (ValueError, TypeError, KeyError):
            continue
        if not (now_utc < commence_dt <= window_end):
            continue
        stadium = NFL_STADIUMS[home_abbr]
        periods = fetch_nws_forecast(stadium["lat"], stadium["lon"])
        if not periods:
            continue
        # Find the forecast period closest to kickoff
        best = min(periods, key=lambda p: abs(
            datetime.fromisoformat(p["startTime"]) - commence_dt.astimezone()
        ))
        wind_str = best.get("windSpeed", "0 mph")
        try:
            wind_mph = float(wind_str.split()[0])
        except (ValueError, IndexError):
            wind_mph = 0.0
        out[game_id] = {"wind_mph": wind_mph, "temp_f": best.get("temperature")}
    return out


def analyze_player_props(prop_odds, games) -> list:
    """TODO Phase 2 (Task 5) — NOT called until FETCH_PLAYER_PROPS is enabled
    in nfl_fetch_odds.py and the user confirms the credit upgrade."""
    return []


# ── Odds grouping ──────────────────────────────────────────────────────────────
def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def group_odds_by_game(odds_rows: list[dict]) -> dict:
    """Group flat NFL Odds tab rows into one structure per game_id, with
    per-book quotes for each market. See nfl_fetch_odds.py's NFL_ODDS_HEADER
    for the source column layout."""
    games = {}
    for row in odds_rows:
        gid = row.get("game_id")
        if not gid:
            continue
        g = games.setdefault(gid, {
            "home_team": row.get("home_team"), "away_team": row.get("away_team"),
            "commence_time": row.get("commence_time"),
            "h2h": {}, "spreads": {}, "totals": {}, "team_totals": {},
        })
        mk, book, name = row.get("market_key"), row.get("sportsbook"), row.get("name")
        price, point = _to_float(row.get("price")), _to_float(row.get("point"))
        if price is None:
            continue

        if mk == "h2h":
            d = g["h2h"].setdefault(book, {})
            if name == g["home_team"]:
                d["home_price"] = price
            elif name == g["away_team"]:
                d["away_price"] = price
        elif mk == "spreads":
            d = g["spreads"].setdefault(book, {})
            if name == g["home_team"]:
                d["home_price"], d["home_point"] = price, point
            elif name == g["away_team"]:
                d["away_price"], d["away_point"] = price, point
        elif mk == "totals":
            d = g["totals"].setdefault(book, {})
            if name == "Over":
                d["over_price"], d["point"] = price, point
            elif name == "Under":
                d["under_price"], d["point"] = price, point
        elif mk == "team_totals":
            direction = row.get("direction")
            side = "home" if name == g["home_team"] else "away" if name == g["away_team"] else None
            if side:
                d = g["team_totals"].setdefault(book, {})
                if direction == "Over":
                    d[f"{side}_over_price"], d[f"{side}_point"] = price, point
                elif direction == "Under":
                    d[f"{side}_under_price"], d[f"{side}_point"] = price, point
    return games


def build_rest_lookup(current_season: int = 2026) -> dict:
    """{(home_abbr, away_abbr): {"home_rest": n, "away_rest": n}} from the
    nflverse/habitatring schedule feed, which has rest days pre-computed for
    the full season (including future games — rest is just schedule math,
    no game results needed)."""
    sched = nfl_data.import_schedules([current_season])
    lookup = {}
    for _, g in sched.iterrows():
        lookup[(g["home_team"], g["away_team"])] = {
            "home_rest": int(g["home_rest"]) if g["home_rest"] == g["home_rest"] else None,
            "away_rest": int(g["away_rest"]) if g["away_rest"] == g["away_rest"] else None,
        }
    return lookup


def filter_to_scheduled_games(games_by_id: dict, rest_lookup: dict) -> tuple[dict, list]:
    """
    Keep only games that appear on the official nflverse schedule.

    PRESEASON GUARD. Sportsbooks post preseason lines, and nothing else in this
    model would notice: `project_game_score()` would happily rate a preseason
    game off regular-season team ratings, even though starters play a series or
    two and the result is close to noise relative to team quality. That would
    emit real-looking bets on games the model has no business pricing.

    `rest_lookup` is built from `import_schedules()` filtered to REG, so
    membership in it is exactly "this is a real regular-season game".

    NOTE: when playoff betting is added, widen the schedule filter in
    build_rest_lookup() to include POST or this will silently drop playoff
    games too.
    """
    keep, dropped = {}, []
    for game_id, g in games_by_id.items():
        home = TEAM_NAME_TO_ABBR.get(g.get("home_team"))
        away = TEAM_NAME_TO_ABBR.get(g.get("away_team"))
        if home and away and (home, away) in rest_lookup:
            keep[game_id] = g
        else:
            dropped.append(f"{g.get('away_team')} @ {g.get('home_team')}")
    return keep, dropped


def _fmt_time_et(commence_iso: str) -> str:
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(commence_iso.replace("Z", "+00:00"))
        dt_et = dt.astimezone(ZoneInfo("America/New_York"))
        s = dt_et.strftime("%I:%M %p ET")
        return s.lstrip("0")
    except Exception:
        return ""


def best_available(quotes_by_line: dict, prefer: str):
    """
    Pick the best REAL, BETTABLE line — never an average across books.

    Averaging book lines produces numbers no book offers (a 3.5/3.5/4/4 spread
    consensus is 3.75; totals came out at 38.8 and 42.1). You cannot place that
    bet and you cannot grade it honestly, and recording a line nobody posted is
    the same family of error as MLB's CLV bug #1.

    Line beats juice when they conflict: a half point is worth far more than a
    few cents, especially near key numbers. So we take the most favourable LINE
    first, then the best price among the books actually offering it.

    prefer: "low"  -> smaller number is better for us (Over a total)
            "high" -> larger number is better for us (Under a total; any spread,
                      since +10.5 beats +10 and -3 beats -3.5 alike)

    quotes_by_line: {line_float: {book: price}}
    Returns (line, price, book, n_books_at_that_line) or (None, None, None, 0).
    """
    if not quotes_by_line:
        return None, None, None, 0
    line = (min if prefer == "low" else max)(quotes_by_line)
    quotes = quotes_by_line[line]
    book, price = max(quotes.items(), key=lambda kv: kv[1])
    return line, price, book, len(quotes)


def _totals_by_line(books: dict, price_key: str) -> dict:
    out = {}
    for book, v in books.items():
        if v.get("point") is None or v.get(price_key) is None:
            continue
        out.setdefault(float(v["point"]), {})[book] = v[price_key]
    return out


def _spreads_by_line(books: dict, side_key: str) -> dict:
    out = {}
    for book, v in books.items():
        pt, pr = v.get(f"{side_key}_point"), v.get(f"{side_key}_price")
        if pt is None or pr is None:
            continue
        out.setdefault(float(pt), {})[book] = pr
    return out


def row_from_header(header: list[str], d: dict) -> list:
    return [d.get(h, "") for h in header]


# ── Game score projection engine ──────────────────────────────────────────────
def project_game_score(home_abbr, away_abbr, team_stats, rest_lookup, weather_by_game, game_id):
    """
    Core scoring model: a team's projected points = average of (its own
    offensive scoring rate) and (the opponent's own points-allowed rate),
    plus home field advantage / rest / weather adjustments. See the
    HOME_FIELD_ADV / PRIOR_SEASON_SHRINK_K / CURRENT_SEASON_PRIOR_WEIGHT /
    rest / weather constants above for the reasoning behind each term.
    """
    home, away = team_stats.get(home_abbr), team_stats.get(away_abbr)
    if not home or not away:
        return None

    # HFA is a margin effect (home team performs better relative to a neutral
    # site), not extra total scoring — split it so it shifts the projected
    # margin by the full HOME_FIELD_ADV but leaves the projected TOTAL
    # unaffected. (Bug caught in testing: applying the full +1.5 only to the
    # home side inflated every projected total by ~1.5 pts vs. the book,
    # confirmed via a 75-game backtest showing a systematic +1.53 mean bias.)
    proj_away = (away["off_rating"] + home["def_rating"]) / 2 - HOME_FIELD_ADV / 2
    proj_home = (home["off_rating"] + away["def_rating"]) / 2 + HOME_FIELD_ADV / 2

    rest = rest_lookup.get((home_abbr, away_abbr), {})
    home_rest, away_rest = rest.get("home_rest"), rest.get("away_rest")
    if home_rest is not None:
        if home_rest < 6:
            proj_home += SHORT_WEEK_PENALTY
        elif home_rest >= 13:
            proj_home += BYE_WEEK_BONUS
    if away_rest is not None:
        if away_rest < 6:
            proj_away += SHORT_WEEK_PENALTY
        elif away_rest >= 13:
            proj_away += BYE_WEEK_BONUS

    w = weather_by_game.get(game_id)
    weather_adj, wind_mph, temp_f = 0.0, None, None
    if w and w.get("wind_mph") is not None:
        wind_mph, temp_f = w["wind_mph"], w.get("temp_f")
        excess = max(0.0, wind_mph - WIND_THRESHOLD_MPH)
        weather_adj = max(WIND_MAX_PENALTY, excess * WIND_PENALTY_PER_MPH)
        proj_away += weather_adj / 2
        proj_home += weather_adj / 2

    return {
        "proj_away": round(proj_away, 2), "proj_home": round(proj_home, 2),
        "home_rest": home_rest, "away_rest": away_rest,
        "weather_adj": round(weather_adj, 2), "wind_mph": wind_mph, "temp_f": temp_f,
    }


# ── Game Totals ────────────────────────────────────────────────────────────────
def analyze_game_totals(games_by_id, team_stats, rest_lookup, weather_by_game):
    """Returns edge_dicts — one per qualifying game-total edge.

    Bet History is now a SINGLE table keyed game+type+side+line (see
    nfl_bet_tracking), so the old per-bet-type shadow tabs and their row
    builders are gone. Everything downstream consumes the edge dicts."""
    edge_dicts = []
    today = datetime.now().strftime("%Y-%m-%d")

    for game_id, g in games_by_id.items():
        home_abbr = TEAM_NAME_TO_ABBR.get(g["home_team"])
        away_abbr = TEAM_NAME_TO_ABBR.get(g["away_team"])
        totals_books = g.get("totals", {})
        if not home_abbr or not away_abbr or not totals_books:
            continue
        proj = project_game_score(home_abbr, away_abbr, team_stats, rest_lookup, weather_by_game, game_id)
        if not proj:
            continue

        our_total = proj["proj_away"] + proj["proj_home"]
        points = [v["point"] for v in totals_books.values() if v.get("point") is not None]
        if not points:
            continue
        # Consensus decides WHICH SIDE we're on (it's the market's centre), but
        # the bet is then placed at a real book's real line — see best_available().
        consensus_line = round(sum(points) / len(points), 2)
        direction = "Over" if our_total > consensus_line else "Under"
        price_key = "over_price" if direction == "Over" else "under_price"

        by_line = _totals_by_line(totals_books, price_key)
        line, best_price, best_book, n_books = best_available(
            by_line, prefer="low" if direction == "Over" else "high")
        if line is None:
            continue

        # Edge is measured against the line we would actually bet, not the
        # unbettable average.
        edge = (our_total - line) if direction == "Over" else (line - our_total)
        abs_edge = abs(edge)
        if abs_edge < GAME_TOTAL_SCALE[0][0]:
            continue
        units = unit_scale(abs_edge, GAME_TOTAL_SCALE)
        stars = stars_from_units(units)
        dk_price = totals_books.get("draftkings", {}).get(price_key)

        game_label = f"{g['away_team']} @ {g['home_team']}"
        edge_pct = round((abs_edge / line * 100), 1) if line else 0.0
        conf = "High" if stars == 5 else "Medium" if stars == 4 else "Standard"
        conf_pct = round(units * 100, 1)

        d = {
            "Date": today, "Game": game_label, "Time (ET)": _fmt_time_et(g["commence_time"]),
            "Away Team": g["away_team"], "Home Team": g["home_team"],
            "Away QB": "", "Home QB": "",
            "Book": best_book or "", "Bet Type": "Game Total", "Direction": direction,
            "Bet On": f"{direction} {line:g}",
            "Stars": stars_emoji(stars), "Units": units, "Units Bet": units, "Units Would Bet": units,
            "Book Line": line, "Consensus Line": consensus_line,
            "Books At Line": n_books,
            "Book Juice": best_price if best_price is not None else "",
            "DK Juice": dk_price if dk_price is not None else "",
            "Our Projection": round(our_total, 1), "Edge": round(edge, 2), "Edge %": edge_pct,
            "Edge % of Line": edge_pct,
            "Proj Away Score": proj["proj_away"], "Proj Home Score": proj["proj_home"],
            "Home Win%": "", "Away Win%": "",
            "Confidence": conf, "Confidence %": conf_pct,
            "Away Rest Days": proj["away_rest"] or "", "Home Rest Days": proj["home_rest"] or "",
            "Wind MPH": proj["wind_mph"] or "", "Wind Dir": "", "Temp (F)": proj["temp_f"] or "",
            "Weather Adj": proj["weather_adj"], "Roof": NFL_STADIUMS.get(home_abbr, {}).get("roof", ""),
            "Run at": datetime.now().strftime("%H:%M"),
            # Canonical fields for nfl_bet_tracking (leading underscore = not a
            # sheet column; row_from_header ignores anything not in the header)
            "_game_id": game_id, "_stars_n": stars,
            "_side": direction, "_line": line,
            "_kickoff_et": _fmt_time_et(g["commence_time"]),
            "_kickoff_utc": g.get("commence_time", ""),
        }
        edge_dicts.append(d)

    return edge_dicts


# ── Moneyline + Spread ─────────────────────────────────────────────────────────
def analyze_moneyline_spread(games_by_id, team_stats, rest_lookup, weather_by_game):
    """Returns edge_dicts — one per qualifying moneyline or spread edge."""
    edge_dicts = []
    today = datetime.now().strftime("%Y-%m-%d")

    for game_id, g in games_by_id.items():
        home_abbr = TEAM_NAME_TO_ABBR.get(g["home_team"])
        away_abbr = TEAM_NAME_TO_ABBR.get(g["away_team"])
        if not home_abbr or not away_abbr:
            continue
        proj = project_game_score(home_abbr, away_abbr, team_stats, rest_lookup, weather_by_game, game_id)
        if not proj:
            continue
        proj_margin_home = proj["proj_home"] - proj["proj_away"]
        home_win_pct = normal_cdf(proj_margin_home, sd=MARGIN_STD_DEV) * 100
        away_win_pct = 100 - home_win_pct
        game_label = f"{g['away_team']} @ {g['home_team']}"
        time_et = _fmt_time_et(g["commence_time"])

        # -- Moneyline --
        h2h_books = g.get("h2h", {})
        novig_home_list = []
        for v in h2h_books.values():
            hp, ap = v.get("home_price"), v.get("away_price")
            if hp is None or ap is None:
                continue
            ih, ia = american_to_implied(hp), american_to_implied(ap)
            novig_home_list.append(ih / (ih + ia))
        if novig_home_list:
            consensus_home_implied = sum(novig_home_list) / len(novig_home_list) * 100
            edge_home = home_win_pct - consensus_home_implied
            side = "home" if edge_home > 0 else "away"
            abs_edge = abs(edge_home)
            if abs_edge >= ML_SCALE[0][0]:
                units = unit_scale(abs_edge, ML_SCALE)
                stars = stars_from_units(units)
                price_key = f"{side}_price"
                best_book, best_price = None, None
                for book, v in h2h_books.items():
                    p = v.get(price_key)
                    if p is not None and (best_price is None or p > best_price):
                        best_book, best_price = book, p
                bet_team = g["home_team"] if side == "home" else g["away_team"]
                d = {
                    "Date": today, "Game": game_label, "Time (ET)": time_et,
                    "Away Team": g["away_team"], "Home Team": g["home_team"],
                    "Away QB": "", "Home QB": "",
                    "Away Off Adj": team_stats[away_abbr]["off_rating"], "Home Off Adj": team_stats[home_abbr]["off_rating"],
                    "Away Def Adj": team_stats[away_abbr]["def_rating"], "Home Def Adj": team_stats[home_abbr]["def_rating"],
                    "Bet Type": "Moneyline", "Bet Team": bet_team, "Bet Side": side,
                    "Our Win%": round(home_win_pct if side == "home" else away_win_pct, 1),
                    "Proj Away Score": proj["proj_away"], "Proj Home Score": proj["proj_home"],
                    "Proj Margin": round(proj_margin_home, 1),
                    "Book": best_book or "", "Book Juice": best_price if best_price is not None else "",
                    "Book Implied%": round(consensus_home_implied if side == "home" else 100 - consensus_home_implied, 1),
                    "Consensus Implied%": round(consensus_home_implied, 1),
                    "Edge vs Book%": round(abs_edge, 1), "Edge vs Consensus%": round(abs_edge, 1),
                    "Spread Line": "", "Stars": stars_emoji(stars), "Units": units, "Units Would Bet": units,
                    "Edge Bucket": f"{int(abs_edge)}-{int(abs_edge) + 2}%",
                    "Market Favorite": g["home_team"] if consensus_home_implied >= 50 else g["away_team"],
                    "Book Line": "", "Our Projection": round(home_win_pct if side == "home" else away_win_pct, 1),
                    "Edge": round(abs_edge, 1), "Edge %": round(abs_edge, 1),
                    "Bet On": f"{bet_team} ML", "Direction": side, "Home Win%": round(home_win_pct, 1),
                    "Away Win%": round(away_win_pct, 1),
                    "Confidence": "High" if stars == 5 else "Medium" if stars == 4 else "Standard",
                    "Confidence %": round(units * 100, 1),
                    "Run at": datetime.now().strftime("%H:%M"),
                    "_game_id": game_id, "_stars_n": stars,
                    "_side": bet_team, "_line": None,
                    "_kickoff_et": time_et,
                    "_kickoff_utc": g.get("commence_time", ""),
                }
                edge_dicts.append(d)

        # -- Spread --
        spread_books = g.get("spreads", {})
        home_points = [v["home_point"] for v in spread_books.values() if v.get("home_point") is not None]
        if home_points:
            # Consensus picks the SIDE (it's the market's centre); the bet is
            # then placed at a real book's real line — averaging book spreads
            # produces numbers nobody offers (10.166, 6.833, 2.833 all appeared
            # in testing) which can be neither bet nor honestly graded.
            consensus_home_point = round(sum(home_points) / len(home_points), 2)
            side = "home" if (proj_margin_home + consensus_home_point) > 0 else "away"
            # Higher is always better for the side you back: +10.5 beats +10,
            # and -3 beats -3.5. So "high" works for favourite and dog alike.
            bet_point, best_price, best_book, n_books = best_available(
                _spreads_by_line(spread_books, side), prefer="high")
            own_margin = proj_margin_home if side == "home" else -proj_margin_home
            abs_edge = abs(own_margin + bet_point) if bet_point is not None else -1.0
            if bet_point is not None and abs_edge >= SPREAD_SCALE[0][0]:
                units = unit_scale(abs_edge, SPREAD_SCALE)
                stars = stars_from_units(units)
                bet_team = g["home_team"] if side == "home" else g["away_team"]
                consensus_for_side = (consensus_home_point if side == "home"
                                      else -consensus_home_point)
                d = {
                    "Date": today, "Game": game_label, "Time (ET)": time_et,
                    "Away Team": g["away_team"], "Home Team": g["home_team"],
                    "Away QB": "", "Home QB": "",
                    "Away Off Adj": team_stats[away_abbr]["off_rating"], "Home Off Adj": team_stats[home_abbr]["off_rating"],
                    "Away Def Adj": team_stats[away_abbr]["def_rating"], "Home Def Adj": team_stats[home_abbr]["def_rating"],
                    "Bet Type": "Spread", "Bet Team": bet_team, "Bet Side": side,
                    "Our Win%": "", "Proj Away Score": proj["proj_away"], "Proj Home Score": proj["proj_home"],
                    "Proj Margin": round(proj_margin_home, 1),
                    "Book": best_book or "", "Book Juice": best_price if best_price is not None else "",
                    "Book Implied%": "", "Consensus Implied%": "",
                    "Edge vs Book%": round(abs_edge, 1), "Edge vs Consensus%": round(abs_edge, 1),
                    "Spread Line": bet_point, "Consensus Line": consensus_for_side,
                    "Books At Line": n_books,
                    "Stars": stars_emoji(stars), "Units": units, "Units Would Bet": units,
                    "Edge Bucket": f"{int(abs_edge)}-{int(abs_edge) + 2}pts",
                    "Market Favorite": g["home_team"] if consensus_home_point < 0 else g["away_team"],
                    "Book Line": bet_point, "Our Projection": round(proj_margin_home if side == "home" else -proj_margin_home, 1),
                    "Edge": round(abs_edge, 1), "Edge %": round(abs_edge, 1),
                    "Bet On": f"{bet_team} {bet_point:+g}", "Direction": side,
                    "Home Win%": round(home_win_pct, 1), "Away Win%": round(away_win_pct, 1),
                    "Confidence": "High" if stars == 5 else "Medium" if stars == 4 else "Standard",
                    "Confidence %": round(units * 100, 1),
                    "Run at": datetime.now().strftime("%H:%M"),
                    "_game_id": game_id, "_stars_n": stars,
                    "_side": bet_team, "_line": bet_point,
                    "_kickoff_et": time_et,
                    "_kickoff_utc": g.get("commence_time", ""),
                }
                edge_dicts.append(d)

    return edge_dicts


# ── Team Totals ────────────────────────────────────────────────────────────────
def analyze_team_totals(games_by_id, team_stats, rest_lookup, weather_by_game):
    """Returns edge_dicts — one per qualifying team-total edge."""
    edge_dicts = []
    today = datetime.now().strftime("%Y-%m-%d")

    for game_id, g in games_by_id.items():
        home_abbr = TEAM_NAME_TO_ABBR.get(g["home_team"])
        away_abbr = TEAM_NAME_TO_ABBR.get(g["away_team"])
        tt_books = g.get("team_totals", {})
        if not home_abbr or not away_abbr or not tt_books:
            continue
        proj = project_game_score(home_abbr, away_abbr, team_stats, rest_lookup, weather_by_game, game_id)
        if not proj:
            continue
        game_label = f"{g['away_team']} @ {g['home_team']}"

        for side, team_name, our_score in (
            ("home", g["home_team"], proj["proj_home"]),
            ("away", g["away_team"], proj["proj_away"]),
        ):
            pts = [v[f"{side}_point"] for v in tt_books.values() if v.get(f"{side}_point") is not None]
            if not pts:
                continue
            # Consensus picks the side; the bet is placed at a real book's line
            # (same bettable-line fix as game totals — see best_available()).
            consensus_line = round(sum(pts) / len(pts), 2)
            direction = "Over" if our_score > consensus_line else "Under"
            price_key = f"{side}_{'over' if direction == 'Over' else 'under'}_price"

            by_line = {}
            for book, v in tt_books.items():
                pt, pr = v.get(f"{side}_point"), v.get(price_key)
                if pt is None or pr is None:
                    continue
                by_line.setdefault(float(pt), {})[book] = pr
            line, best_price, best_book, n_books = best_available(
                by_line, prefer="low" if direction == "Over" else "high")
            if line is None:
                continue

            edge = (our_score - line) if direction == "Over" else (line - our_score)
            abs_edge = abs(edge)
            if abs_edge < TEAM_TOTAL_SCALE_NFL[0][0]:
                continue
            units = unit_scale(abs_edge, TEAM_TOTAL_SCALE_NFL)
            stars = stars_from_units(units)
            edge_pct = round((abs_edge / line * 100), 1) if line else 0.0
            stars_label = stars_emoji(stars)
            d = {
                "Date": today, "Game": game_label, "Team": team_name, "Direction": direction,
                "Best Book": best_book or "", "Book": best_book or "",
                "Book Line": line, "Consensus Line": consensus_line,
                "Books At Line": n_books,
                "Book Juice": best_price if best_price is not None else "",
                "Our Projection": round(our_score, 1), "Edge": round(edge, 2), "Edge %": edge_pct,
                "Stars": stars_label, "Units": units,
                "Confidence": "High" if stars == 5 else "Medium" if stars == 4 else "Standard",
                "Confidence %": round(units * 100, 1),
                "Time (ET)": _fmt_time_et(g["commence_time"]),
                "Bet Type": "Team Total", "Bet On": f"{team_name} {direction} {line:g}",
                "Proj Away Score": proj["proj_away"], "Proj Home Score": proj["proj_home"],
                "Home Win%": "", "Away Win%": "", "Away QB": "", "Home QB": "",
                "Run at": datetime.now().strftime("%H:%M"),
                "_game_id": game_id, "_stars_n": stars,
                "_side": f"{team_name} {direction}", "_line": line,
                "_kickoff_et": _fmt_time_et(g["commence_time"]),
            "_kickoff_utc": g.get("commence_time", ""),
            }
            edge_dicts.append(d)

    return edge_dicts


def to_tracking_candidates(all_edge_dicts: list[dict]) -> list[dict]:
    """
    Map this run's qualifying edges into the canonical shape
    nfl_bet_tracking.upsert_bet_history() expects.

    Every bet type funnels through here, so Bet History is one table with a
    Bet Type column rather than a tab per type — the old per-type shadow tabs
    were a MLB-ism that the game+type+side+line key makes redundant.
    """
    out = []
    for d in all_edge_dicts:
        out.append({
            "game_id":    d.get("_game_id", ""),
            "game":       d.get("Game", ""),
            "kickoff_et": d.get("_kickoff_et", d.get("Time (ET)", "")),
            "kickoff_utc": d.get("_kickoff_utc", ""),
            "bet_type":   d.get("Bet Type", ""),
            "side":       d.get("_side", ""),
            "bet_on":     d.get("Bet On", ""),
            "line":       d.get("_line"),
            "consensus_line": d.get("Consensus Line"),
            "books_at_line":  d.get("Books At Line"),
            "price":      d.get("Book Juice"),
            "book":       d.get("Book", ""),
            "stars":      d.get("_stars_n", 0),
            "units":      d.get("Units"),
            "projection": d.get("Our Projection"),
            "edge":       d.get("Edge"),
            "edge_pct":   d.get("Edge %"),
        })
    return out


def build_projection_log_entries(games_by_id, team_stats, rest_lookup,
                                 weather_by_game, qualified_keys: set) -> list[dict]:
    """
    One row per game per bet type per side, EVERY run, whether or not it
    qualified. This is what makes the star thresholds testable after the fact —
    without it you only ever see bets that already cleared the bar, so you can
    never ask "would my 2.8-point edges have won too?"
    """
    entries = []
    for game_id, g in games_by_id.items():
        home = TEAM_NAME_TO_ABBR.get(g["home_team"])
        away = TEAM_NAME_TO_ABBR.get(g["away_team"])
        if not home or not away:
            continue
        proj = project_game_score(home, away, team_stats, rest_lookup,
                                  weather_by_game, game_id)
        if not proj:
            continue
        label = f"{g['away_team']} @ {g['home_team']}"
        kick = _fmt_time_et(g.get("commence_time", ""))
        our_total = proj["proj_away"] + proj["proj_home"]
        margin = proj["proj_home"] - proj["proj_away"]

        def add(bet_type, side, projection, consensus, edge):
            # Edge % is only meaningful where the line is a magnitude (a total).
            # For a spread it divides by a signed handicap, so a -3.5 line turns
            # a 1.55-point edge into "-44.29%" — sign-flipped and meaningless.
            # Moneylines have no line at all. Left blank for both.
            if bet_type in ("Game Total", "Team Total") and edge is not None and consensus:
                pct = round(abs(edge) / abs(consensus) * 100, 2)
            else:
                pct = ""
            key = f"{game_id}|{bet_type}|{side}"
            entries.append({
                "game_id": game_id, "game": label, "kickoff_et": kick,
                "bet_type": bet_type, "side": side,
                "projection": round(projection, 2) if projection is not None else "",
                "consensus_line": consensus,
                "edge": round(edge, 2) if edge is not None else "",
                "edge_pct": pct,
                "qualified": key in qualified_keys,
            })

        pts = [v["point"] for v in g.get("totals", {}).values() if v.get("point") is not None]
        if pts:
            line = round(sum(pts) / len(pts), 1)
            add("Game Total", "Over", our_total, line, our_total - line)

        hp = [v["home_point"] for v in g.get("spreads", {}).values()
              if v.get("home_point") is not None]
        if hp:
            line = round(sum(hp) / len(hp), 2)
            add("Spread", g["home_team"], margin, line, margin + line)

        add("Moneyline", g["home_team"],
            round(normal_cdf(margin, sd=MARGIN_STD_DEV) * 100, 1), "", None)
    return entries


def build_edges(all_edge_dicts: list[dict]) -> list:
    """Combine all bet-type edge dicts into unified Edges rows.

    Ordering is applied in write_edges_tab() instead, once props have been
    merged in — see sort_edge_rows()."""
    return [row_from_header(EDGES_HEADER, d) for d in all_edge_dicts]


# ── Header schemas ─────────────────────────────────────────────────────────────
# Column sets mirror the proven MLB structure, swapped to football-relevant
# fields (QB instead of SP, spread instead of run line, wind/temp kept since
# outdoor NFL games are just as weather-sensitive as MLB). These are locked in
# now as the agreed schema; Step 3 fills the values, not the columns.
EDGES_HEADER = [
    "Game", "Time (ET)", "Book", "Bet Type", "Direction", "Bet On",
    "Stars", "Units", "Book Line", "Consensus Line", "Books At Line",
    "Book Juice", "Our Projection",
    "Edge", "Edge %", "Away QB", "Home QB",
    "Proj Away Score", "Proj Home Score", "Home Win%", "Away Win%",
    "Confidence", "Confidence %", "Run at",
]





# Not created as a tab yet — mirrors the MLB precedent of keeping the schema
# ready in code and only adding the tab once FETCH_PLAYER_PROPS is enabled.
PLAYER_PROPS_HEADER = [
    "Date", "Game", "Player", "Prop Type", "Direction",
    "Best Book", "Book Line", "Book Juice", "Our Projection",
    "Edge", "Edge %", "Stars", "Units",
    "Confidence", "Confidence %",
    "Actual", "Result", "Units Result", "Run at",
]


# ── Tab writers ────────────────────────────────────────────────────────────────
def today_already_logged(worksheet) -> bool:
    try:
        col = worksheet.col_values(1)
        return datetime.now().strftime("%Y-%m-%d") in col[1:]
    except Exception:
        return False


def sort_edge_rows(rows: list[list]) -> list[list]:
    """
    Sort the Edges board by STARS then UNITS, both descending — same convention
    as the MLB Edges tab, so the strongest plays are always at the top.

    Sorting happens HERE rather than in build_edges() because props are added
    after that call; sorting earlier left them simply appended below the
    game-level rows instead of interleaved by strength.

    Stars are emoji strings, so rank on the count rather than the string —
    lexicographic order on emoji happens to work for a single repeated glyph
    but breaks the moment the label format changes.
    """
    si = EDGES_HEADER.index("Stars")
    ui = EDGES_HEADER.index("Units")

    def key(r):
        stars = str(r[si]).count("⭐") if si < len(r) else 0
        try:
            units = float(r[ui])
        except (TypeError, ValueError, IndexError):
            units = 0.0
        return (stars, units)

    return sorted(rows, key=key, reverse=True)


def write_edges_tab(gc, edge_rows):
    edge_rows = sort_edge_rows(edge_rows)
    w = ws(gc, NFL_SHEET_ID, "Edges", header=EDGES_HEADER)
    w.clear()
    w.update([EDGES_HEADER] + edge_rows, value_input_option="USER_ENTERED")
    print(f"Wrote {len(edge_rows)} rows to 'Edges' tab (sorted by stars, then units)")



# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    import nfl_bet_tracking as tracking  # deferred: it imports this module back

    print("=" * 60)
    print("nfl_analyze_edges.py — Fantasy Six Pack NFL Edge Analysis")
    print(f"Run time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    gc = get_client()

    print("\nLoading NFL Odds tab ...")
    odds_ws = ws(gc, NFL_SHEET_ID, "NFL Odds", header=None)
    odds_rows = sheet_to_dicts(odds_ws)
    print(f"  {len(odds_rows)} odds rows loaded")
    prop_rows = [r for r in odds_rows if str(r.get("market_key", "")).startswith("player_")]
    prop_edge_rows = []

    if snapshot_only:
        # CLV snapshot path — see the `snapshot_only` comment at the top.
        rest_lookup = build_rest_lookup()
        games_by_id = group_odds_by_game(odds_rows)
        games_by_id, dropped = filter_to_scheduled_games(games_by_id, rest_lookup)
        print(f"  {len(games_by_id)} regular-season game(s)")
        # Closing capture ALWAYS runs — it is idempotent, costs no credits, and
        # backfills anything a missed run left behind.
        cap = tracking.capture_closing_and_clv(gc)
        print(f"Closing/CLV: {cap['captured']} captured, {cap['pending']} awaiting kickoff"
              + (f", {cap['no_snapshot']} with NO pre-kickoff snapshot" if cap['no_snapshot'] else ""))

        # But only APPEND a snapshot when a game is actually near kickoff. The
        # fetch step has the same guard, so on a game-less day the odds tab is
        # stale — re-logging it would write thousands of duplicate rows that say
        # nothing new (Sun Sep 6: 18 fires x ~220 rows with zero games).
        soonest_h = None
        now = datetime.now(timezone.utc)
        for g in games_by_id.values():
            k = None
            try:
                k = datetime.fromisoformat(str(g.get("commence_time", "")).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass
            if k and k > now:
                h = (k - now).total_seconds() / 3600
                soonest_h = h if soonest_h is None else min(soonest_h, h)
        if soonest_h is not None and soonest_h > SNAPSHOT_KICKOFF_WINDOW_H:
            print(f"Line Log: skipped — next kickoff {soonest_h:.1f}h away "
                  f"(> {SNAPSHOT_KICKOFF_WINDOW_H}h), nothing new to record")
        else:
            n_lines = tracking.append_line_log(gc, games_by_id, prop_rows=prop_rows or None)
            print(f"Line Log: appended {n_lines} line quote(s)")
        print("\nDone (snapshot-only).")
        return

    print("\nLoading organic projections sheet ...")
    try:
        organic = load_organic_projections(gc)
        print(f"  {len(organic)} players loaded from consensus projections")
    except Exception as e:
        print(f"  [warn] could not load organic sheet: {e}")
        organic = {}

    print("\nLoading nflverse team stats (2025 prior, regressed to mean) ...")
    team_stats = load_team_stats()
    print(f"  League avg PPG: {team_stats['_league_avg']}")

    print("Loading 2026 schedule for rest-day lookups ...")
    rest_lookup = build_rest_lookup()

    games_by_id = group_odds_by_game(odds_rows)
    print(f"  {len(games_by_id)} unique games in NFL Odds tab")

    # Preseason guard — see filter_to_scheduled_games(). Books post preseason
    # lines and nothing downstream would notice they aren't real games.
    games_by_id, dropped = filter_to_scheduled_games(games_by_id, rest_lookup)
    if dropped:
        print(f"  Dropped {len(dropped)} game(s) not on the regular-season schedule "
              f"(preseason/other): {', '.join(dropped[:4])}"
              + (" ..." if len(dropped) > 4 else ""))
    print(f"  {len(games_by_id)} regular-season game(s) to analyse")

    print("Checking weather for outdoor games within 7 days ...")
    weather_by_game = load_weather(games_by_id)
    print(f"  {len(weather_by_game)} game(s) with a weather pull")

    print("\nRunning game-level analysis ...")
    gt_edges = analyze_game_totals(games_by_id, team_stats, rest_lookup, weather_by_game)
    ml_edges = analyze_moneyline_spread(games_by_id, team_stats, rest_lookup, weather_by_game)
    tt_edges = analyze_team_totals(games_by_id, team_stats, rest_lookup, weather_by_game)
    edge_rows                              = build_edges(gt_edges + ml_edges + tt_edges)

    print(f"  {len(edge_rows)} edges found")
    print(f"  {len(gt_edges)} game total edge(s)")
    print(f"  {len(ml_edges)} ML/Spread edge(s)")
    print(f"  {len(tt_edges)} team total edge(s)")

    # ── Capture closing lines for anything that has kicked off since last run.
    # Runs first so a game that started overnight is captured before anything
    # else touches Bet History. Backfills by design — see the function docstring.
    cap = tracking.capture_closing_and_clv(gc)
    print(f"Closing/CLV: {cap['captured']} captured, {cap['pending']} awaiting kickoff"
          + (f", {cap['backfilled']} kickoff backfilled" if cap.get('backfilled') else "")
          + (f", {cap['no_snapshot']} with NO pre-kickoff snapshot" if cap['no_snapshot'] else ""))


    # ── Bet History: permanent, keyed game+type+side+line, upserted ──────────
    all_edges = gt_edges + ml_edges + tt_edges
    candidates = to_tracking_candidates(all_edges)
    stats = tracking.upsert_bet_history(gc, candidates)
    print(f"Bet History: {stats['added']} new bet(s), {stats['updated']} updated, "
          f"{stats['total']} tracked total")

    # ── Player props (gate is in nfl_fetch_odds.FETCH_PLAYER_PROPS) ─────────
    if prop_rows:
        import nfl_props_model as props_model
        print("")
        print(f"Player props: {len(prop_rows)} quote(s) on the board")
        projections, pdiag = props_model.project_slate(
            gc, games_by_id, team_stats, rest_lookup, weather_by_game)

        # Re-level against the posted market BEFORE pricing anything. The
        # organic sheet refreshes itself (it dropped 12-15% overnight on
        # 2026-09-08) and a stale level correction turns the whole board
        # one-directional — 94% Unders that day, none of them real. Runs
        # before analyze_player_props so the role gate and every edge see
        # corrected numbers. See calibrate_to_market() for the reasoning.
        calib = props_model.calibrate_to_market(projections, prop_rows)
        for stat, c in sorted(calib.items()):
            note = (f"measured {c['measured']} on n={c['n']}"
                    if c["measured"] is not None else f"n={c['n']}")
            print(f"  calibration {stat:12} x{c['applied']:<6} {note}  [{c['status']}]")
        rejected = [s for s, c in calib.items() if "REJECTED" in c["status"]]
        if rejected:
            print(f"  [CHECK] calibration rejected for {', '.join(rejected)} — "
                  f"the organic sheet may be mid-update or broken.")

        prop_cands, pmeta = props_model.analyze_player_props(prop_rows, projections)
        print(f"  {pdiag['players_projected']} player-game projections | "
              f"{len(pmeta['diagnostics'])} market(s) priced | "
              f"{len(prop_cands)} edge(s) qualifying")
        if pmeta["unmatched"]:
            print(f"  [check] {len(pmeta['unmatched'])} book player(s) with no projection: "
                  + ", ".join(pmeta["unmatched"][:5])
                  + (" ..." if len(pmeta["unmatched"]) > 5 else ""))
        # Log EVERY priced prop market, qualifying or not — same reasoning as
        # the game-level Projection Log. Without it, only bets that already
        # cleared the threshold are ever visible, so the threshold can only be
        # tuned on its own survivors. This is what lets us ask later "would the
        # sub-4pp prop edges have won too?" without betting them now.
        prop_log = [{
            "game_id": x["game_id"], "game": x["game"], "kickoff_et": "",
            "bet_type": props_model.PROP_LABEL.get(x["prop"], x["prop"]),
            "side": f"{x['player']}",
            "projection": x["projection"], "consensus_line": x["line"],
            "edge": x["edge_pp"], "edge_pct": x["edge_pp"],
            "stars": "", "units": "",
            "qualified": x["edge_pp"] >= props_model.scale_for(x["prop"])[0][0],
        } for x in pmeta["diagnostics"]]
        n_plog = tracking.append_projection_log(gc, prop_log)
        print(f"  Projection Log (props): appended {n_plog} market(s)")

        # Props belong on the Edges tab too. It is the "what looks good right
        # now" view, and it was showing only the four game-level types while
        # props went straight to Bet History — so the live board silently
        # disagreed with the tracked record.
        # EDGES SHOWS ONE ROW PER OPINION — the best line only.
        # Bet History still tracks every distinct line separately (that is what
        # gives each its own CLV), but the board is "what should I bet right
        # now", and three Cam Ward pass-yard lines is one decision, not three.
        # Highest edge IS the best line here: for an Over the lowest number
        # carries the biggest edge and is also easiest to hit, and for an Under
        # the highest does — so ranking on edge picks the right one either way.
        best_by_opinion = {}
        for c in prop_cands:
            k = (c["game_id"], c["bet_type"], c["side"])
            if k not in best_by_opinion or c["edge"] > best_by_opinion[k]["edge"]:
                best_by_opinion[k] = c
        deduped = list(best_by_opinion.values())
        if len(deduped) < len(prop_cands):
            print(f"  Edges board: {len(prop_cands)} tracked prop line(s) -> "
                  f"{len(deduped)} shown (best line per player/prop)")

        prop_edge_rows = [row_from_header(EDGES_HEADER, {
            "Game": c["game"], "Time (ET)": c.get("kickoff_et", ""),
            "Book": c.get("book", ""), "Bet Type": c["bet_type"],
            "Direction": c["side"].rsplit(" ", 1)[-1] if c["bet_type"] != "Anytime TD" else "Yes",
            "Bet On": c["bet_on"], "Stars": stars_emoji(c["stars"]), "Units": c["units"],
            "Book Line": c.get("line", ""), "Book Juice": c.get("price", ""),
            "Our Projection": c.get("projection", ""),
            "Edge": c.get("edge", ""), "Edge %": c.get("edge_pct", ""),
            "Confidence": "High" if c["stars"] == 5 else "Medium" if c["stars"] == 4 else "Standard",
            "Confidence %": round(c["units"] * 100, 1),
            "Run at": datetime.now().strftime("%H:%M"),
        }) for c in deduped]

        if props_model.PROPS_TRACKING_ENABLED:
            pstats = tracking.upsert_bet_history(gc, prop_cands)
            print(f"  Bet History (props): {pstats['added']} new, {pstats['updated']} updated")
        else:
            print("  Bet History (props): SKIPPED — prop tracking gated pending "
                  "per-stat divisor recalibration (see PROPS_TRACKING_ENABLED)")

    # ── Edges tab: the LIVE view, cleared and rewritten every run ────────────
    write_edges_tab(gc, edge_rows + prop_edge_rows)

    # ── Line Log: every distinct line on the market, all games, every run ────
    n_lines = tracking.append_line_log(gc, games_by_id, prop_rows=prop_rows or None)
    print(f"Line Log: appended {n_lines} line quote(s)")

    # ── Projection Log: every game, qualifying or not ────────────────────────
    qualified_keys = {f"{d.get('_game_id')}|{d.get('Bet Type')}|{d.get('_side')}"
                      for d in all_edges}
    entries = build_projection_log_entries(games_by_id, team_stats, rest_lookup,
                                           weather_by_game, qualified_keys)
    n_proj = tracking.append_projection_log(gc, entries)
    print(f"Projection Log: appended {n_proj} projection(s)")

    print("\nDone.")


if __name__ == "__main__":
    main()
