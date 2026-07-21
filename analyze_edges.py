"""
analyze_edges.py — MLB edge analysis model.
Loads odds + pitcher/hitter data, projects game totals and win probabilities,
finds edges vs. book lines, writes to Edges / Bet History / Moneyline & Run Line / Game Totals tabs.
"""

import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timezone, timedelta
import math
import os
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Config ────────────────────────────────────────────────────────────────────
ODDS_SHEET_ID  = "1RaSm1ogJtNykM7WbYfQ3b9L7MUePcRBqlFMKuQfA_I4"
DATA_SHEET_ID  = "1AAuiHodCcMzOCpC7oW5xzBXIobcavIvh2AV-sy8OaD4"
CREDS_FILE     = os.path.join(os.path.dirname(__file__), "google_credentials.json")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

BOOKS_TO_KEEP = {"fanduel", "draftkings", "betmgm", "betrivers"}

# Model parameters
BASE_RUNS        = 4.00
LEAGUE_AVG_ERA   = 4.10   # measured avg blended era_est across full SP pool 2026-06-20 (was 4.00, stale/too low)
OFFENSE_WEIGHT   = 0.08
MIN_PROJ_TOTAL   = 6.50
PROJ_CAP_MULT    = 1.20
MAX_WIN_PCT      = 0.70
MIN_WIN_PCT      = 0.30
RUN_LINE_SD      = 4.49  # measured stdev of actual MLB run differentials (205 graded games, 2026-06-28); was a guessed 3.0

TEAM_ABBREV = {
    "ARI": "Arizona Diamondbacks", "ATH": "Athletics", "ATL": "Atlanta Braves",
    "BAL": "Baltimore Orioles",    "BOS": "Boston Red Sox",  "CHC": "Chicago Cubs",
    "CWS": "Chicago White Sox",    "CIN": "Cincinnati Reds", "CLE": "Cleveland Guardians",
    "COL": "Colorado Rockies",     "DET": "Detroit Tigers",  "HOU": "Houston Astros",
    "KCR": "Kansas City Royals",   "LAA": "Los Angeles Angels", "LAD": "Los Angeles Dodgers",
    "MIA": "Miami Marlins",        "MIL": "Milwaukee Brewers",  "MIN": "Minnesota Twins",
    "NYM": "New York Mets",        "NYY": "New York Yankees",   "OAK": "Athletics",
    "PHI": "Philadelphia Phillies","PIT": "Pittsburgh Pirates", "SDP": "San Diego Padres",
    "SEA": "Seattle Mariners",     "SFG": "San Francisco Giants","STL": "St. Louis Cardinals",
    "TBR": "Tampa Bay Rays",       "TEX": "Texas Rangers",      "TOR": "Toronto Blue Jays",
    "WSN": "Washington Nationals",
}

PARK_FACTORS = {
    # ── Full-season recalibration 2026-07-12: 1,428 games, 70% actual implied × 30% prior ──
    # Implied PF = (venue avg runs / league avg 9.03) × 100; prior = previous code value.
    "Washington Nationals": 118,  # implied 126, prior 100  (Nationals Park — massively under-projected)
    "Pittsburgh Pirates":   112,  # implied 117, prior  99  (PNC Park — significantly under-projected)
    "Colorado Rockies":     121,  # implied 124, prior 115  (Coors Field)
    "Chicago Cubs":         106,  # implied 107, prior 102  (Wrigley Field)
    "Kansas City Royals":   104,  # implied 106, prior 100  (Kauffman Stadium)
    "Houston Astros":       104,  # implied 104, prior 103  (Daikin Park)
    "Baltimore Orioles":    103,  # implied 104, prior 101  (Camden Yards)
    "New York Yankees":     103,  # implied 104, prior 102  (Yankee Stadium)
    "New York Mets":        102,  # implied 104, prior  98  (Citi Field)
    "Minnesota Twins":      102,  # implied 102, prior 100  (Target Field)
    "Philadelphia Phillies": 107, # implied 107, prior 107  (Citizens Bank Park — no change)
    "Cincinnati Reds":      100,  # implied  96, prior 108  (Great American Ball Park — was over-projected)
    "Milwaukee Brewers":     98,  # implied  96, prior 103  (American Family Field — was over-projected)
    "Toronto Blue Jays":     98,  # implied  97, prior 100  (Rogers Centre)
    "Tampa Bay Rays":        98,  # implied  98, prior  99  (Tropicana Field)
    "Arizona Diamondbacks":  97,  # implied  97, prior  96  (Chase Field)
    "Chicago White Sox":     95,  # implied  95, prior  99  (Rate Field)
    "Los Angeles Angels":    95,  # implied  95, prior  96  (Angel Stadium)
    "Atlanta Braves":        96,  # implied  94, prior  99  (Truist Park)
    "San Francisco Giants":  94,  # implied  93, prior  96  (Oracle Park)
    "Los Angeles Dodgers":   94,  # implied  92, prior 100  (Dodger Stadium — was over-projected)
    "St. Louis Cardinals":   91,  # implied  88, prior  98  (Busch Stadium — was over-projected)
    "Cleveland Guardians":   90,  # implied  88, prior  96  (Progressive Field — was over-projected)
    "Detroit Tigers":        91,  # implied  89, prior  96  (Comerica Park — was over-projected)
    "Texas Rangers":         91,  # implied  89, prior  96  (Globe Life Field — was over-projected)
    "Miami Marlins":         90,  # implied  87, prior  97  (loanDepot park — was over-projected)
    "San Diego Padres":      88,  # implied  85, prior  94  (Petco Park — was over-projected)
    "Boston Red Sox":        89,  # implied  84, prior 102  (Fenway Park — significantly over-projected)
    "Seattle Mariners":      85,  # implied  81, prior  96  (T-Mobile Park — significantly over-projected)
    "Athletics":             110, # nomadic — see SPECIAL_VENUES for home park overrides
}


# Special venue park factors — A's nomadic home parks (minor league stadiums, extreme hitter environments)
SPECIAL_VENUES = {
    "las vegas ballpark":  160,  # implied 188 (17.0 avg runs, 47 games); raised from 140 on 2026-07-12
    "aviators stadium":    160,
    "sutter health park":  127,  # implied 127 (11.51 avg runs, prior 122)
}

# ── Bullpen model parameters ──────────────────────────────────────────────────
SP_INNINGS             = 5.0    # avg innings per SP start in modern game
BULLPEN_INNINGS        = 4.0    # avg relief innings per game
TOTAL_INNINGS          = 9.0
LEAGUE_AVG_BULLPEN_ERA = 4.20   # 2026 season league avg bullpen ERA

# ── SP rest day ERA multipliers (days since last start → ERA multiplier) ──────
# <4 days = short rest penalty, 4 = normal, 5 = slight benefit, 6+ = rust
REST_DAY_FACTORS = {3: 1.08, 4: 1.00, 5: 0.97, 6: 1.02}
REST_DAY_LONG    = 1.05   # 10+ days — IL return / long layoff
REST_DAY_DEFAULT = 1.00   # unknown rest

# ── SP ERA blend weights (season skill metrics vs recent actual performance) ──
# Season est (xFIP/SIERA blend) stabilizes faster than ERA — anchor at 50%
# Recent starts capture fatigue, injury, mechanics changes
ERA_BLEND_SEASON = 0.50   # season xFIP/xERA/SIERA estimate
ERA_BLEND_LAST5  = 0.30   # ERA over last 5 starts
ERA_BLEND_LAST3  = 0.20   # ERA over last 3 starts (most recent form)

# ── MLB team IDs (for roster/splits API lookups) ──────────────────────────────
TEAM_IDS = {
    "Arizona Diamondbacks": 109, "Athletics": 133,       "Atlanta Braves": 144,
    "Baltimore Orioles":    110, "Boston Red Sox": 111,  "Chicago Cubs": 112,
    "Chicago White Sox":    145, "Cincinnati Reds": 113, "Cleveland Guardians": 114,
    "Colorado Rockies":     115, "Detroit Tigers": 116,  "Houston Astros": 117,
    "Kansas City Royals":   118, "Los Angeles Angels": 108, "Los Angeles Dodgers": 119,
    "Miami Marlins":        146, "Milwaukee Brewers": 158,  "Minnesota Twins": 142,
    "New York Mets":        121, "New York Yankees": 147,   "Philadelphia Phillies": 143,
    "Pittsburgh Pirates":   134, "San Diego Padres": 135,   "San Francisco Giants": 137,
    "Seattle Mariners":     136, "St. Louis Cardinals": 138, "Tampa Bay Rays": 139,
    "Texas Rangers":        140, "Toronto Blue Jays": 141,  "Washington Nationals": 120,
}

# ── Umpire run factors (additive runs vs league avg, applied to projected total) ─
# Source: Umpire Scorecards / Baseball Savant research (2022-2025 data)
# Positive = more runs allowed (hitter-friendly zone), Negative = pitcher-friendly
UMP_FACTORS = {
    # Hitter-friendly umps
    "cb bucknor":         +0.45,
    "doug eddings":       +0.45,
    "dan iassogna":       +0.40,
    "vic carapazza":      +0.35,
    "lance barksdale":    +0.30,
    "mike estabrook":     +0.30,
    "adam hamari":        +0.25,
    "john libka":         +0.30,
    "stu scheurwater":    +0.25,
    "jeremie rehak":      +0.25,
    "pat hoberg":         +0.20,
    # Pitcher-friendly umps
    "hunter wendelstedt": -0.45,
    "ted barrett":        -0.40,
    "tripp gibson":       -0.35,
    "mike muchlinski":    -0.30,
    "larry vanover":      -0.30,
    "chris guccione":     -0.25,
    "bill welke":         -0.25,
    "mark carlson":       -0.25,
    "ryan blakney":       -0.20,
    "jordan baker":       -0.20,
    "angel hernandez":    -0.10,
}

# ── Ballpark coordinates for weather lookup (outdoor + retractable roof parks) ─
# Fully domed parks are excluded — no weather effect
DOME_PARKS = {"Tampa Bay Rays"}   # Tropicana Field — only true fixed dome in MLB

BALLPARK_COORDS = {
    "Arizona Diamondbacks":    (33.4453, -112.0667),
    "Atlanta Braves":          (33.8907, -84.4677),
    "Baltimore Orioles":       (39.2838, -76.6218),
    "Boston Red Sox":          (42.3467, -71.0972),
    "Chicago Cubs":            (41.9484, -87.6553),
    "Chicago White Sox":       (41.8299, -87.6338),
    "Cincinnati Reds":         (39.0979, -84.5082),
    "Cleveland Guardians":     (41.4962, -81.6852),
    "Colorado Rockies":        (39.7559, -104.9942),
    "Detroit Tigers":          (42.3390, -83.0485),
    "Houston Astros":          (29.7573, -95.3555),
    "Kansas City Royals":      (39.0517, -94.4803),
    "Los Angeles Angels":      (33.8003, -117.8827),
    "Los Angeles Dodgers":     (34.0739, -118.2400),
    "Miami Marlins":           (25.7781, -80.2197),
    "Milwaukee Brewers":       (43.0280, -87.9712),
    "Minnesota Twins":         (44.9817, -93.2778),
    "New York Mets":           (40.7571, -73.8458),
    "New York Yankees":        (40.8296, -73.9262),
    "Athletics":               (36.1716, -115.1461),
    "Philadelphia Phillies":   (39.9061, -75.1665),
    "Pittsburgh Pirates":      (40.4469, -80.0057),
    "San Diego Padres":        (32.7076, -117.1570),
    "San Francisco Giants":    (37.7786, -122.3893),
    "Seattle Mariners":        (47.5914, -122.3325),
    "St. Louis Cardinals":     (38.6226, -90.1928),
    "Texas Rangers":           (32.7511, -97.0832),
    "Toronto Blue Jays":       (43.6414, -79.3894),
    "Washington Nationals":    (38.8730, -77.0074),
}

# ── MLB Stats API — venue + umpire lookup ─────────────────────────────────────
def fetch_venue_map(date_str: str) -> dict:
    """Back-compat wrapper — returns only venue map."""
    venue_map, _ = fetch_venue_and_ump_map(date_str)
    return venue_map


def fetch_venue_and_ump_map(date_str: str) -> tuple:
    """
    Returns ({(away,home): venue_name}, {(away,home): umpire_name}).
    Hydrates officials so we get the home plate ump per game.
    """
    import requests as _req
    url    = "https://statsapi.mlb.com/api/v1/schedule"
    params = {"sportId": 1, "date": date_str, "gameType": "R",
              "hydrate": "venue,team,officials"}
    try:
        resp = _req.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [venue/ump lookup failed: {e}]")
        return {}, {}

    venue_map = {}
    ump_map   = {}
    for date_block in data.get("dates", []):
        for game in date_block.get("games", []):
            away  = game["teams"]["away"]["team"]["name"]
            home  = game["teams"]["home"]["team"]["name"]
            venue = game.get("venue", {}).get("name", "").lower()
            venue_map[(away, home)] = venue
            # Find home plate umpire
            for official in game.get("officials", []):
                if official.get("officialType", "").lower() == "home plate":
                    ump_map[(away, home)] = official["official"]["fullName"].lower()
                    break
    return venue_map, ump_map


# ── Bullpen ERA loader (MLB Stats API) ────────────────────────────────────────
def _fetch_bullpen_season() -> dict:
    """
    Returns {team_name: bullpen_era} — season-to-date from MLB Stats API.
    Aggregates all relief pitchers (gamesStarted == 0, IP >= 2) by team.
    """
    import requests as _req
    url    = "https://statsapi.mlb.com/api/v1/stats"
    params = {"stats": "season", "group": "pitching", "gameType": "R",
              "season": 2026, "sportId": 1, "limit": 1500, "playerPool": "All"}
    try:
        resp = _req.get(url, params=params, timeout=25)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [Bullpen season load failed: {e}]")
        return {}

    team_ip = {}
    team_er = {}
    for item in data.get("stats", []):
        for split in item.get("splits", []):
            stat      = split.get("stat", {})
            team_info = split.get("team", {})
            raw_name  = team_info.get("name", "")
            team      = TEAM_ABBREV.get(raw_name, raw_name)
            gs = stat.get("gamesStarted", 0)
            if gs > 0:
                continue   # skip starters
            try:
                ip = float(stat.get("inningsPitched", 0))
                er = float(stat.get("earnedRuns", 0))
            except (ValueError, TypeError):
                continue
            if ip < 2:
                continue   # filter noise
            team_ip[team] = team_ip.get(team, 0.0) + ip
            team_er[team] = team_er.get(team, 0.0) + er

    return {team: round(team_er[team] / ip * 9, 3)
            for team, ip in team_ip.items() if ip > 0}


def _fetch_bullpen_rolling(days: int = 21) -> dict:
    """
    Returns {team_name: bullpen_era} — last N days only, from MLB schedule + boxscore.
    Uses team-level pitching splits per game to sum IP and ER for relievers.
    Min 10 bullpen innings over the window before using rolling data.
    """
    import requests as _req
    from datetime import datetime, timedelta

    end_dt   = datetime.now()
    start_dt = end_dt - timedelta(days=days)

    # Step 1 — get list of completed game PKs in the window
    sched_url = "https://statsapi.mlb.com/api/v1/schedule"
    params = {
        "sportId":   1,
        "startDate": start_dt.strftime("%Y-%m-%d"),
        "endDate":   (end_dt - timedelta(days=1)).strftime("%Y-%m-%d"),
        "gameType":  "R",
    }
    try:
        resp = _req.get(sched_url, params=params, timeout=20)
        resp.raise_for_status()
        sched = resp.json()
    except Exception as e:
        print(f"  [Bullpen rolling schedule fetch failed: {e}]")
        return {}

    game_pks = []
    for db in sched.get("dates", []):
        for g in db.get("games", []):
            if g.get("status", {}).get("abstractGameState") == "Final":
                game_pks.append(g["gamePk"])

    if not game_pks:
        return {}

    # Step 2 — for each game, pull pitching splits and sum reliever IP/ER by team
    team_ip = {}
    team_er = {}

    for pk in game_pks:
        try:
            box_url = f"https://statsapi.mlb.com/api/v1/game/{pk}/boxscore"
            r = _req.get(box_url, timeout=15)
            r.raise_for_status()
            box = r.json()
        except Exception:
            continue

        for side in ("away", "home"):
            team_name = box.get("teams", {}).get(side, {}).get("team", {}).get("name", "")
            team = TEAM_ABBREV.get(team_name, team_name)
            pitchers = box.get("teams", {}).get(side, {}).get("pitchers", [])
            player_info = box.get("teams", {}).get(side, {}).get("players", {})

            for pid in pitchers:
                player = player_info.get(f"ID{pid}", {})
                stats  = player.get("stats", {}).get("pitching", {})
                gs     = stats.get("gamesStarted", 0)
                if gs and int(gs) > 0:
                    continue   # skip starter
                try:
                    ip = float(stats.get("inningsPitched", 0))
                    er = float(stats.get("earnedRuns", 0))
                except (ValueError, TypeError):
                    continue
                if ip < 0.1:
                    continue
                team_ip[team] = team_ip.get(team, 0.0) + ip
                team_er[team] = team_er.get(team, 0.0) + er

    # Require at least 10 bullpen innings over the window for reliability
    return {team: round(team_er[team] / ip * 9, 3)
            for team, ip in team_ip.items() if ip >= 10}


def load_bullpen(days: int = 21) -> dict:
    """
    Returns {team_name: blended_bullpen_era} — 60% rolling (last 21 days) + 40% season.
    Falls back to season avg for teams with insufficient rolling data.
    """
    print(f"  Loading season bullpen ERAs ...")
    season = _fetch_bullpen_season()
    print(f"    {len(season)} teams with season bullpen data")

    print(f"  Loading {days}-day rolling bullpen ERAs ...")
    rolling = _fetch_bullpen_rolling(days)
    print(f"    {len(rolling)} teams with rolling bullpen data")

    blended = {}
    for team in set(list(season.keys()) + list(rolling.keys())):
        s = season.get(team, LEAGUE_AVG_BULLPEN_ERA)
        r = rolling.get(team, s)   # fall back to season if rolling unavailable
        blended[team] = round(0.60 * r + 0.40 * s, 3)
    return blended


# ── SP profiles: recent form, rest days, handedness ───────────────────────────
def load_pitcher_profiles(pitchers: dict) -> dict:
    """
    For each SP in pitchers dict, fetches from MLB Stats API:
      - Handedness (L/R throws)
      - Last 3 and 5 start ERAs
      - Days since last start (rest days)
    Returns {team: {handedness, era_3, era_5, days_rest, last_start_date, player_id}}
    Falls back gracefully on any lookup failure.
    """
    import requests as _req
    from datetime import datetime as _dt

    today = _dt.now().date()
    profiles = {}

    for team, sp_data in pitchers.items():
        name = sp_data.get("name", "").strip()
        if not name or name == "Unknown":
            continue

        try:
            # Step 1 — search for player ID by name
            search = _req.get(
                "https://statsapi.mlb.com/api/v1/people/search",
                params={"names": name, "sport.code": "mlb"},
                timeout=10,
            )
            search.raise_for_status()
            people = search.json().get("people", [])
            if not people:
                continue
            player_id   = people[0]["id"]
            pitch_hand  = people[0].get("pitchHand", {}).get("code", "R")

            # Step 2 — pull 2026 game log
            gl = _req.get(
                f"https://statsapi.mlb.com/api/v1/people/{player_id}/stats",
                params={"stats": "gameLog", "group": "pitching",
                        "gameType": "R", "season": 2026},
                timeout=10,
            )
            gl.raise_for_status()
            splits = gl.json().get("stats", [{}])[0].get("splits", [])

            # Keep only starts (GS == 1), sorted newest first
            starts = sorted(
                [s for s in splits if s.get("stat", {}).get("gamesStarted", 0) == 1],
                key=lambda s: s.get("date", ""), reverse=True,
            )

            if not starts:
                profiles[team] = {"handedness": pitch_hand, "era_3": None,
                                  "era_5": None, "days_rest": None,
                                  "last_start_date": None, "player_id": player_id}
                continue

            # Last start date → days rest
            last_date_str = starts[0].get("date", "")
            try:
                last_date = _dt.strptime(last_date_str, "%Y-%m-%d").date()
                days_rest = (today - last_date).days
            except ValueError:
                days_rest = None
                last_date_str = None

            def _era_from_starts(subset):
                ip_total = er_total = 0.0
                for s in subset:
                    stat = s.get("stat", {})
                    try:
                        ip = float(stat.get("inningsPitched", 0))
                        er = float(stat.get("earnedRuns", 0))
                    except (ValueError, TypeError):
                        continue
                    ip_total += ip
                    er_total += er
                # Require ~2 starts worth of innings before trusting this rate stat —
                # 3 IP (the old floor) is roughly half of one start and highly unstable.
                return round(er_total / ip_total * 9, 3) if ip_total >= 9 else None

            era_3 = _era_from_starts(starts[:3])
            era_5 = _era_from_starts(starts[:5])

            profiles[team] = {
                "handedness":      pitch_hand,
                "era_3":           era_3,
                "era_5":           era_5,
                "days_rest":       days_rest,
                "last_start_date": last_date_str,
                "player_id":       player_id,
            }

        except Exception:
            continue   # silent fail — model still runs without profile

    return profiles


def blend_sp_era(era_est: float, profile: dict) -> tuple[float, float]:
    """
    Returns (blended_era, rest_factor).
    Blends season skill estimate with recent start ERAs, then applies rest day factor.
    """
    era_3 = profile.get("era_3")
    era_5 = profile.get("era_5")

    if era_3 is not None and era_5 is not None:
        blended = (ERA_BLEND_SEASON * era_est +
                   ERA_BLEND_LAST5  * era_5  +
                   ERA_BLEND_LAST3  * era_3)
    elif era_5 is not None:
        blended = 0.60 * era_est + 0.40 * era_5
    else:
        blended = era_est

    days = profile.get("days_rest")
    if days is None:
        rest_factor = REST_DAY_DEFAULT
    elif days >= 10:
        rest_factor = REST_DAY_LONG
    else:
        rest_factor = REST_DAY_FACTORS.get(days, REST_DAY_DEFAULT)

    return round(max(blended * rest_factor, 1.0), 3), rest_factor


# ── Team batting splits vs LHP / RHP ─────────────────────────────────────────
def load_team_batting_splits() -> dict:
    """
    Returns {team: {"vs_lhp": adj, "vs_rhp": adj}} where adj is normalized
    relative to league average (same scale as offense_adj from load_offense).
    Pulls individual player stats split by sitCode=vl (vs left) and vr (vs right),
    aggregates runs scored by team, then normalizes.
    """
    import requests as _req

    def _fetch_split(sit_code: str) -> dict:
        """Returns {team: runs_per_game} for the given sitCode."""
        url    = "https://statsapi.mlb.com/api/v1/stats"
        params = {"stats": "statSplits", "group": "hitting", "gameType": "R",
                  "season": 2026, "sportId": 1, "sitCodes": sit_code,
                  "playerPool": "All", "limit": 2000}
        try:
            resp = _req.get(url, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  [Batting splits ({sit_code}) failed: {e}]")
            return {}

        team_r  = {}
        team_g  = {}
        for item in data.get("stats", []):
            for split in item.get("splits", []):
                team_raw = split.get("team", {}).get("name", "")
                team     = TEAM_ABBREV.get(team_raw, team_raw)
                stat     = split.get("stat", {})
                try:
                    runs = float(stat.get("runs", 0))
                    gp   = float(stat.get("gamesPlayed", 1))
                except (ValueError, TypeError):
                    continue
                if gp < 1:
                    continue
                team_r[team]  = team_r.get(team, 0.0)  + runs
                team_g[team]  = team_g.get(team, 0.0)  + gp

        return {t: team_r[t] / team_g[t] for t in team_r if team_g[t] > 0}

    rpg_vs_l = _fetch_split("vl")
    rpg_vs_r = _fetch_split("vr")

    if not rpg_vs_l or not rpg_vs_r:
        return {}

    # Normalize: adj = (team_rpg - league_mean) / league_mean
    def _normalize(rpg_dict: dict) -> dict:
        if not rpg_dict:
            return {}
        mean = sum(rpg_dict.values()) / len(rpg_dict)
        if mean == 0:
            return {t: 0.0 for t in rpg_dict}
        return {t: round((v - mean) / mean, 4) for t, v in rpg_dict.items()}

    vs_lhp = _normalize(rpg_vs_l)
    vs_rhp = _normalize(rpg_vs_r)

    teams = set(list(vs_lhp.keys()) + list(vs_rhp.keys()))
    return {t: {"vs_lhp": vs_lhp.get(t, 0.0), "vs_rhp": vs_rhp.get(t, 0.0)}
            for t in teams}


# ── SP History logger ─────────────────────────────────────────────────────────
SP_HISTORY_HEADER = [
    "Date", "Team", "Pitcher", "Throws",
    "Season ERA Est", "xFIP",
    "Last 3 Starts ERA", "Last 5 Starts ERA",
    "Days Rest", "Rest Factor", "Blended ERA Used",
    "Last Start Date",
]

def store_sp_history(gc, pitchers: dict, sp_profiles: dict,
                     blended_eras: dict, today: str) -> None:
    """
    Append today's SP projections to 'SP History' tab in the Data Sheet.
    Newest rows inserted at top so most recent is always visible first.
    One-row-per-SP-per-day; skips if today already present (first-run protection).
    """
    try:
        sh = gc.open_by_key(DATA_SHEET_ID)
        try:
            ws = sh.worksheet("SP History")
        except Exception:
            ws = sh.add_worksheet(title="SP History", rows=2000, cols=20)

        existing = ws.get_all_values()
        has_header = existing and existing[0] and existing[0][0] == "Date"
        if has_header and any(row[0] == today for row in existing[1:] if row):
            print("  SP History: today already logged — skipping")
            return

        rows = []
        for team, sp in pitchers.items():
            prof      = sp_profiles.get(team, {})
            era_est   = sp.get("era_est", LEAGUE_AVG_ERA)
            xfip      = sp.get("xfip", "")
            blended   = blended_eras.get(team, era_est)
            _, rest_f = blend_sp_era(era_est, prof) if prof else (era_est, REST_DAY_DEFAULT)

            rows.append([
                today,
                team,
                sp.get("name", "Unknown"),
                prof.get("handedness", ""),
                round(era_est, 3),
                round(xfip, 3) if xfip else "",
                prof.get("era_3", "") or "",
                prof.get("era_5", "") or "",
                prof.get("days_rest", "") if prof.get("days_rest") is not None else "",
                rest_f,
                round(blended, 3),
                prof.get("last_start_date", "") or "",
            ])

        if not rows:
            return

        if not has_header:
            ws.update(values=[SP_HISTORY_HEADER] + rows, range_name="A1",
                      value_input_option="RAW")
        else:
            ws.insert_rows(rows, row=2, value_input_option="RAW")

        print(f"  SP History: logged {len(rows)} pitchers for {today}")

    except Exception as e:
        print(f"  [SP History log failed: {e}]")


# ── Rolling offense loader (last 21 days, blended 60/40 with season avg) ──────
def load_rolling_offense(season_offense: dict, days: int = 21) -> dict:
    """
    Pulls team run totals for the last N days from MLB Stats API schedule.
    Returns blended offense adj: 60% recent × 40% season.
    """
    import requests as _req
    end_dt   = datetime.now()
    start_dt = end_dt - timedelta(days=days)
    url      = "https://statsapi.mlb.com/api/v1/schedule"
    params   = {
        "sportId": 1,
        "startDate": start_dt.strftime("%Y-%m-%d"),
        "endDate":   (end_dt - timedelta(days=1)).strftime("%Y-%m-%d"),
        "gameType":  "R",
        "hydrate":   "linescore",
    }
    try:
        resp = _req.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [Rolling offense load failed: {e}] — using season avg only")
        return season_offense

    team_runs  = {}
    team_games = {}
    for date_block in data.get("dates", []):
        for game in date_block.get("games", []):
            status = game.get("status", {}).get("abstractGameState", "")
            if status != "Final":
                continue
            ls     = game.get("linescore", {})
            teams  = game.get("teams", {})
            away   = teams.get("away", {}).get("team", {}).get("name", "")
            home   = teams.get("home", {}).get("team", {}).get("name", "")
            away_r = ls.get("teams", {}).get("away", {}).get("runs", None)
            home_r = ls.get("teams", {}).get("home", {}).get("runs", None)
            if away_r is None or home_r is None:
                continue
            for team_raw, runs in [(away, away_r), (home, home_r)]:
                team = TEAM_ABBREV.get(team_raw, team_raw)
                team_runs[team]  = team_runs.get(team, 0.0)  + float(runs)
                team_games[team] = team_games.get(team, 0)   + 1

    if not team_runs:
        return season_offense

    # Build recent offense adjustment (same normalization as season)
    recent_rpg  = {t: team_runs[t] / team_games[t] for t in team_runs if team_games[t] >= 5}
    if not recent_rpg:
        return season_offense
    mean_recent = sum(recent_rpg.values()) / len(recent_rpg)
    recent_adj  = {t: (v - mean_recent) / mean_recent for t, v in recent_rpg.items()} if mean_recent else {}

    # Blend: 60% recent, 40% season
    blended = {}
    for team in set(list(season_offense.keys()) + list(recent_adj.keys())):
        s = season_offense.get(team, 0.0)
        r = recent_adj.get(team, s)   # fall back to season if no recent data
        blended[team] = round(0.60 * r + 0.40 * s, 4)
    return blended


# ── Weather factor lookup (NWS API — no key required) ─────────────────────────
def fetch_weather_factor(lat: float, lon: float, game_time_utc: datetime) -> dict:
    """
    Returns dict with wind_mph, wind_dir, temp_f, wind_factor, temp_factor.
    Uses National Weather Service API (free, no API key).
    """
    import requests as _req
    empty = {"wind_mph": 0, "wind_dir": "", "temp_f": 72,
             "wind_factor": 0.0, "temp_factor": 0.0, "description": "N/A"}
    try:
        headers = {"User-Agent": "FantasySixPack/1.0 (fantasysixpack.net)"}
        # Step 1 — get gridpoint
        pts = _req.get(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}",
                       headers=headers, timeout=10)
        pts.raise_for_status()
        hourly_url = pts.json()["properties"]["forecastHourly"]

        # Step 2 — get hourly forecast
        fc = _req.get(hourly_url, headers=headers, timeout=10)
        fc.raise_for_status()
        periods = fc.json()["properties"]["periods"]

        # Find period closest to game time
        best = min(
            periods,
            key=lambda p: abs((datetime.fromisoformat(p["startTime"]) - game_time_utc).total_seconds()),
            default=None,
        )
        if not best:
            return empty

        wind_raw = best.get("windSpeed", "0 mph")
        wind_dir = best.get("windDirection", "")
        temp_f   = int(best.get("temperature", 72))
        try:
            wind_mph = int(str(wind_raw).split()[0])
        except (ValueError, IndexError):
            wind_mph = 0

        # Wind factor: high wind adds variance → slight over lean
        if wind_mph >= 20:
            wind_factor = 0.40
        elif wind_mph >= 15:
            wind_factor = 0.25
        elif wind_mph >= 10:
            wind_factor = 0.12
        elif wind_mph >= 5:
            wind_factor = 0.05
        else:
            wind_factor = 0.0

        # Temperature factor: cold suppresses offense, heat adds carry
        if temp_f < 45:
            temp_factor = -0.50
        elif temp_f < 55:
            temp_factor = -0.30
        elif temp_f < 62:
            temp_factor = -0.15
        elif temp_f > 90:
            temp_factor = +0.25
        elif temp_f > 82:
            temp_factor = +0.15
        elif temp_f > 75:
            temp_factor = +0.08
        else:
            temp_factor = 0.0

        return {
            "wind_mph":    wind_mph,
            "wind_dir":    wind_dir,
            "temp_f":      temp_f,
            "wind_factor": round(wind_factor, 3),
            "temp_factor": round(temp_factor, 3),
            "description": f"{temp_f}°F, {wind_mph}mph {wind_dir}",
        }
    except Exception as e:
        return {**empty, "description": f"unavailable ({type(e).__name__})"}


# ── Google Sheets helpers ─────────────────────────────────────────────────────
def auth():
    creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


def ws(gc, sheet_id, tab):
    sh = gc.open_by_key(sheet_id)
    try:
        return sh.worksheet(tab)
    except Exception:
        # Tab doesn't exist yet — create it
        return sh.add_worksheet(title=tab, rows=2000, cols=40)


def sheet_to_dicts(worksheet) -> list[dict]:
    try:
        return worksheet.get_all_records(default_blank="")
    except Exception:
        # Fall back for sheets with duplicate/empty headers
        rows = worksheet.get_all_values()
        if not rows:
            return []
        header = rows[0]
        result = []
        for row in rows[1:]:
            # Pad short rows
            padded = row + [""] * (len(header) - len(row))
            d = {}
            for i, key in enumerate(header):
                if key and key not in d:
                    d[key] = padded[i]
            result.append(d)
        return result


# ── Probability helpers ───────────────────────────────────────────────────────
def american_to_implied(price: float) -> float:
    """Convert American odds to implied probability (0–1)."""
    if price >= 100:
        return 100 / (price + 100)
    else:
        return abs(price) / (abs(price) + 100)


def normal_cdf(x: float, mean: float = 0.0, sd: float = 1.0) -> float:
    """Standard normal CDF via math.erf."""
    z = (x - mean) / (sd * math.sqrt(2))
    return 0.5 * (1 + math.erf(z))


# ── Cross-bet-type confidence percentile ──────────────────────────────────────
# Game Total, Moneyline, and Run Line each use their own edge metric and their
# own calibrated star scale — a "15% edge" means something different in each.
# To let the user compare confidence across bet types (e.g. when units/stars tie),
# we rank each bet's edge against the historical distribution of edges for its
# OWN bet type, producing a 0-100 percentile that IS directly comparable across
# types even though the underlying math isn't.
def load_historical_edges(gc) -> dict:
    """Returns {"Game Total": [...], "Moneyline": [...], "Run Line": [...], "Team Total": [...]}
    of historical edge_pct values pulled from shadow/props tabs."""
    history = {"Game Total": [], "Moneyline": [], "Run Line": [], "Team Total": []}

    try:
        gt_ws = ws(gc, ODDS_SHEET_ID, "Game Totals")
        gt_rows = gt_ws.get_all_values()
        if gt_rows:
            gt_hdr = gt_rows[0]
            gi = {h: i for i, h in enumerate(gt_hdr)}
            for r in gt_rows[1:]:
                if not r:
                    continue
                if gi.get("Pitcher Flag") is not None and r[gi["Pitcher Flag"]].strip() == "Missing SP":
                    continue
                try:
                    history["Game Total"].append(float(r[gi["Edge % of Line"]].replace("%", "")))
                except (ValueError, TypeError, KeyError, IndexError):
                    continue
    except Exception:
        pass

    try:
        ml_ws = ws(gc, ODDS_SHEET_ID, "ML RL")
        ml_rows = ml_ws.get_all_values()
        if ml_rows:
            ml_hdr = ml_rows[0]
            mi = {h: i for i, h in enumerate(ml_hdr)}
            for r in ml_rows[1:]:
                if not r:
                    continue
                if mi.get("Pitcher Flag") is not None and r[mi["Pitcher Flag"]].strip() == "Missing SP":
                    continue
                bet_type = r[mi["Bet Type"]] if "Bet Type" in mi else ""
                if bet_type not in ("Moneyline", "Run Line"):
                    continue
                try:
                    history[bet_type].append(float(r[mi["Edge vs Book%"]].replace("%", "")))
                except (ValueError, TypeError, KeyError, IndexError):
                    continue
    except Exception:
        pass

    try:
        tt_ws = ws(gc, ODDS_SHEET_ID, "Team Totals")
        tt_rows = tt_ws.get_all_values()
        if tt_rows:
            tt_hdr = tt_rows[0]
            ti = {h: i for i, h in enumerate(tt_hdr)}
            for r in tt_rows[1:]:
                if not r:
                    continue
                if ti.get("Result") is not None and r[ti["Result"]].strip() not in ("Win", "Loss", "Push"):
                    continue
                try:
                    history["Team Total"].append(float(str(r[ti["Edge %"]]).replace("%", "")))
                except (ValueError, TypeError, KeyError, IndexError):
                    continue
    except Exception:
        pass

    return history


def confidence_percentile(edge_val: float, historical_edges: list) -> float:
    """Percentile rank (0-100) of edge_val within historical_edges for its bet type."""
    if not historical_edges:
        return 50.0
    n = len(historical_edges)
    below = sum(1 for e in historical_edges if e <= edge_val)
    return round(below / n * 100, 1)


def unit_scale(edge, scale_points: list[tuple]) -> float:
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
    return scale_points[-1][1]


TOTAL_SCALE = [
    # Recalibrated 2026-06-14: shifted thresholds up based on 48-game sample.
    # Data showed edge 1.0-1.5 was 7-11 (39%), edge 1.5+ was 14-7 (67%).
    # 4-star (Bet History minimum) now starts at 1.50 run edge (was 1.10).
    # 5-star now starts at 1.90 run edge (was 1.50).
    (0.75, 0.3), (1.10, 0.4), (1.50, 0.5), (1.65, 0.6),
    (1.90, 0.7), (2.05, 0.8), (2.20, 0.9), (2.40, 1.0),
]
TEAM_TOTAL_SCALE = [
    (8.0, 0.3), (10.0, 0.4), (12.0, 0.5), (14.0, 0.6),
    (16.0, 0.7), (18.0, 0.8), (20.0, 0.9), (22.0, 1.0),
]
# ── Moneyline & Run Line are tracked with SEPARATE scales as of 2026-06-21 ──────
# They are different bets with measurably different edge distributions and different
# current performance (Moneyline: median edge 9.1%, currently -0.064 ROI/bet, 45.9% win
# across 61 graded; Run Line: median edge 10.4%, currently +0.036 ROI/bet, 53.6% win
# across 28 graded). Treating them as one pool was masking that divergence.
# Both are "best current estimate" — NOT yet performance-validated (see caveat below).
# IMPORTANT CAVEAT: at this sample size neither scale has enough graded volume to confirm
# a real edge_pct-to-performance relationship. Thresholds reflect "how unusual is this
# edge relative to what we generate for this bet type," not a proven profitability claim.
# Proactive recalibration check runs automatically every morning in grade_bets.py
# (check_moneyline_calibration / check_run_line_calibration) — see 'Calibration Tracker' tab.
# NOTE: unit_scale() rounds interpolated units to 1 decimal, which makes the true
# star-tier transition land at the MIDPOINT between consecutive scale points, not at
# the upper point itself — midpoints below are tuned with that in mind.
ML_SCALE = [
    # Moneyline: midpoints anchored to p50≈9.1%, p80≈14.1% across 70 rows (61 graded).
    (4.0, 0.3), (7.0, 0.4), (11.3, 0.5), (12.5, 0.6),
    (15.8, 0.7), (19.0, 0.8), (23.0, 0.9), (28.3, 1.0),
]
RL_SCALE = [
    # Run Line: recalibrated 2026-06-30, then corrected same day. First pass anchored
    # the 1.0u ceiling to the single highest edge ever observed (25.41%) — but that
    # makes ONE outlier data point the sole 1.0u earner and continues to finely
    # differentiate within the "elite" tier even though 75 rows isn't enough data to
    # say a 24% edge is meaningfully better than a 22% edge. Switched to a PLATEAU:
    # any edge >= 22% (~top 4% of all RL edges ever seen) earns the full 1.0u, since
    # past that point we're in "rare, exceptional" territory and shouldn't pretend to
    # rank within it. Star-tier midpoints unchanged: p50≈9.5%, p80≈14.5% across 75 rows.
    (4.0, 0.3), (7.0, 0.4), (12.0, 0.5), (13.0, 0.6),
    (16.0, 0.7), (18.0, 0.8), (20.0, 0.9), (22.0, 1.0),
]


# ── Player props scales ───────────────────────────────────────────────────────
# All props are early-stage — these are best-guess starting points, not calibrated.
# Proactive calibration checks will fire once each type hits its threshold.
# Edge % here = (our_proj - book_line) / book_line * 100
PROPS_K_SCALE = [
    # SP Strikeouts: book lines typically 4.5-7.5. Edge = (proj - line) / line * 100
    (5.0, 0.3), (8.0, 0.4), (11.0, 0.5), (13.0, 0.6),
    (16.0, 0.7), (19.0, 0.8), (22.0, 0.9), (25.0, 1.0),
]
PROPS_TB_SCALE = [
    # Batter Total Bases: edge = (our_win_prob - book_implied) / book_implied * 100.
    # Poisson-based — real edge vs book's implied. 8% real edge ~ 4 stars.
    (4.0, 0.3), (6.0, 0.4), (8.0, 0.5), (11.0, 0.6),
    (14.0, 0.7), (18.0, 0.8), (22.0, 0.9), (27.0, 1.0),
]
PROPS_HRR_SCALE = [
    # H+R+RBI: same Poisson-based real edge. Wider variance → slightly higher thresholds.
    (5.0, 0.3), (7.0, 0.4), (9.0, 0.5), (12.0, 0.6),
    (15.0, 0.7), (19.0, 0.8), (23.0, 0.9), (28.0, 1.0),
]
TEAM_TOTAL_SCALE = [
    # Team Totals: lines typically 2.0-3.5. Similar variance to game totals halved.
    (5.0, 0.3), (8.0, 0.4), (11.0, 0.5), (13.0, 0.6),
    (16.0, 0.7), (19.0, 0.8), (22.0, 0.9), (25.0, 1.0),
]
# HR props: capped at 4 stars, max 0.2u regardless of edge, max 1 per 4 games
HR_MAX_UNITS  = 0.2
HR_MAX_STARS  = 4
HR_GAMES_RATIO = 4  # 1 HR bet allowed per this many games on slate

# ── Player props projection constants ────────────────────────────────────────
LEAGUE_AVG_K_PCT        = 0.224   # MLB-wide K% per batter faced (~22.4%, 2024-25 avg)
LEAGUE_AVG_BATTERS_PER_IP = 4.30  # average BF/IP across MLB starters
EXPECTED_SP_IP          = 5.5     # expected innings for a typical SP start
EXPECTED_PA_PER_GAME    = 4.0     # average plate appearances per batter per game
PA_TO_AB_RATIO          = 0.865   # AB/PA league average (excludes BB, HBP, SAC)
LEAGUE_AVG_BARREL_PCT   = 8.5     # MLB-wide barrel rate %
LEAGUE_AVG_ISO          = 0.165   # MLB-wide ISO (SLG - AVG)


def stars_from_units(units: float) -> int:
    if units >= 0.7:
        return 5
    if units >= 0.5:
        return 4
    if units >= 0.3:
        return 3
    return 0

def poisson_cdf(k: int, lam: float) -> float:
    """P(X <= k) for Poisson(lam). Uses math to avoid scipy dependency."""
    if lam <= 0:
        return 1.0
    total = 0.0
    log_lam = math.log(lam)
    log_term = -lam  # log(e^-lam * lam^0 / 0!)
    total = math.exp(log_term)
    for i in range(1, int(k) + 1):
        log_term += log_lam - math.log(i)
        total += math.exp(log_term)
    return min(1.0, total)

def prop_win_prob(proj: float, line: float, direction: str) -> float:
    """
    True win probability for a discrete prop (H+R+RBI, Total Bases) modeled as Poisson.
    For Over line 1.5 → need X >= 2 → P(X >= 2) = 1 - P(X <= 1).
    For Under line 1.5 → need X <= 1 → P(X <= 1).
    """
    k_floor = int(line)  # floor: for line=1.5 → k=1
    if direction == "Over":
        return 1.0 - poisson_cdf(k_floor, proj)
    else:
        return poisson_cdf(k_floor, proj)

def stars_emoji(n: int) -> str:
    return "⭐" * n


# ── Data loaders ──────────────────────────────────────────────────────────────
MIN_IP_FOR_FULL_TRUST = 15.0  # ~3 starts. Below this, season-long rate stats (xFIP/xERA/SIERA)
# are unstable — a single rough/short outing can produce an absurd ERA-scale number
# (e.g. Shane Bieber's 1-start, 3.2 IP return from injury produced xERA=34.3, season
# composite est=15.96 on 2026-06-28). Shrink linearly toward LEAGUE_AVG_ERA as IP
# approaches 0, full trust at MIN_IP_FOR_FULL_TRUST+.


def load_pitchers(gc) -> dict:
    """
    Returns {team_name: era_est} from MLB Daily Pitching Data tab.
    Composite: xFIP*0.40 + xERA*0.35 + SIERA*0.25, fallbacks: xFIP → ERA → 4.00
    Shrunk toward LEAGUE_AVG_ERA when season IP is too low to trust (see MIN_IP_FOR_FULL_TRUST).
    """
    data = sheet_to_dicts(ws(gc, DATA_SHEET_ID, "MLB Daily Pitching Data"))
    pitchers = {}
    for row in data:
        name = str(row.get("Pitcher", row.get("pitcher", ""))).strip()
        team_raw = str(row.get("Team", row.get("team", ""))).strip()
        if not team_raw:
            continue
        # Translate abbreviation to full name if needed
        team = TEAM_ABBREV.get(team_raw, team_raw)

        def safe(col_names):
            for c in col_names:
                v = row.get(c, "")
                try:
                    return float(v)
                except (ValueError, TypeError):
                    continue
            return None

        xfip  = safe(["xFIP",  "xfip"])
        xera  = safe(["xERA",  "xera"])
        siera = safe(["SIERA", "siera"])
        era   = safe(["ERA",   "era"])
        ip    = safe(["IP",    "ip"])

        if xfip is not None and xera is not None and siera is not None:
            composite = xfip * 0.40 + xera * 0.35 + siera * 0.25
        elif xfip is not None:
            composite = xfip
        elif era is not None:
            composite = era
        else:
            composite = LEAGUE_AVG_ERA

        if ip is not None and ip < MIN_IP_FOR_FULL_TRUST:
            weight = max(ip, 0.0) / MIN_IP_FOR_FULL_TRUST
            est = weight * composite + (1 - weight) * LEAGUE_AVG_ERA
        else:
            est = composite

        k_pct      = safe(["K%",         "k_pct",    "kpct"])
        k_pct_opp  = safe(["K% (Opp)",   "k_pct_opp","kpct_opp"])
        swstr      = safe(["SwStr%",      "swstr_pct","swstr"])
        swstr_opp  = safe(["SwStr% (Opp)","swstr_opp"])
        so_season  = safe(["SO",          "so",       "strikeouts"])

        pitchers[team] = {
            "name":       name,
            "era_est":    round(est, 3),
            "xfip":       xfip,
            "era":        era,
            "ip":         ip,
            "k_pct":      k_pct,
            "k_pct_opp":  k_pct_opp,
            "swstr":      swstr,
            "swstr_opp":  swstr_opp,
            "so_season":  so_season,
        }
    return pitchers


def load_offense(gc) -> dict:
    """
    Returns {team_name: offense_adj} from Dave(H) tab.
    offense_adj = (team_sum - mean) / mean across all teams with dave_h > 0.
    """
    data   = sheet_to_dicts(ws(gc, DATA_SHEET_ID, "Dave(H)"))
    totals = {}
    for row in data:
        team_raw = str(row.get("Team", row.get("team", ""))).strip()
        if not team_raw:
            continue
        team = TEAM_ABBREV.get(team_raw, team_raw)
        try:
            val = float(row.get("Dave+", row.get("total", row.get("Total", 0))) or 0)
        except (ValueError, TypeError):
            val = 0.0
        if val > 0 and team:
            totals[team] = totals.get(team, 0.0) + val

    if not totals:
        return {}

    mean_val = sum(totals.values()) / len(totals)
    if mean_val == 0:
        return {t: 0.0 for t in totals}

    return {t: (s - mean_val) / mean_val for t, s in totals.items()}


GAME_LINE_MARKETS = {"h2h", "spreads", "totals", "alternate_totals"}

def load_odds(gc) -> list[dict]:
    """Returns only game-line rows from MLB Odds (h2h, spreads, totals)."""
    all_rows = sheet_to_dicts(ws(gc, ODDS_SHEET_ID, "MLB Odds"))
    return [r for r in all_rows if r.get("market_key", "").strip() in GAME_LINE_MARKETS]


def load_batter_stats(gc) -> dict:
    """
    Returns {player_name_lower: stat_dict} from MLB Daily Hitter Data tab.
    Also merges individual Dave+ from Dave(H) tab (pre-computed matchup-adjusted score).
    Keyed by lowercase player name for matching against prop odds player names.
    Also keyed by player_id for reliable cross-source lookup.
    """
    try:
        data = sheet_to_dicts(ws(gc, DATA_SHEET_ID, "MLB Daily Hitter Data"))
    except Exception:
        return {}

    # Load Dave(H) for individual batter Dave+ (already factors in opposing pitcher)
    dave_h = {}
    try:
        for row in sheet_to_dicts(ws(gc, DATA_SHEET_ID, "Dave(H)")):
            pid  = str(row.get("ID", "")).strip()
            davep = row.get("Dave+", "")
            try:    dave_h[pid] = float(davep)
            except: pass
    except Exception:
        pass

    batters = {}
    for row in data:
        name = str(row.get("Player", row.get("player", ""))).strip()
        if not name:
            continue
        team_raw  = str(row.get("Team", row.get("team", ""))).strip()
        team      = TEAM_ABBREV.get(team_raw, team_raw)
        player_id = str(row.get("Player ID", row.get("ID", ""))).strip()

        def sf(cols):
            for c in cols:
                v = row.get(c, "")
                try:    return float(str(v).replace("%", "").strip())
                except: continue
            return None

        entry = {
            "name":       name,
            "team":       team,
            "player_id":  player_id,
            "dave_plus":  dave_h.get(player_id),   # matchup-adjusted composite score
            "pa":         sf(["PA"]),
            "hr":         sf(["HR"]),
            "r":          sf(["R"]),
            "rbi":        sf(["RBI"]),
            "avg":        sf(["AVG"]),
            "xba":        sf(["xBA"]),
            "slg":        sf(["SLG"]),
            "xslg":       sf(["xSLG%", "xSLG"]),
            "iso14d":     sf(["ISO14d"]),
            "iso30d":     sf(["ISO30d"]),
            "woba14d":    sf(["wOBA14d"]),
            "woba30d":    sf(["wOBA30d"]),
            "barrel_pct": sf(["Barrel%"]),
            "hard_hit":   sf(["Hard Hit%"]),
            "wrc_plus14": sf(["wRC+14d"]),
        }
        batters[name.lower()] = entry
        if player_id:
            batters[f"id:{player_id}"] = entry  # secondary lookup by ID
    return batters


def load_sp_opp_stats(gc) -> dict:
    """
    Returns {team_abbrev: {k_pct, swstr, wrc_plus, woba, iso}} from MLB Pitchers Opp Data.
    Used for opponent-team adjustment in SP strikeout projections.
    More reliable than the per-pitcher K% (Opp) field since it's a dedicated team-level tab.
    """
    try:
        data = sheet_to_dicts(ws(gc, DATA_SHEET_ID, "MLB Pitchers Opp Data"))
    except Exception:
        return {}

    opp_stats = {}
    for row in data:
        team_raw = str(row.get("Team", "")).strip()
        if not team_raw:
            continue
        team = TEAM_ABBREV.get(team_raw, team_raw)

        def sf(col):
            try:    return float(str(row.get(col, "")).replace("%", "").strip())
            except: return None

        opp_stats[team] = {
            "k_pct":    sf("K%"),
            "swstr":    sf("SwStr%"),
            "wrc_plus": sf("wRC+"),
            "woba":     sf("wOBA"),
            "iso":      sf("ISO"),
        }
    return opp_stats


PROP_MARKETS = {
    "team_totals",
    "pitcher_strikeouts", "batter_total_bases", "batter_home_runs", "batter_hits_runs_rbis",
}

def load_prop_odds(gc) -> list[dict]:
    """Returns prop/team-total rows from the MLB Odds tab (market_key in PROP_MARKETS)."""
    try:
        all_rows = sheet_to_dicts(ws(gc, ODDS_SHEET_ID, "MLB Odds"))
        return [r for r in all_rows if r.get("market_key", "").strip() in PROP_MARKETS]
    except Exception:
        return []


# ── Game projection ───────────────────────────────────────────────────────────
def project_game(home_team, away_team, home_era, away_era,
                 home_off, away_off, park_factor,
                 home_bullpen_era=None, away_bullpen_era=None,
                 away_off_vs_sp=None, home_off_vs_sp=None) -> dict:
    """
    Projects runs scored for each team.
    SP innings (5/9): uses SP ERA + platoon-adjusted offense for the specific L/R matchup.
    Bullpen innings (4/9): uses bullpen ERA + generic offense (mixed handedness).
    home_era / away_era: already blended with recent form + rest day factor before calling.
    away_off_vs_sp / home_off_vs_sp: platoon-adjusted offense vs opposing SP handedness.
    """
    pf   = park_factor / 100
    h_bp = home_bullpen_era if home_bullpen_era is not None else LEAGUE_AVG_BULLPEN_ERA
    a_bp = away_bullpen_era if away_bullpen_era is not None else LEAGUE_AVG_BULLPEN_ERA

    sp_w = SP_INNINGS      / TOTAL_INNINGS   # 5/9
    bp_w = BULLPEN_INNINGS / TOTAL_INNINGS   # 4/9

    # Platoon-adjusted offense for SP innings; generic for bullpen innings
    a_off_sp = away_off_vs_sp if away_off_vs_sp is not None else away_off
    h_off_sp = home_off_vs_sp if home_off_vs_sp is not None else home_off

    # Away runs scored (vs home pitching)
    proj_away = (
        BASE_RUNS * sp_w * (home_era / LEAGUE_AVG_ERA) * (1 + OFFENSE_WEIGHT * a_off_sp) * pf +
        BASE_RUNS * bp_w * (h_bp     / LEAGUE_AVG_BULLPEN_ERA) * (1 + OFFENSE_WEIGHT * away_off) * pf
    )
    # Home runs scored (vs away pitching)
    proj_home = (
        BASE_RUNS * sp_w * (away_era / LEAGUE_AVG_ERA) * (1 + OFFENSE_WEIGHT * h_off_sp) * pf +
        BASE_RUNS * bp_w * (a_bp     / LEAGUE_AVG_BULLPEN_ERA) * (1 + OFFENSE_WEIGHT * home_off) * pf
    )

    proj_away = max(proj_away, 1.5)
    proj_home = max(proj_home, 1.5)

    proj_total_raw = proj_away + proj_home

    # Pythagorean expectation: reduced from 1.83 (season-level standard) to 1.3
    # because single-game win probability discriminates less than season-level stats.
    # Data shows: proj +1.75 run diff = 64.5% actual win; EXP=1.3 gives 63.8% (well-calibrated).
    # EXP=1.83 was over-converting modest run advantages into 65-70% projections that only won 50-52%.
    PYTH_EXP = 1.3
    home_sq = proj_home ** PYTH_EXP
    away_sq = proj_away ** PYTH_EXP
    denom   = home_sq + away_sq if (home_sq + away_sq) > 0 else 1
    home_win_raw = home_sq / denom
    # Add explicit home field advantage (+2.5%); MLB home teams win ~54% historically
    # and the run projection formula is symmetric (no HFA), so this is the residual adjustment
    home_win = max(MIN_WIN_PCT, min(MAX_WIN_PCT, home_win_raw + 0.025))
    away_win = 1.0 - home_win

    proj_run_diff = proj_home - proj_away
    home_rl_pct   = 1.0 - normal_cdf(1.5, proj_run_diff, RUN_LINE_SD)
    away_rl_pct   = normal_cdf(-1.5, proj_run_diff, RUN_LINE_SD)

    return {
        "proj_away":        round(proj_away, 3),
        "proj_home":        round(proj_home, 3),
        "proj_total_raw":   round(proj_total_raw, 2),
        "home_win":         round(home_win, 4),
        "away_win":         round(away_win, 4),
        "home_win_raw":     round(home_win_raw, 4),
        "proj_run_diff":    round(proj_run_diff, 3),
        "home_rl_pct":      round(home_rl_pct, 4),
        "away_rl_pct":      round(away_rl_pct, 4),
    }


# ── Build game universe from odds ─────────────────────────────────────────────
def build_game_universe(odds_rows: list[dict]) -> dict:
    """
    Group rows by game_id. Returns dict of game_id → game meta.
    Filter out games commencing within 10 minutes of now.
    """
    now    = datetime.now(timezone.utc)
    cutoff = now + timedelta(minutes=10)
    games  = {}
    for row in odds_rows:
        gid     = row.get("game_id", "")
        commence = row.get("commence_time", "")
        if not gid or not commence:
            continue
        try:
            # ISO format may end in Z or +00:00
            ct = datetime.fromisoformat(commence.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ct <= cutoff:
            continue
        if gid not in games:
            games[gid] = {
                "game_id":       gid,
                "home_team":     row.get("home_team", ""),
                "away_team":     row.get("away_team", ""),
                "commence_time": ct,
                "venue":         "",   # filled in below
            }
    return games


def attach_venues(games: dict, venue_map: dict) -> None:
    """Fill in venue name for each game from the MLB Stats API venue map."""
    for gid, game in games.items():
        key = (game["away_team"], game["home_team"])
        game["venue"] = venue_map.get(key, "Unknown")


def build_book_lines(odds_rows: list[dict], games: dict) -> dict:
    """
    For each game_id → {book → {h2h_home, h2h_away, total_line, total_over_price, ...}}
    """
    lines = {gid: {} for gid in games}
    for row in odds_rows:
        gid  = row.get("game_id", "")
        if gid not in lines:
            continue
        book   = str(row.get("sportsbook", "")).lower()
        if book not in BOOKS_TO_KEEP:
            continue
        market = row.get("market_key", "")
        name   = str(row.get("name", ""))
        price  = row.get("price", "")
        point  = row.get("point", "")

        try:
            price = float(price)
        except (ValueError, TypeError):
            price = None
        try:
            point = float(point)
        except (ValueError, TypeError):
            point = None

        if book not in lines[gid]:
            lines[gid][book] = {}
        bl = lines[gid][book]
        home = games[gid]["home_team"]
        away = games[gid]["away_team"]

        if market == "h2h":
            if name == home:
                bl["h2h_home_price"] = price
            elif name == away:
                bl["h2h_away_price"] = price
        elif market == "spreads":
            if name == home:
                bl["rl_home_point"] = point
                bl["rl_home_price"] = price
            elif name == away:
                bl["rl_away_point"] = point
                bl["rl_away_price"] = price
        elif market == "totals":
            if name == "Over":
                bl["total_line"]        = point
                bl["total_over_price"]  = price
            elif name == "Under":
                bl["total_under_price"] = price

    return lines


# ── Player props projections ──────────────────────────────────────────────────

def project_sp_strikeouts(sp: dict, opp_team_stats: dict = None) -> float | None:
    """
    Project strikeouts for a starting pitcher today.
    Blends season K% with SwStr%-implied K% (SwStr% × 2 ≈ K%), then scales
    by opposing team K% from MLB Pitchers Opp Data (preferred) or pitcher row fallback.
    Returns projected K total for the start, or None if insufficient data.
    """
    k_pct = sp.get("k_pct")
    swstr = sp.get("swstr")

    if k_pct is None:
        return None

    def to_frac(v):
        if v is None: return None
        return v / 100.0 if v > 1.0 else v

    k_pct = to_frac(k_pct)
    swstr = to_frac(swstr)

    # Blend SP's own K% with SwStr%-implied K% (0.7/0.3 split)
    if swstr is not None:
        blended_k = 0.70 * k_pct + 0.30 * (swstr * 2.0)
    else:
        blended_k = k_pct

    # Opponent K% — prefer dedicated Opp Data tab over pitcher row field
    opp_k = None
    if opp_team_stats:
        opp_k = opp_team_stats.get("k_pct")
        if opp_k and opp_k > 1.0:
            opp_k /= 100.0
    if opp_k is None:
        fallback = sp.get("k_pct_opp")
        opp_k = to_frac(fallback)

    opp_adj = (opp_k / LEAGUE_AVG_K_PCT) if opp_k else 1.0
    proj_k  = blended_k * LEAGUE_AVG_BATTERS_PER_IP * EXPECTED_SP_IP * opp_adj
    return round(proj_k, 2)


def project_batter_tb(batter: dict, park_factor: int = 100) -> float | None:
    """
    Project total bases for a batter in today's game.
    xSLG × PA_to_AB × expected_PA, blended with recent ISO trend.
    Returns projected TB, or None if insufficient data.
    """
    xslg    = batter.get("xslg")
    iso14d  = batter.get("iso14d")
    iso30d  = batter.get("iso30d")
    pa      = batter.get("pa")

    if xslg is None or pa is None or pa < 200:
        return None

    # xSLG is TB per AB → convert to per PA
    base_tb_rate = xslg * PA_TO_AB_RATIO

    # Recent form modifier: ISO trend (14d vs 30d) adjusts by up to ±10%
    if iso14d is not None and iso30d is not None and iso30d > 0:
        trend = (iso14d / iso30d) - 1.0          # +0.1 = 10% hotter recently
        trend_adj = max(-0.10, min(0.10, trend))  # clamp ±10%
        base_tb_rate *= (1.0 + trend_adj)

    # Park factor adjustment (scaled down — TB is less park-sensitive than HRs)
    pf_adj = 1.0 + (park_factor - 100) / 100.0 * 0.20
    proj_tb = base_tb_rate * EXPECTED_PA_PER_GAME * pf_adj
    return round(proj_tb, 2)


def project_batter_hr(batter: dict, park_factor: int = 100) -> float | None:
    """
    Project home run probability for a batter in today's game.
    Season HR/PA rate, adjusted by Barrel% vs league average and park factor.
    Returns projected HR (will be a fraction like 0.08 for ~1 HR every 12 games).
    """
    hr  = batter.get("hr")
    pa  = batter.get("pa")
    barrel = batter.get("barrel_pct")
    iso14d = batter.get("iso14d")

    if hr is None or pa is None or pa < 200:
        return None

    hr_rate = hr / pa  # season HR per PA

    # Barrel adjustment: above/below league average barrel% scales HR rate
    if barrel is not None and barrel > 0:
        barrel_adj = barrel / LEAGUE_AVG_BARREL_PCT
        barrel_adj = max(0.5, min(2.0, barrel_adj))  # clamp extreme outliers
        hr_rate *= (0.70 + 0.30 * barrel_adj)        # 70% base + 30% barrel signal

    # Recent power signal via ISO14d
    if iso14d is not None:
        iso_adj = iso14d / max(LEAGUE_AVG_ISO, 0.01)
        iso_adj = max(0.5, min(2.0, iso_adj))
        hr_rate = 0.70 * hr_rate + 0.30 * (hr_rate * iso_adj)

    # Park factor (HR is the most park-sensitive stat — full factor)
    pf_adj  = 1.0 + (park_factor - 100) / 100.0 * 0.50
    proj_hr = hr_rate * EXPECTED_PA_PER_GAME * pf_adj
    return round(proj_hr, 3)


def project_batter_hrr(batter: dict) -> float | None:
    """
    Project Hits + Runs + RBIs for a batter in today's game.
    H: xBA × expected PA (converted to AB).
    R + RBI: season per-PA rates × expected PA.
    Returns projected H+R+RBI total.
    """
    xba = batter.get("xba")
    r   = batter.get("r")
    rbi = batter.get("rbi")
    pa  = batter.get("pa")

    if xba is None or r is None or rbi is None or pa is None or pa < 200:
        return None

    proj_h   = xba * EXPECTED_PA_PER_GAME * PA_TO_AB_RATIO
    proj_r   = (r   / pa) * EXPECTED_PA_PER_GAME
    proj_rbi = (rbi / pa) * EXPECTED_PA_PER_GAME

    # Recent form via wOBA14d (proxy for overall offensive output)
    woba14 = batter.get("woba14d")
    woba30 = batter.get("woba30d")
    if woba14 is not None and woba30 is not None and woba30 > 0:
        trend = (woba14 / woba30) - 1.0
        trend_adj = max(-0.10, min(0.10, trend))
        proj_h   *= (1.0 + trend_adj)
        proj_r   *= (1.0 + trend_adj * 0.5)   # R/RBI less sensitive to recent form
        proj_rbi *= (1.0 + trend_adj * 0.5)

    return round(proj_h + proj_r + proj_rbi, 2)


def _pitcher_batter_adj(pitchers: dict, batter_team: str, home: str, away: str) -> float:
    """
    Return projection multiplier for a batter based on opposing starter quality.
    ERA below league avg (good pitcher) → multiplier < 1.0 (reduce projection).
    ERA above league avg (bad pitcher)  → multiplier > 1.0 (boost projection).
    Clamped to [0.80, 1.15] so no single adjustment dominates.
    """
    opp_team = away if batter_team == home else (home if batter_team == away else None)
    if opp_team is None:
        return 1.0
    sp = pitchers.get(opp_team)
    if sp is None:
        return 1.0
    opp_era = sp.get("era_est") or LEAGUE_AVG_ERA
    return max(0.80, min(1.15, opp_era / LEAGUE_AVG_ERA))


def analyze_props(prop_odds: list[dict], pitchers: dict, batter_stats: dict,
                  games: dict, run_now: str, sp_opp_stats: dict = None,
                  game_projections: dict = None,
                  historical_edges: dict = None) -> tuple[list, list]:
    """
    Compare projections to book lines for all prop markets.
    Returns (team_total_rows, player_prop_rows).
    Only bets with ≥4 stars make it through (all edges logged).
    HR bets capped at 4 stars, 0.2u max, 1 per 4 games on slate.
    """
    if not prop_odds:
        return [], []

    # Build lookup: (home_team, away_team) → game dict for park factor
    game_by_teams = {}
    for gid, g in games.items():
        game_by_teams[(g["home_team"], g["away_team"])] = g

    # Best line per (player, market, direction) across books
    best_line: dict = {}
    for row in prop_odds:
        player    = str(row.get("player", "") or row.get("name", "")).strip()
        market    = str(row.get("market_key", "")).strip()
        direction = str(row.get("direction", "")).strip()
        home      = str(row.get("home_team", "")).strip()
        away      = str(row.get("away_team", "")).strip()
        try:
            line  = float(row.get("point", 0) or row.get("line", 0) or 0)
            price = float(row.get("price", -110) or -110)
        except (ValueError, TypeError):
            continue
        book = str(row.get("sportsbook", "")).strip()

        if not direction or not player or not market:
            continue

        key = (player, market, direction, home, away)
        prev = best_line.get(key)
        def _is_better(new_line, new_price, prev_entry, dir_):
            """For Over: prefer lower line (easier to win), then better price.
               For Under: prefer higher line, then better price."""
            pl, pp = prev_entry["line"], float(str(prev_entry["price"]).replace("+",""))
            np_ = float(str(new_price).replace("+",""))
            if dir_ == "Over":
                if new_line < pl: return True
                if new_line == pl and np_ > pp: return True
            else:
                if new_line > pl: return True
                if new_line == pl and np_ > pp: return True
            return False
        if prev is None or _is_better(line, price, prev, direction):
            best_line[key] = {"line": line, "price": price, "book": book,
                               "home": home, "away": away}

    tt_rows     = []   # Team Totals — own tab
    prop_rows   = []   # Player Props (SP K, TB, HR, H+R+RBI)
    hr_bets_out = 0
    max_hr      = max(1, len(games) // HR_GAMES_RATIO)
    hrr_bets_out = 0
    tb_bets_out  = 0
    today       = datetime.now().strftime("%Y-%m-%d")

    for (player, market, direction, home, away), bl in best_line.items():
        line  = bl["line"]
        price = bl["price"]
        book  = bl["book"]
        game_label = f"{away} @ {home}"

        # Find game for park factor
        g = game_by_teams.get((home, away), {})
        park_factor = PARK_FACTORS.get(home, 100)
        if g:
            park_factor = g.get("park_factor", park_factor)

        # ── SP Strikeouts ─────────────────────────────────────────────────
        if market == "pitcher_strikeouts":
            sp = pitchers.get(home) if player.lower() in (pitchers.get(home, {}).get("name","").lower()) \
                 else pitchers.get(away)
            # Match pitcher name to team
            sp = None
            for team_sp in pitchers.values():
                if team_sp.get("name","").lower() == player.lower():
                    sp = team_sp
                    break
            if sp is None:
                continue
            # SP pitches against the other team — find which team they pitch for
            sp_team = sp.get("team", "")
            opp_team = away if sp_team and TEAM_ABBREV.get(home, home) == sp_team else home
            opp_ts = (sp_opp_stats or {}).get(opp_team) or (sp_opp_stats or {}).get(
                TEAM_ABBREV.get(opp_team, opp_team))
            proj = project_sp_strikeouts(sp, opp_ts)
            if proj is None:
                continue
            implied = american_to_implied(price)
            our_prob = prop_win_prob(proj, line, direction)
            edge_pct = (our_prob - implied) / implied * 100 if implied > 0 else 0
            if edge_pct < 4.0:
                continue
            units = unit_scale(edge_pct, PROPS_K_SCALE)
            stars = stars_from_units(units)
            prop_type = "SP Strikeouts"

        # ── Batter Total Bases ────────────────────────────────────────────
        elif market == "batter_total_bases":
            b = batter_stats.get(player.lower())
            if b is None:
                continue
            # 0.5 lines reflect lineup uncertainty (player might sit), not performance —
            # book prices them at -120 when there's real doubt. Skip to avoid that noise.
            if line is None or line < 1.0:
                continue
            pf = PARK_FACTORS.get(home, 100)
            proj = project_batter_tb(b, pf)
            if proj is None:
                continue
            # Adjust projection for opposing pitcher quality (ERA vs league average).
            # Calibration 2026-07-12: ignoring pitcher context was the primary source of
            # false Over edges on 1.5 lines; elite starters reduce expected TB by up to 20%.
            pitcher_adj = _pitcher_batter_adj(pitchers, b.get("team", ""), home, away)
            proj = proj * pitcher_adj
            implied = american_to_implied(price)
            our_prob = prop_win_prob(proj, line, direction)
            edge_pct = (our_prob - implied) / implied * 100 if implied > 0 else 0
            if edge_pct < 4.0:
                continue
            units = unit_scale(edge_pct, PROPS_TB_SCALE)
            stars = stars_from_units(units)
            prop_type = "Total Bases"

        # ── Batter Home Runs (shadow-only — market too efficient to bet) ────
        elif market == "batter_home_runs":
            b = batter_stats.get(player.lower())
            if b is None:
                continue
            pf = PARK_FACTORS.get(home, 100)
            proj = project_batter_hr(b, pf)
            if proj is None or proj < 0.06:
                continue  # skip sub-threshold power hitters
            implied = american_to_implied(price)
            our_prob = prop_win_prob(proj, line, direction)
            edge_pct = (our_prob - implied) / implied * 100 if implied > 0 else 0
            # Write to shadow regardless of edge for future calibration — never bet
            prop_rows.append([
                today, game_label, player, "Home Run", direction,
                book.title(), line, price, round(proj, 2),
                f"{round(edge_pct, 2)}%",
                "—", HR_MAX_UNITS,
                "Shadow Only",
                "", "Pending",
                "", run_now,
            ])
            hr_bets_out += 1
            continue

        # ── H + R + RBI ───────────────────────────────────────────────────
        elif market == "batter_hits_runs_rbis":
            b = batter_stats.get(player.lower())
            if b is None:
                continue
            # 0.5 lines reflect lineup uncertainty, not performance — skip.
            if line is None or line < 1.0:
                continue
            proj = project_batter_hrr(b)
            if proj is None:
                continue
            # Adjust projection for opposing pitcher quality (ERA vs league average).
            # Calibration 2026-07-12: HRR Over 1.5 was 39% win rate using raw season stats.
            # Facing an elite starter (ERA 2.50) reduces expected H+R+RBI by ~20%.
            pitcher_adj = _pitcher_batter_adj(pitchers, b.get("team", ""), home, away)
            proj = proj * pitcher_adj
            implied = american_to_implied(price)
            our_prob = prop_win_prob(proj, line, direction)
            edge_pct = (our_prob - implied) / implied * 100 if implied > 0 else 0
            if edge_pct < 4.0:
                continue
            units = unit_scale(edge_pct, PROPS_HRR_SCALE)
            stars = stars_from_units(units)
            prop_type = "H+R+RBI"

        # ── Team Totals ───────────────────────────────────────────────────
        elif market == "team_totals":
            gp = (game_projections or {}).get((home, away))
            if gp is None:
                continue
            # The Odds API puts team name in "player" field for team_totals
            if player == home:
                proj = gp["proj_home"]
            elif player == away:
                proj = gp["proj_away"]
            else:
                continue
            edge_pct = (proj - line) / line * 100 if line else 0
            if direction == "Under":
                edge_pct = -edge_pct
            if edge_pct < 4.0:
                continue
            units = unit_scale(edge_pct, TEAM_TOTAL_SCALE)
            stars = stars_from_units(units)
            prop_type = "Team Total"

        else:
            continue

        if stars < 4:
            continue

        implied = american_to_implied(price)
        hist = (historical_edges or {})
        tt_hist = hist.get("Team Total", []) if prop_type == "Team Total" else []
        if tt_hist:
            conf_pct = confidence_percentile(edge_pct, tt_hist)
            conf_label = "High" if stars == 5 else "Medium" if stars == 4 else "Standard"
        else:
            conf_label = "High" if stars == 5 else "Medium" if stars == 4 else "Standard"
            conf_pct = None
        conf_pct_str = f"{conf_pct}%" if conf_pct is not None else conf_label
        if prop_type == "Team Total":
            # Skip bets with juice worse than -199 — not worth the risk/reward
            if price is not None and price <= -200:
                continue
            # Team Total → own tab with dedicated header (no "Prop Type" column)
            tt_rows.append([
                today, game_label, player, direction,
                book.title(), line, price, round(proj, 2),
                round(edge_pct, 2), f"{round(edge_pct,2)}%",
                stars_emoji(stars), units,
                conf_label, conf_pct_str,
                "", "", "", "Pending", "",
                run_now,
            ])
        else:
            prop_rows.append([
                today, game_label, player, prop_type, direction,
                book.title(), line, price, round(proj, 2),
                f"{round(edge_pct,2)}%",
                stars_emoji(stars), units,
                conf_pct_str,
                "", "Pending",
                "", run_now,
            ])

        if market == "batter_home_runs":
            hr_bets_out += 1
        elif market == "batter_total_bases":
            tb_bets_out += 1
        elif market == "batter_hits_runs_rbis":
            hrr_bets_out += 1

    # Sort by units desc, then edge % desc
    sort_key = lambda r: (r[11], r[8])  # units col=11, edge col=8
    tt_rows.sort(key=sort_key, reverse=True)
    prop_rows.sort(key=lambda r: (r[11], float(str(r[9]).replace("%","")) if r[9] else 0), reverse=True)  # units col=11, edge% col=9
    return tt_rows, prop_rows


# ── Edge analysis ─────────────────────────────────────────────────────────────
def analyze(games, book_lines, pitchers, offense, run_now: str, special_games: dict = None,
            bullpen_eras: dict = None, ump_map: dict = None,
            sp_profiles: dict = None, batting_splits: dict = None,
            historical_edges: dict = None) -> tuple[list, list, list, list]:
    """
    Returns (edge_rows, bet_history_rows, shadow_rows, gt_shadow_rows).
    """
    edge_rows    = []
    history_rows = []
    shadow_rows  = []
    gt_shadow_rows  = []
    game_projections = {}  # {(home, away): {"proj_home": X, "proj_away": Y}}

    bullpen_eras   = bullpen_eras  or {}
    ump_map        = ump_map       or {}
    sp_profiles    = sp_profiles   or {}
    batting_splits = batting_splits or {}
    historical_edges = historical_edges or {"Game Total": [], "Moneyline": [], "Run Line": [], "Team Total": []}
    weather_cache  = {}   # {home_team: weather_dict} — one NWS call per ballpark per run

    # Track best game total per game for snapshot (Bet History — 4+ star only)
    best_total_per_game = {}

    # Track best game total per game+direction for GT Shadow (ALL star levels, best juice)
    best_gt_shadow_per_game_dir = {}

    # Track best ML/RL line per unique signal (game+type+side) for shadow snapshot
    best_shadow_per_signal = {}

    def juice_numeric(price):
        """Higher value = better odds for the bettor (works for both +/- lines)."""
        try:
            return float(str(price).replace("+", "").strip())
        except (ValueError, TypeError):
            return -9999

    for gid, game in games.items():
        home = game["home_team"]
        away = game["away_team"]
        ct   = game["commence_time"]
        time_et = ct.astimezone(timezone(timedelta(hours=-5))).strftime("%-I:%M %p ET") \
                  if sys.platform != "win32" else \
                  ct.astimezone(timezone(timedelta(hours=-5))).strftime("%#I:%M %p ET")
        game_label = f"{away} @ {home}"

        venue = game.get("venue", "")
        venue_lower = venue.lower()
        special_factor = next((f for vn, f in SPECIAL_VENUES.items() if vn in venue_lower), None)
        if special_factor:
            park_factor = special_factor
            print(f"  [Special Venue] {game_label} at {venue} — using park factor {special_factor}")
        else:
            park_factor = PARK_FACTORS.get(home, 100)

        # ── Umpire factor ──────────────────────────────────────────────────────
        ump_name   = ump_map.get((away, home), "")
        ump_factor = UMP_FACTORS.get(ump_name, 0.0)

        # ── Bullpen ERAs ───────────────────────────────────────────────────────
        home_bp_era = bullpen_eras.get(home, LEAGUE_AVG_BULLPEN_ERA)
        away_bp_era = bullpen_eras.get(away, LEAGUE_AVG_BULLPEN_ERA)

        # ── Weather (outdoor parks only, cached per ballpark per run) ──────────
        weather     = {}
        weather_adj = 0.0
        if home not in DOME_PARKS and home in BALLPARK_COORDS:
            if home not in weather_cache:
                lat, lon = BALLPARK_COORDS[home]
                weather_cache[home] = fetch_weather_factor(lat, lon, ct)
            weather = weather_cache[home]
            weather_adj = round(
                weather.get("wind_factor", 0.0) + weather.get("temp_factor", 0.0), 3
            )

        home_sp_data = pitchers.get(home, None)
        away_sp_data = pitchers.get(away, None)
        has_sp       = home_sp_data is not None and away_sp_data is not None

        home_era = home_sp_data["era_est"] if home_sp_data else LEAGUE_AVG_ERA
        away_era = away_sp_data["era_est"] if away_sp_data else LEAGUE_AVG_ERA
        home_sp  = home_sp_data["name"]    if home_sp_data else "Unknown"
        away_sp  = away_sp_data["name"]    if away_sp_data else "Unknown"
        home_xfip = home_sp_data.get("xfip") if home_sp_data else None
        away_xfip = away_sp_data.get("xfip") if away_sp_data else None

        home_off = offense.get(home, 0.0)
        away_off = offense.get(away, 0.0)

        # ── Blend SP ERA with recent form + apply rest day factor ──────────────
        home_prof = sp_profiles.get(home, {})
        away_prof = sp_profiles.get(away, {})
        home_era_final, home_rest_f = blend_sp_era(home_era, home_prof) if home_sp_data else (home_era, REST_DAY_DEFAULT)
        away_era_final, away_rest_f = blend_sp_era(away_era, away_prof) if away_sp_data else (away_era, REST_DAY_DEFAULT)

        # ── Platoon splits: offense vs SP handedness (SP innings only) ─────────
        home_sp_hand = home_prof.get("handedness", "R") if home_prof else "R"
        away_sp_hand = away_prof.get("handedness", "R") if away_prof else "R"
        away_split_key = "vs_lhp" if home_sp_hand == "L" else "vs_rhp"
        home_split_key = "vs_lhp" if away_sp_hand == "L" else "vs_rhp"
        away_off_vs_sp = batting_splits.get(away, {}).get(away_split_key)   # None = fallback to generic
        home_off_vs_sp = batting_splits.get(home, {}).get(home_split_key)

        proj = project_game(home, away, home_era_final, away_era_final,
                            home_off, away_off, park_factor,
                            home_bullpen_era=home_bp_era, away_bullpen_era=away_bp_era,
                            away_off_vs_sp=away_off_vs_sp, home_off_vs_sp=home_off_vs_sp)

        game_projections[(home, away)] = {
            "proj_home": proj["proj_home"],
            "proj_away": proj["proj_away"],
        }

        books = book_lines.get(gid, {})
        if not books:
            continue

        # Consensus total line (mean of available total lines across books)
        total_lines = [b.get("total_line") for b in books.values() if b.get("total_line")]
        consensus_line = sum(total_lines) / len(total_lines) if total_lines else None

        if consensus_line:
            proj_total = min(
                max(proj["proj_total_raw"], MIN_PROJ_TOTAL),
                consensus_line * PROJ_CAP_MULT,
            )
        else:
            proj_total = max(proj["proj_total_raw"], MIN_PROJ_TOTAL)
        # Apply umpire + weather contextual adjustments (additive to total projection)
        proj_total = round(max(proj_total + ump_factor + weather_adj, MIN_PROJ_TOTAL), 2)

        # ── Per-book analysis ─────────────────────────────────────────────────
        for book, bl in books.items():

            # ── Game Totals ────────────────────────────────────────────────
            t_line = bl.get("total_line")
            if t_line:
                edge = proj_total - t_line
                direction = "Over" if edge > 0 else "Under"
                abs_edge  = abs(edge)

                if abs_edge >= 0.75:
                    units = unit_scale(abs_edge, TOTAL_SCALE)
                    stars = stars_from_units(units)
                    juice = bl.get("total_over_price") if direction == "Over" else bl.get("total_under_price")
                    conf  = "High" if stars == 5 else "Medium" if stars == 4 else "Standard"

                    edge_pct_of_line = (abs_edge / t_line * 100) if t_line else 0.0
                    conf_pct = confidence_percentile(edge_pct_of_line, historical_edges["Game Total"])

                    edge_row = [
                        game_label, time_et, book.title(), "Game Total", direction,
                        f"{direction} {t_line}", stars_emoji(stars), stars, units,
                        t_line, juice, proj_total, round(edge, 2), "",
                        away_sp, round(away_era, 3), home_sp, round(home_era, 3),
                        proj["proj_away"], proj["proj_home"], park_factor,
                        f"{proj['home_win']*100:.1f}%", f"{proj['away_win']*100:.1f}%",
                        conf, f"{conf_pct}%", run_now,
                    ]
                    edge_rows.append(edge_row)

                    # Track best per game (for snapshot — only if SP known)
                    # Priority: (1) same direction + best line (lower for Over, higher for Under),
                    #           (2) same line → best juice, (3) different direction → larger edge
                    # Calibration 2026-07-12: Over bets 4★ win at 63%; Under bets 4★ win at 40%.
                    # Under bets lose because wrong park factors generated false edges at under-projected
                    # venues. Park factors updated 2026-07-12 using 1,428-game 2026 dataset.
                    # Under threshold raised to 20% (vs 15% for Over) to account for higher variance.
                    min_pct = 20.0 if direction == "Under" else 15.0
                    athletics_home = (home == "Athletics")
                    if has_sp and stars >= 4 and not athletics_home and edge_pct_of_line >= min_pct:
                        prev = best_total_per_game.get(gid)
                        if prev is None:
                            take_it = True
                        elif direction == prev["direction"]:
                            # Same direction — prefer the line that's easier to win
                            # Over: lower line is better; Under: higher line is better
                            if direction == "Over":
                                if t_line < prev["t_line"]:
                                    take_it = True   # lower Over line wins outright
                                elif t_line == prev["t_line"]:
                                    take_it = juice_numeric(juice) > juice_numeric(prev["juice"])
                                else:
                                    take_it = False  # higher Over line is worse
                            else:  # Under
                                if t_line > prev["t_line"]:
                                    take_it = True   # higher Under line wins outright
                                elif t_line == prev["t_line"]:
                                    take_it = juice_numeric(juice) > juice_numeric(prev["juice"])
                                else:
                                    take_it = False  # lower Under line is worse
                        else:
                            # Different direction — only switch if significantly better edge
                            take_it = abs_edge > prev["abs_edge"]
                        if take_it:
                            # Find DK juice for same line (empty if DK is best or unavailable)
                            dk_juice = ""
                            if book != "draftkings":
                                dk_bl = books.get("draftkings", {})
                                dk_over = dk_bl.get("over_juice") if direction == "Over" else None
                                dk_under = dk_bl.get("under_juice") if direction == "Under" else None
                                dk_j = dk_over if direction == "Over" else dk_under
                                dk_line = dk_bl.get("total_line")
                                if dk_j is not None and dk_line == t_line:
                                    dk_juice = dk_j
                            best_total_per_game[gid] = {
                                "abs_edge": abs_edge, "edge": edge,
                                "direction": direction, "t_line": t_line,
                                "juice": juice, "dk_juice": dk_juice,
                                "stars": stars, "units": units,
                                "book": book, "conf": conf, "game_label": game_label,
                                "time_et": time_et, "away_sp": away_sp, "home_sp": home_sp,
                                "away_era": away_era, "home_era": home_era,
                                "proj_total": proj_total, "proj": proj,
                                "park_factor": park_factor, "venue": venue,
                            }

                    # ── GT Shadow — track best line (then juice) per game+direction ──
                    gt_key = (gid, direction)
                    gt_prev = best_gt_shadow_per_game_dir.get(gt_key)
                    if gt_prev is None:
                        _gt_take = True
                    elif direction == "Over":
                        _gt_take = (t_line < gt_prev.get("t_line", t_line)) or \
                                   (t_line == gt_prev.get("t_line") and juice_numeric(juice) > juice_numeric(gt_prev["juice"]))
                    else:
                        _gt_take = (t_line > gt_prev.get("t_line", t_line)) or \
                                   (t_line == gt_prev.get("t_line") and juice_numeric(juice) > juice_numeric(gt_prev["juice"]))
                    if _gt_take:
                        best_gt_shadow_per_game_dir[gt_key] = {
                            "game_label": game_label, "time_et": time_et,
                            "away": away, "home": home,
                            "away_sp": away_sp, "home_sp": home_sp,
                            "away_era": away_era, "home_era": home_era,
                            "away_xfip": away_xfip, "home_xfip": home_xfip,
                            "away_off": away_off, "home_off": home_off,
                            "park_factor": park_factor, "venue": venue,
                            "has_sp": has_sp, "pitcher_flag": "" if has_sp else "Missing SP",
                            "direction": direction, "t_line": t_line,
                            "book": book, "juice": juice,
                            "proj_total": proj_total, "edge": edge, "abs_edge": abs_edge,
                            "stars": stars, "units": units, "conf": conf,
                            "proj_away": proj["proj_away"], "proj_home": proj["proj_home"],
                            "implied_pct": round(
                                (lambda j: (100 / (abs(j) + 100) * 100) if j < 0
                                 else (j / (j + 100) * 100))(float(str(juice).replace("+", "")))
                                if juice else 50.0, 2
                            ),
                            # New context factors
                            "away_bp_era": round(away_bp_era, 2),
                            "home_bp_era": round(home_bp_era, 2),
                            "ump_name":    ump_name or "Unknown",
                            "ump_factor":  ump_factor,
                            "wind_mph":    weather.get("wind_mph", ""),
                            "wind_dir":    weather.get("wind_dir", ""),
                            "temp_f":      weather.get("temp_f", ""),
                            "weather_adj": weather_adj,
                        }

            # ── Moneylines ─────────────────────────────────────────────────
            for side, our_pct, book_price_key in [
                ("Home", proj["home_win"], "h2h_home_price"),
                ("Away", proj["away_win"], "h2h_away_price"),
            ]:
                price = bl.get(book_price_key)
                if price is None:
                    continue
                implied = american_to_implied(price)
                edge_pct = (our_pct - implied) * 100

                if abs(edge_pct) >= 4.0 and edge_pct > 0:
                    units = unit_scale(edge_pct, ML_SCALE)
                    stars = stars_from_units(units)
                    conf  = "High" if stars == 5 else "Medium" if stars == 4 else "Standard"
                    conf_pct = confidence_percentile(edge_pct, historical_edges["Moneyline"])
                    bet_team = home if side == "Home" else away
                    raw_win = proj["home_win_raw"] if side == "Home" else (1 - proj["home_win_raw"])

                    edge_row = [
                        game_label, time_et, book.title(), "Moneyline", side,
                        bet_team, stars_emoji(stars), stars, units,
                        "", price, proj_total,
                        "", f"{round(edge_pct, 2)}%",
                        away_sp, round(away_era, 3), home_sp, round(home_era, 3),
                        proj["proj_away"], proj["proj_home"], park_factor,
                        f"{proj['home_win']*100:.1f}%", f"{proj['away_win']*100:.1f}%",
                        conf, f"{conf_pct}%", run_now,
                    ]
                    edge_rows.append(edge_row)

                    # ML RL Shadow — keep only best juice per game+type+side
                    sig_key = (gid, "Moneyline", side)
                    if sig_key not in best_shadow_per_signal or \
                       juice_numeric(price) > juice_numeric(best_shadow_per_signal[sig_key]["price"]):
                        dk_juice_ml = ""
                        if book != "draftkings":
                            dk_price_key = "h2h_home_price" if side == "Home" else "h2h_away_price"
                            dk_j = books.get("draftkings", {}).get(dk_price_key)
                            if dk_j is not None:
                                dk_juice_ml = dk_j
                        best_shadow_per_signal[sig_key] = dict(
                            game_label=game_label, time_et=time_et, away=away, home=home,
                            away_sp=away_sp, home_sp=home_sp, away_era=away_era, home_era=home_era,
                            away_xfip=away_xfip, home_xfip=home_xfip, away_off=away_off, home_off=home_off,
                            park_factor=park_factor, venue=venue, has_sp=has_sp,
                            bet_type="Moneyline", bet_team=bet_team, side=side,
                            our_pct=our_pct, raw_win=raw_win, proj=proj, proj_total=proj_total,
                            book=book, price=price, dk_juice=dk_juice_ml, implied=implied,
                            edge_pct=edge_pct, spread=None, stars=stars, units=units,
                        )

            # ── Run Lines ──────────────────────────────────────────────────
            for side, our_pct, spread_key, price_key in [
                ("Home", proj["home_rl_pct"], "rl_home_point", "rl_home_price"),
                ("Away", proj["away_rl_pct"], "rl_away_point", "rl_away_price"),
            ]:
                price  = bl.get(price_key)
                spread = bl.get(spread_key)
                if price is None or spread is None:
                    continue
                implied  = american_to_implied(price)
                edge_pct = (our_pct - implied) * 100

                if edge_pct >= 4.0:
                    units = unit_scale(edge_pct, RL_SCALE)
                    stars = stars_from_units(units)
                    conf  = "High" if stars == 5 else "Medium" if stars == 4 else "Standard"
                    conf_pct = confidence_percentile(edge_pct, historical_edges["Run Line"])
                    bet_team = home if side == "Home" else away
                    raw_win  = proj["home_win_raw"] if side == "Home" else (1 - proj["home_win_raw"])

                    edge_row = [
                        game_label, time_et, book.title(), "Run Line", side,
                        f"{bet_team} {spread:+.1f}", stars_emoji(stars), stars, units,
                        spread, price, proj_total,
                        "", f"{round(edge_pct, 2)}%",
                        away_sp, round(away_era, 3), home_sp, round(home_era, 3),
                        proj["proj_away"], proj["proj_home"], park_factor,
                        f"{proj['home_win']*100:.1f}%", f"{proj['away_win']*100:.1f}%",
                        conf, f"{conf_pct}%", run_now,
                    ]
                    edge_rows.append(edge_row)

                    # ML RL Shadow — keep only best juice per game+type+side
                    sig_key = (gid, "Run Line", side)
                    if sig_key not in best_shadow_per_signal or \
                       juice_numeric(price) > juice_numeric(best_shadow_per_signal[sig_key]["price"]):
                        dk_juice_rl = ""
                        if book != "draftkings":
                            dk_price_key = "rl_home_price" if side == "Home" else "rl_away_price"
                            dk_spread_key = "rl_home_point" if side == "Home" else "rl_away_point"
                            dk_bl = books.get("draftkings", {})
                            dk_j = dk_bl.get(dk_price_key)
                            dk_sp = dk_bl.get(dk_spread_key)
                            if dk_j is not None and dk_sp == spread:
                                dk_juice_rl = dk_j
                        best_shadow_per_signal[sig_key] = dict(
                            game_label=game_label, time_et=time_et, away=away, home=home,
                            away_sp=away_sp, home_sp=home_sp, away_era=away_era, home_era=home_era,
                            away_xfip=away_xfip, home_xfip=home_xfip, away_off=away_off, home_off=home_off,
                            park_factor=park_factor, venue=venue, has_sp=has_sp,
                            bet_type="Run Line", bet_team=bet_team, side=side,
                            our_pct=our_pct, raw_win=raw_win, proj=proj, proj_total=proj_total,
                            book=book, price=price, dk_juice=dk_juice_rl, implied=implied,
                            edge_pct=edge_pct, spread=spread, stars=stars, units=units,
                        )

    # ── Build Bet History snapshot rows ──────────────────────────────────────
    today = datetime.now().strftime("%Y-%m-%d")
    for gid, best in best_total_per_game.items():
        p = best["proj"]
        edge_pct_of_line = (best["abs_edge"] / best["t_line"] * 100) if best["t_line"] else 0.0
        conf_pct = confidence_percentile(edge_pct_of_line, historical_edges["Game Total"])
        history_rows.append([
            today, best["game_label"], best["time_et"],
            best["away_sp"], best["home_sp"],
            "Game Total", best["direction"], stars_emoji(best["stars"]), best["units"],
            best["book"].title(), best["t_line"], best["juice"], best.get("dk_juice", ""),
            best["proj_total"], round(best["edge"], 2),
            "", "", "", "Pending", "",
            best["park_factor"], best["venue"], best["conf"], f"{conf_pct}%",
            f"{best['direction']} {best['t_line']}", "",
        ])

    # ── Build ML/RL Bet History rows (4+ star, has_sp, not Athletics home) ─────
    for s in best_shadow_per_signal.values():
        if not s["has_sp"] or s["home"] == "Athletics" or s["stars"] < 4:
            continue
        conf_pct = confidence_percentile(s["edge_pct"], historical_edges[s["bet_type"]])
        conf = "High" if s["stars"] == 5 else "Medium" if s["stars"] == 4 else "Standard"
        if s["bet_type"] == "Moneyline":
            history_rows.append([
                today, s["game_label"], s["time_et"],
                s["away_sp"], s["home_sp"],
                "Moneyline", s["side"], stars_emoji(s["stars"]), s["units"],
                s["book"].title(), "", s["price"], s.get("dk_juice", ""),
                f"{s['our_pct']*100:.1f}%", "",
                "", "", "", "Pending", "",
                s["park_factor"], s["venue"], conf, f"{conf_pct}%",
                s["bet_team"], f"{round(s['edge_pct'], 2)}%",
            ])
        else:  # Run Line
            spread_str = f"{s['spread']:+.1f}" if s["spread"] is not None else ""
            history_rows.append([
                today, s["game_label"], s["time_et"],
                s["away_sp"], s["home_sp"],
                "Run Line", s["side"], stars_emoji(s["stars"]), s["units"],
                s["book"].title(), s["spread"] if s["spread"] is not None else "", s["price"],
                s.get("dk_juice", ""),
                f"{s['our_pct']*100:.1f}%", "",
                "", "", "", "Pending", "",
                s["park_factor"], s["venue"], conf, f"{conf_pct}%",
                f"{s['bet_team']} {spread_str}".strip(), f"{round(s['edge_pct'], 2)}%",
            ])

    # ── Build ML/RL shadow rows from best-line-per-signal dict ──────────────
    for s in best_shadow_per_signal.values():
        conf_pct = confidence_percentile(s["edge_pct"], historical_edges[s["bet_type"]])
        shadow_rows.append(_shadow_row(
            s["game_label"], s["time_et"], s["away"], s["home"],
            s["away_sp"], s["home_sp"], s["away_era"], s["home_era"],
            s["away_xfip"], s["home_xfip"], s["away_off"], s["home_off"],
            s["park_factor"], s["venue"], s["has_sp"], s["bet_type"], s["bet_team"], s["side"],
            s["our_pct"], s["raw_win"], s["proj"], s["proj_total"],
            s["book"], s["price"], s["implied"],
            s["implied"], s["edge_pct"], s["edge_pct"], s["spread"], s["stars"], s["units"],
            conf_pct, run_now,
        ))

    # ── Build Game Total Shadow rows (ALL star levels, best juice per game+dir) ─
    today = datetime.now().strftime("%Y-%m-%d")
    for g in best_gt_shadow_per_game_dir.values():
        def r2(v):
            try: return round(float(v), 2)
            except (TypeError, ValueError): return v
        edge_pct_of_line = round(g["abs_edge"] / g["t_line"] * 100, 2) if g["t_line"] else ""
        conf_pct = confidence_percentile(float(edge_pct_of_line) if edge_pct_of_line != "" else 0.0,
                                          historical_edges["Game Total"])
        gt_shadow_rows.append([
            today, g["game_label"], g["time_et"], g["away"], g["home"],
            g["away_sp"], g["home_sp"],
            r2(g["away_era"]), r2(g["home_era"]),
            r2(g["away_xfip"]) if g["away_xfip"] else "",
            r2(g["home_xfip"]) if g["home_xfip"] else "",
            r2(g["away_off"]), r2(g["home_off"]),
            g["park_factor"], g["venue"], g["pitcher_flag"],
            g["direction"], g["t_line"], g["book"].title(), g["juice"],
            g["proj_total"], round(g["edge"], 2), f"{edge_pct_of_line}%",
            stars_emoji(g["stars"]), g["units"],
            r2(g["proj_away"]), r2(g["proj_home"]),
            f"{g['implied_pct']}%", g["conf"], f"{conf_pct}%",
            # Context factors (bullpen, umpire, weather)
            g.get("away_bp_era", ""), g.get("home_bp_era", ""),
            g.get("ump_name", ""), g.get("ump_factor", ""),
            g.get("wind_mph", ""), g.get("wind_dir", ""),
            g.get("temp_f", ""), g.get("weather_adj", ""),
            # Result columns — filled by grade_bets.py
            "", "", "", "", "",
            run_now,
        ])

    return edge_rows, history_rows, shadow_rows, gt_shadow_rows, game_projections


def _shadow_row(
    game_label, time_et, away, home,
    away_sp, home_sp, away_era, home_era,
    away_xfip, home_xfip, away_off, home_off,
    park_factor, venue, has_sp, bet_type, bet_team, side,
    our_pct, raw_win, proj, proj_total,
    book, price, implied, consensus_implied, edge_pct, edge_vs_consensus,
    spread, stars, units, conf_pct, run_now,
):
    today = datetime.now().strftime("%Y-%m-%d")
    pitcher_flag = "" if has_sp else "Missing SP"
    bucket = (
        "7%+" if edge_pct >= 7 else
        "5.5-6.9%" if edge_pct >= 5.5 else
        "4-5.4%" if edge_pct >= 4 else "<4%"
    )
    fav = home if proj["proj_home"] > proj["proj_away"] else away
    def r2(v):
        try:
            return round(float(v), 2)
        except (TypeError, ValueError):
            return v

    def pct(v):
        """Format as plain percent string e.g. '67.55%' — immune to cell formatting."""
        try:
            return f"{round(float(v), 2)}%"
        except (TypeError, ValueError):
            return ""

    return [
        today, game_label, time_et, away, home,
        away_sp, home_sp, r2(away_era), r2(home_era),
        r2(away_xfip) if away_xfip else "", r2(home_xfip) if home_xfip else "",
        r2(away_off), r2(home_off),
        park_factor, venue, pitcher_flag, bet_type, bet_team, side,
        pct(our_pct * 100), pct(raw_win * 100),
        r2(proj["proj_away"]), r2(proj["proj_home"]), r2(proj_total),
        r2(proj["proj_run_diff"]), book.title(), price,
        pct(implied * 100), pct(consensus_implied * 100),
        pct(edge_pct), pct(edge_vs_consensus),
        spread or "", stars_emoji(stars), units, bucket, fav,
        # Result columns (filled by grade_bets.py)
        "", "", "", "", "", "", "", "", "", "", "",
        f"{conf_pct}%",
        run_now,
    ]


# ── Sheet writers ─────────────────────────────────────────────────────────────
EDGES_HEADER = [
    "Game", "Time (ET)", "Book", "Bet Type", "Direction", "Bet On",
    "Stars", "Stars (#)", "Units", "Book Line", "Book Juice", "Our Projection",
    "Edge (runs)", "Edge %", "Away SP", "Away ERA Est",
    "Home SP", "Home ERA Est", "Proj Away Runs", "Proj Home Runs",
    "Park Factor", "Home Win%", "Away Win%", "Confidence", "Confidence %", "Run at",
]

HISTORY_HEADER = [
    "Date", "Game", "Time (ET)", "Away SP", "Home SP",
    "Bet Type", "Direction", "Stars", "Units Bet",
    "Book", "Book Line", "Book Juice", "DK Juice", "Our Projection",
    "Edge (runs)", "Away Score", "Home Score", "Actual Total",
    "Result", "Units Result", "Park Factor", "Venue", "Confidence", "Confidence %", "Bet On", "Edge %",
]

SHADOW_HEADER = [
    "Date", "Game", "Time (ET)", "Away Team", "Home Team",
    "Away SP", "Home SP", "Away ERA Est", "Home ERA Est",
    "Away xFIP", "Home xFIP", "Away Offense Adj", "Home Offense Adj",
    "Park Factor", "Venue", "Pitcher Flag", "Bet Type", "Bet Team", "Bet Side",
    "Our Win%", "Raw Our Win%", "Proj Away Runs", "Proj Home Runs",
    "Proj Total", "Proj Run Diff", "Book", "Book Juice",
    "Book Implied%", "Consensus Implied%", "Edge vs Book%", "Edge vs Consensus%",
    "RL Spread", "Stars", "Units Would Bet", "Edge Bucket", "Market Favorite",
    "Away Score", "Home Score", "Actual Winner", "Actual Run Diff",
    "Did Favorite Win", "Bet Result", "Units Result", "Actual Outcome",
    "Prediction Error", "Was Overconfident", "Confidence", "Confidence %", "Run at",
]


GT_SHADOW_HEADER = [
    "Date", "Game", "Time (ET)", "Away Team", "Home Team",
    "Away SP", "Home SP", "Away ERA Est", "Home ERA Est",
    "Away xFIP", "Home xFIP", "Away Offense Adj", "Home Offense Adj",
    "Park Factor", "Venue", "Pitcher Flag",
    "Direction", "Book Line", "Book", "Book Juice",
    "Our Projection", "Edge (runs)", "Edge % of Line",
    "Stars", "Units Would Bet",
    "Proj Away Runs", "Proj Home Runs",
    "Book Implied%", "Confidence", "Confidence %",
    # Context factors (new — bullpen, umpire, weather)
    "Away Bullpen ERA", "Home Bullpen ERA",
    "Umpire", "Ump Factor",
    "Wind MPH", "Wind Dir", "Temp (F)", "Weather Adj",
    # Result columns (filled by grade_bets.py)
    "Away Score", "Home Score", "Actual Total",
    "Result", "Units Result",
    "Run at",
]


TEAM_TOTAL_HEADER = [
    "Date", "Game", "Team", "Direction",
    "Best Book", "Book Line", "Book Juice", "Our Projection",
    "Edge", "Edge %", "Stars", "Units",
    "Confidence", "Confidence %",
    "Away Score", "Home Score", "Actual Team Total",
    "Result", "Units Result", "Run at",
]

PLAYER_PROPS_HEADER = [
    "Date", "Game", "Player", "Prop Type", "Direction",
    "Best Book", "Book Line", "Book Juice", "Our Projection",
    "Edge %", "Stars", "Units",
    "Confidence %",
    "Actual Stat", "Result",
    "Units Result", "Run at",
]


def today_already_in_gt_shadow(ws_gt_shadow) -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        col = ws_gt_shadow.col_values(1)
        return today in col[1:]
    except Exception:
        return False


def today_already_in_history(ws_history) -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        col = ws_history.col_values(1)  # Date column
        return today in col[1:]         # skip header
    except Exception:
        return False


def today_already_in_shadow(ws_shadow) -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        col = ws_shadow.col_values(1)
        return today in col[1:]
    except Exception:
        return False


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Fantasy Six Pack Edge Analyzer")
    parser.add_argument("--force", action="store_true",
                        help="Bypass first-run-of-day protection and re-snapshot all tabs")
    args = parser.parse_args()
    force = args.force

    run_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("=" * 60)
    print("analyze_edges.py — Fantasy Six Pack Edge Analyzer")
    print(f"Run time: {run_now}")
    if force:
        print("  *** FORCE MODE — overwriting today's snapshots ***")
    print("=" * 60)

    print("\nConnecting to Google Sheets ...")
    gc = auth()

    print("Loading odds ...")
    odds_rows = load_odds(gc)
    print(f"  {len(odds_rows)} odds rows loaded")

    print("Loading pitcher data ...")
    pitchers = load_pitchers(gc)
    print(f"  {len(pitchers)} teams with pitcher data")

    print("Loading batter stats ...")
    batter_stats = load_batter_stats(gc)
    print(f"  {len(batter_stats)} batters loaded")

    print("Loading SP opponent stats ...")
    sp_opp_stats = load_sp_opp_stats(gc)
    print(f"  {len(sp_opp_stats)} teams with SP opp stats")

    print("Loading player props odds ...")
    prop_odds = load_prop_odds(gc)
    print(f"  {len(prop_odds)} prop odds rows loaded")

    print("Loading hitter/offense data ...")
    season_offense = load_offense(gc)
    print(f"  {len(season_offense)} teams with season offense data")

    print("Blending rolling offense (last 21 days, 60/40) ...")
    offense = load_rolling_offense(season_offense)
    n_rolling = sum(1 for t in offense if t in season_offense and offense[t] != season_offense.get(t, 0))
    print(f"  Rolling blend applied ({len(offense)} teams)")

    print("Loading bullpen data ...")
    bullpen_eras = load_bullpen()
    print(f"  {len(bullpen_eras)} teams with bullpen ERA data")

    print("Loading SP profiles (recent form, rest days, handedness) ...")
    sp_profiles = load_pitcher_profiles(pitchers)
    print(f"  {len(sp_profiles)} SPs profiled")

    print("Loading team batting splits (vs LHP / RHP) ...")
    batting_splits = load_team_batting_splits()
    print(f"  {len(batting_splits)} teams with platoon split data")

    print("Fetching venue + umpire data ...")
    today_str  = datetime.now().strftime("%Y-%m-%d")
    venue_map, ump_map = fetch_venue_and_ump_map(today_str)
    print(f"  {len(venue_map)} games with venue data, {len(ump_map)} with umpire data")
    special_games = {k: next((f for vn, f in SPECIAL_VENUES.items() if vn in v.lower()), None)
                     for k, v in venue_map.items()
                     if any(vn in v.lower() for vn in SPECIAL_VENUES)}
    if special_games:
        for teams, factor in special_games.items():
            print(f"  Special venue: {teams[0]} @ {teams[1]} — park factor {factor}")
    else:
        print("  No special venue games today")

    print("Building game universe ...")
    games      = build_game_universe(odds_rows)
    attach_venues(games, venue_map)
    book_lines = build_book_lines(odds_rows, games)
    print(f"  {len(games)} upcoming games found")

    print("\nRunning edge analysis ...")
    historical_edges = load_historical_edges(gc)
    edge_rows, history_rows, shadow_rows, gt_shadow_rows, game_projections = analyze(
        games, book_lines, pitchers, offense, run_now, special_games,
        bullpen_eras=bullpen_eras, ump_map=ump_map,
        sp_profiles=sp_profiles, batting_splits=batting_splits,
        historical_edges=historical_edges,
    )
    print(f"  {len(edge_rows)} edges found")
    print(f"  {len(history_rows)} game total bets to snapshot")
    print(f"  {len(shadow_rows)} ML/RL shadow rows")
    print(f"  {len(gt_shadow_rows)} game total shadow rows")

    # ── Sort edges by units high to low ──────────────────────────────────────
    units_col = EDGES_HEADER.index("Units")
    edge_rows.sort(key=lambda r: r[units_col], reverse=True)

    # ── Write Edges tab (today only — cleared and rewritten each run) ─────────
    ws_edges = ws(gc, ODDS_SHEET_ID, "Edges")
    ws_edges.clear()
    ws_edges.format("A1:Z2000", {
        "numberFormat": {"type": "NUMBER", "pattern": "0.##"},
        "borders": {
            "top":    {"style": "NONE"},
            "bottom": {"style": "NONE"},
            "left":   {"style": "NONE"},
            "right":  {"style": "NONE"},
        }
    })
    ws_edges.update([EDGES_HEADER] + edge_rows, value_input_option="USER_ENTERED")
    print(f"\nWrote {len(edge_rows)} rows to 'Edges' tab")

    # ── Snapshot to Bet History (first run of day only — newest date at top) ───
    today = datetime.now().strftime("%Y-%m-%d")
    ws_hist = ws(gc, ODDS_SHEET_ID, "Bet History")
    if history_rows:
        already_in_hist = today_already_in_history(ws_hist)
        if already_in_hist and not force:
            print("Bet History: today already exists — skipping snapshot (first-run protection)")
        else:
            existing = ws_hist.get_all_values()
            if already_in_hist and force:
                # Delete today's rows first so we re-insert fresh. These are
                # always contiguous (today's snapshot always lands right
                # after the header), so one ranged delete covers them —
                # deleting one row at a time here can blow through Google
                # Sheets API's write-requests-per-minute quota on tabs with
                # many rows to delete.
                rows_to_delete = [i+1 for i, r in enumerate(existing)
                                  if i > 0 and r and r[0] == today]
                if rows_to_delete:
                    ws_hist.delete_rows(min(rows_to_delete), max(rows_to_delete))
                existing = ws_hist.get_all_values()
                print(f"  Force: deleted {len(rows_to_delete)} existing Bet History row(s) for today")
            has_hist_header = existing and existing[0] and existing[0][0] == "Date"
            if not has_hist_header:
                ws_hist.update([HISTORY_HEADER] + history_rows, value_input_option="USER_ENTERED")
            else:
                h_units_col   = HISTORY_HEADER.index("Units Bet")
                h_confpct_col = HISTORY_HEADER.index("Confidence %")
                def _hist_sort_key(r):
                    u = float(r[h_units_col]) if r[h_units_col] else 0.0
                    try: c = float(str(r[h_confpct_col]).replace("%", ""))
                    except: c = 0.0
                    return (u, c)
                history_rows.sort(key=_hist_sort_key, reverse=True)
                ws_hist.insert_rows(history_rows, row=2, value_input_option="USER_ENTERED")
            print(f"Inserted {len(history_rows)} bets at top of 'Bet History'")
    else:
        print("No qualifying bets to snapshot")

    # ── Snapshot to Game Total Shadow (first run of day only) ────────────────
    ws_gt_shadow = ws(gc, ODDS_SHEET_ID, "Game Totals")
    if gt_shadow_rows:
        already_in_gt = today_already_in_gt_shadow(ws_gt_shadow)
        if already_in_gt and not force:
            print("Game Total Shadow: today already exists — skipping (first-run protection)")
        else:
            existing = ws_gt_shadow.get_all_values()
            if already_in_gt and force:
                rows_to_delete = [i+1 for i, r in enumerate(existing)
                                  if i > 0 and r and r[0] == today]
                if rows_to_delete:
                    ws_gt_shadow.delete_rows(min(rows_to_delete), max(rows_to_delete))
                existing = ws_gt_shadow.get_all_values()
                print(f"  Force: deleted {len(rows_to_delete)} existing GT Shadow row(s) for today")
            has_header = existing and existing[0] and existing[0][0] == "Date"
            if not has_header:
                ws_gt_shadow.update([GT_SHADOW_HEADER] + gt_shadow_rows, value_input_option="RAW")
            else:
                ws_gt_shadow.insert_rows(gt_shadow_rows, row=2, value_input_option="RAW")
            ws_gt_shadow.format("A1:AJ2000", {"numberFormat": {"type": "TEXT"}})
            print(f"Inserted {len(gt_shadow_rows)} rows at top of 'Game Total Shadow'")
    else:
        print("No game total shadow rows to snapshot")

    # ── Snapshot to ML RL Shadow (first run of day only — newest date at top) ──
    ws_shadow = ws(gc, ODDS_SHEET_ID, "ML RL")
    if shadow_rows:
        already_in_shadow = today_already_in_shadow(ws_shadow)
        if already_in_shadow and not force:
            print("ML RL Shadow: today already exists — skipping (first-run protection)")
        else:
            existing = ws_shadow.get_all_values()
            if already_in_shadow and force:
                rows_to_delete = [i+1 for i, r in enumerate(existing)
                                  if i > 0 and r and r[0] == today]
                if rows_to_delete:
                    ws_shadow.delete_rows(min(rows_to_delete), max(rows_to_delete))
                existing = ws_shadow.get_all_values()
                print(f"  Force: deleted {len(rows_to_delete)} existing ML RL Shadow row(s) for today")
            s_units_col = SHADOW_HEADER.index("Units Would Bet")
            s_edge_col = SHADOW_HEADER.index("Edge vs Book%")
            def _edge_pct_val(v):
                try: return float(str(v).replace("%", ""))
                except (ValueError, TypeError): return 0.0
            shadow_rows.sort(key=lambda r: (float(r[s_units_col]), _edge_pct_val(r[s_edge_col])), reverse=True)
            if not existing:
                ws_shadow.update([SHADOW_HEADER] + shadow_rows, value_input_option="USER_ENTERED")
            else:
                ws_shadow.insert_rows(shadow_rows, row=2, value_input_option="USER_ENTERED")
            ws_shadow.format("A1:AV2000", {"numberFormat": {"type": "TEXT"}})
            print(f"Inserted {len(shadow_rows)} rows at top of 'ML RL Shadow'")
    else:
        print("No ML/RL shadow rows to snapshot")

    # ── Player Props analysis and snapshot ───────────────────────────────────
    print("\nRunning player props analysis ...")
    tt_rows, prop_shadow_rows = analyze_props(prop_odds, pitchers, batter_stats, games, run_now,
                                              sp_opp_stats=sp_opp_stats,
                                              game_projections=game_projections,
                                              historical_edges=historical_edges)
    print(f"  {len(tt_rows)} team total(s) + {len(prop_shadow_rows)} player prop(s) found")

    ws_tt = ws(gc, ODDS_SHEET_ID, "Team Totals")
    if tt_rows:
        existing_tt = ws_tt.get_all_values()
        has_tt_header = existing_tt and existing_tt[0] and existing_tt[0][0] == "Date"
        today_in_tt = any(r and r[0] == today for r in existing_tt[1:]) if existing_tt else False
        if today_in_tt and not force:
            print("Team Totals: today already exists — skipping (first-run protection)")
        else:
            if today_in_tt and force:
                rows_to_del = [i+1 for i, r in enumerate(existing_tt) if i > 0 and r and r[0] == today]
                if rows_to_del:
                    ws_tt.delete_rows(min(rows_to_del), max(rows_to_del))
                existing_tt = ws_tt.get_all_values()
                print(f"  Force: deleted {len(rows_to_del)} existing Team Totals row(s) for today")
            if not has_tt_header or not existing_tt:
                ws_tt.update([TEAM_TOTAL_HEADER] + tt_rows, value_input_option="USER_ENTERED")
            else:
                ws_tt.insert_rows(tt_rows, row=2, value_input_option="USER_ENTERED")
            print(f"  Wrote {len(tt_rows)} rows to 'Team Totals' tab")
    else:
        existing_tt = ws_tt.get_all_values()
        if not existing_tt or not existing_tt[0]:
            ws_tt.update([TEAM_TOTAL_HEADER], value_input_option="USER_ENTERED")
        if not prop_odds:
            print("  No prop odds in MLB Odds — run fetch_odds.py first")

    if prop_shadow_rows:
        ws_props = ws(gc, ODDS_SHEET_ID, "Player Props Shadow")
        existing_props = ws_props.get_all_values()
        correct_header = existing_props and existing_props[0] == PLAYER_PROPS_HEADER
        today_in_props = any(r and r[0] == today_str for r in existing_props[1:]) if existing_props else False
        if today_in_props and not force:
            print("  Player Props Shadow: today already exists — skipping (first-run protection)")
        else:
            if today_in_props and force:
                rows_to_del = [i+1 for i, r in enumerate(existing_props) if i > 0 and r and r[0] == today_str]
                if rows_to_del:
                    ws_props.delete_rows(min(rows_to_del), max(rows_to_del))
                existing_props = ws_props.get_all_values()
                print(f"  Force: deleted {len(rows_to_del)} existing Player Props Shadow row(s) for today")
            if not correct_header or not existing_props:
                # Full rewrite: clears stale/old headers and rewrites everything
                ws_props.clear()
                ws_props.update([PLAYER_PROPS_HEADER] + prop_shadow_rows, value_input_option="USER_ENTERED")
            else:
                ws_props.insert_rows(prop_shadow_rows, row=2, value_input_option="USER_ENTERED")
            print(f"  Wrote {len(prop_shadow_rows)} rows to 'Player Props Shadow' tab")

    # ── Log SP profiles to SP History tab (item 7 — historical SP database) ──
    print("\nLogging SP History ...")
    blended_eras = {}
    for team, sp in pitchers.items():
        prof = sp_profiles.get(team, {})
        era, _ = blend_sp_era(sp["era_est"], prof) if prof else (sp["era_est"], REST_DAY_DEFAULT)
        blended_eras[team] = era
    store_sp_history(gc, pitchers, sp_profiles, blended_eras, today_str)

    # ── Daily bet summary printout ────────────────────────────────────────────
    # Sort best to worst (units desc, then confidence % desc) for display
    _du = HISTORY_HEADER.index("Units Bet")
    _dc = HISTORY_HEADER.index("Confidence %")
    def _disp_sort(r):
        u = float(r[_du]) if r[_du] else 0.0
        try: c = float(str(r[_dc]).replace("%", ""))
        except: c = 0.0
        return (u, c)
    history_rows.sort(key=_disp_sort, reverse=True)

    def _safe(row, i, default=""):
        return row[i] if 0 <= i < len(row) else default

    def star_word(stars_str):
        n = stars_str.count("⭐")
        return f"{n} star" if n else "4 star"

    def fmt_juice(j):
        try: return f"{int(float(j)):+d}"
        except: return str(j)

    ci = {h: i for i, h in enumerate(HISTORY_HEADER)}
    pi = {h: i for i, h in enumerate(PLAYER_PROPS_HEADER)}
    ti = {h: i for i, h in enumerate(TEAM_TOTAL_HEADER)}

    # ── TABLE: Official Bets ──────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print(f"  FANTASY SIX PACK — TODAY'S BETS  ({today})")
    print("=" * 70)
    print("\n-- OFFICIAL BETS (Game Totals / ML / RL) --")
    print(f"  {'Stars':<6} {'Game':<40} {'Time':<12} {'Bet':<28} {'Juice':<12} {'Projection':<14} {'Conf':<8} {'Units':<7} Book")
    print("  " + "-" * 140)
    for r in history_rows:
        stars  = _safe(r, ci.get("Stars", -1))
        game   = _safe(r, ci.get("Game", -1))
        time_  = _safe(r, ci.get("Time (ET)", -1))
        btype  = _safe(r, ci.get("Bet Type", -1))
        direc  = _safe(r, ci.get("Direction", -1))
        beton  = _safe(r, ci.get("Bet On", -1))
        line   = _safe(r, ci.get("Book Line", -1))
        juice  = _safe(r, ci.get("Book Juice", -1))
        dk_j   = _safe(r, ci.get("DK Juice", -1))
        units  = _safe(r, ci.get("Units Bet", -1))
        conf   = _safe(r, ci.get("Confidence %", -1))
        book   = _safe(r, ci.get("Book", -1))
        proj   = _safe(r, ci.get("Our Projection", -1))

        dk_str = f"  DK:{fmt_juice(dk_j)}" if dk_j else ""
        juice_col = f"{fmt_juice(juice)}{dk_str}"

        if btype == "Game Total":
            bet_col  = f"GT {direc} {line}"
            proj_col = f"{proj} runs"
        elif btype == "Moneyline":
            bet_col  = f"ML — {beton}"
            proj_col = f"Win {proj}"
        else:
            bet_col  = f"RL — {beton}"
            proj_col = f"Win {proj}"

        print(f"  {stars:<6} {game:<40} {time_:<12} {bet_col:<28} {juice_col:<16} {proj_col:<14} {conf:<8} {units}u  {book}")

    # ── TABLE: Team Totals ────────────────────────────────────────────────────
    if tt_rows:
        print("\n-- TEAM TOTALS --")
        print(f"  {'Stars':<6} {'Game':<40} {'Team':<25} {'Dir':<6} {'Line':<6} {'Juice':<8} {'Proj':<6} {'Conf':<8} {'Units':<7} Book")
        print("  " + "-" * 130)
        for r in tt_rows:
            stars  = _safe(r, ti.get("Stars", -1))
            game   = _safe(r, ti.get("Game", -1))
            team   = _safe(r, ti.get("Team", -1))
            direc  = _safe(r, ti.get("Direction", -1))
            line   = _safe(r, ti.get("Book Line", -1))
            juice  = _safe(r, ti.get("Book Juice", -1))
            proj   = _safe(r, ti.get("Our Projection", -1))
            units  = _safe(r, ti.get("Units", -1))
            conf   = _safe(r, ti.get("Confidence %", -1))
            book   = _safe(r, ti.get("Best Book", -1))
            print(f"  {stars:<6} {game:<40} {team:<25} {direc:<6} {line:<6} {fmt_juice(juice):<8} {proj:<6} {conf:<8} {units}u  {book}")

    # ── COPY/PASTE: Official Bets Only ────────────────────────────────────────
    print("\n-- COPY/PASTE (Official Bets) --")
    for r in history_rows:
        btype  = _safe(r, ci.get("Bet Type", -1))
        direc  = _safe(r, ci.get("Direction", -1))
        beton  = _safe(r, ci.get("Bet On", -1))
        line   = _safe(r, ci.get("Book Line", -1))
        juice  = _safe(r, ci.get("Book Juice", -1))
        units  = _safe(r, ci.get("Units Bet", -1))
        stars  = _safe(r, ci.get("Stars", -1))
        proj   = _safe(r, ci.get("Our Projection", -1))
        away, home = (_safe(r, ci.get("Game", -1)).split(" @ ") + ["", ""])[:2]

        try: units_fmt = str(float(units))
        except: units_fmt = str(units)

        if btype == "Game Total":
            label = f"{away} @ {home} {'o' if direc == 'Over' else 'u'}{line}"
            try: proj_runs = round(float(str(proj).replace("%","")) / 100.0, 2) if "%" in str(proj) else round(float(proj), 2)
            except: proj_runs = proj
            desc = f"My betting model shows this as a {star_word(stars)} bet as I have it projected for {proj_runs} runs"
        elif btype == "Moneyline":
            label = f"{beton} ML"
            try: win_pct = str(proj).replace("%","").strip() + "%"
            except: win_pct = proj
            desc = f"My betting model shows this as a {star_word(stars)} bet as I have them projected to win {win_pct} of the time"
        else:
            label = f"{beton}"
            try: win_pct = str(proj).replace("%","").strip() + "%"
            except: win_pct = proj
            desc = f"My betting model shows this as a {star_word(stars)} bet as I have them projected to win by 2+ runs {win_pct} of the time"

        print(f"{label}\t{units_fmt}\t{fmt_juice(juice)}\t{desc}")

    print("\n" + "=" * 70)
    print(f"  {len(history_rows)} official bet(s)  |  {len(tt_rows)} team total(s)  |  {len(prop_shadow_rows)} player prop(s)")
    print("=" * 70)

    print("\nDone.")


if __name__ == "__main__":
    main()
