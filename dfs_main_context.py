"""
dfs_main_context.py — Vegas lines and weather for NFL MAIN SLATE DFS.

MainRules_vNext.1 Part 3 ("Unless I explicitly tell you not to, obtain current
game totals, spreads, implied team totals") and Part 22 (weather in the late-news
pass).

    python dfs_main_context.py                  # print the report for the slate
    python dfs_main_context.py --no-odds        # weather only, spends no credits

TIMING MATTERS MORE THAN THE CODE
Weather is a T-72h data point at the earliest. A forecast pulled 18 days out is
climatology wearing a forecast's clothes — worse than nothing, because it looks
like information. Pull on Saturday, refresh Sunday morning before lock, and pull
Vegas at the same time: lines move on weather, so fetching them apart invites a
mismatch between the total you modelled and the wind that caused it.

WIND IS THE VARIABLE
Sustained wind above ~15mph degrades passing and kicking materially and is what
actually moves totals. Precipitation is consistently overweighted by the market
relative to its effect. Temperature matters only at the extremes.

CREDITS
Odds come from The Odds API via nfl_fetch_odds, on the paid pooled key. One
spreads+totals pass is ~2 credits. Weather is Open-Meteo: free, no key, no
account. `--no-odds` skips the paid half entirely.
"""

import os
import sys
from datetime import datetime, timedelta

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WEATHER_URL = "https://api.open-meteo.com/v1/forecast"

# Wind thresholds, in mph, for the report's flag column.
WIND_NOTABLE = 12
WIND_SEVERE = 18

# ── Stadiums ──────────────────────────────────────────────────────────────────
# Keyed on the HOME team's DK abbreviation: (lat, lon, roof).
#   'dome'        — always indoors, weather never applies
#   'retractable' — roof state is a game-day decision and is NOT published in any
#                   feed here, so these are reported as UNCERTAIN rather than
#                   quietly treated as indoors
#   'outdoor'     — weather applies
#
# VERIFY BEFORE THE FIRST REAL RUN. This table is not derived from any file in
# the pipeline; it was written from knowledge and stadiums do change (new builds,
# roof retrofits, temporary relocations). A wrong entry silently suppresses
# weather for a game that has it.
STADIUMS = {
    "ARI": (33.5277, -112.2626, "retractable"),
    "ATL": (33.7554, -84.4008, "retractable"),
    "BAL": (39.2780, -76.6227, "outdoor"),
    "BUF": (42.7738, -78.7870, "outdoor"),
    "CAR": (35.2258, -80.8528, "outdoor"),
    "CHI": (41.8623, -87.6167, "outdoor"),
    "CIN": (39.0955, -84.5161, "outdoor"),
    "CLE": (41.5061, -81.6995, "outdoor"),
    "DAL": (32.7473, -97.0945, "retractable"),
    "DEN": (39.7439, -105.0201, "outdoor"),
    "DET": (42.3400, -83.0456, "dome"),
    "GB":  (44.5013, -88.0622, "outdoor"),
    "HOU": (29.6847, -95.4107, "retractable"),
    "IND": (39.7601, -86.1639, "retractable"),
    "JAX": (30.3239, -81.6373, "outdoor"),
    "KC":  (39.0489, -94.4839, "outdoor"),
    "LAC": (33.9535, -118.3392, "dome"),
    "LAR": (33.9535, -118.3392, "dome"),
    "LV":  (36.0909, -115.1833, "dome"),
    "MIA": (25.9580, -80.2389, "outdoor"),
    "MIN": (44.9736, -93.2575, "dome"),
    "NE":  (42.0909, -71.2643, "outdoor"),
    "NO":  (29.9511, -90.0812, "dome"),
    "NYG": (40.8135, -74.0745, "outdoor"),
    "NYJ": (40.8135, -74.0745, "outdoor"),
    "PHI": (39.9008, -75.1675, "outdoor"),
    "PIT": (40.4468, -80.0158, "outdoor"),
    "SEA": (47.5952, -122.3316, "outdoor"),
    "SF":  (37.4033, -121.9694, "outdoor"),
    "TB":  (27.9759, -82.5033, "outdoor"),
    "TEN": (36.1665, -86.7713, "outdoor"),
    "WAS": (38.9077, -76.8645, "outdoor"),
}

# The Odds API returns full club names; DK uses abbreviations.
ODDS_TEAM = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL",
    "Buffalo Bills": "BUF", "Carolina Panthers": "CAR", "Chicago Bears": "CHI",
    "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE", "Dallas Cowboys": "DAL",
    "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX",
    "Kansas City Chiefs": "KC", "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC",
    "Los Angeles Rams": "LAR", "Miami Dolphins": "MIA", "Minnesota Vikings": "MIN",
    "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT",
    "San Francisco 49ers": "SF", "Seattle Seahawks": "SEA", "Tampa Bay Buccaneers": "TB",
    "Tennessee Titans": "TEN", "Washington Commanders": "WAS",
}


# ── Vegas ─────────────────────────────────────────────────────────────────────

def fetch_vegas(consensus_books=("draftkings", "fanduel", "betmgm")):
    """
    Current spreads and totals, with implied team totals derived.

    Returns {('AWAY','HOME'): {...}}. Reuses nfl_fetch_odds' client so there is
    one Odds API integration in this repo, not two — and one place where the
    paid key lives. Costs ~2 credits.

    Implied team total = total/2 - spread/2, with spread signed from that team's
    perspective. A -7 favourite in a 47-point game implies 27.0; the dog 20.0.
    """
    import nfl_fetch_odds as ODDS

    games, _ = ODDS.fetch_market("spreads,totals")
    out = {}
    for g in games:
        home = ODDS_TEAM.get(g.get("home_team", ""))
        away = ODDS_TEAM.get(g.get("away_team", ""))
        if not home or not away:
            continue
        totals, spreads = [], {}
        for book in g.get("bookmakers", []):
            if book.get("key") not in consensus_books:
                continue
            for m in book.get("markets", []):
                for o in m.get("outcomes", []):
                    if m.get("key") == "totals" and o.get("name") == "Over":
                        totals.append(float(o["point"]))
                    elif m.get("key") == "spreads":
                        t = ODDS_TEAM.get(o.get("name", ""))
                        if t:
                            spreads.setdefault(t, []).append(float(o["point"]))
        if not totals or home not in spreads:
            continue
        total = sum(totals) / len(totals)
        hs = sum(spreads[home]) / len(spreads[home])
        out[(away, home)] = {
            "total": round(total, 1),
            "spread_home": round(hs, 1),
            "implied": {home: round(total / 2 - hs / 2, 1),
                        away: round(total / 2 + hs / 2, 1)},
            "books": len(totals),
        }
    return out


# ── Weather ───────────────────────────────────────────────────────────────────

def fetch_weather(home_team, kickoff):
    """
    Forecast at kickoff for one game, or a reason it does not apply.

    Open-Meteo: free, keyless, and its hourly forecast horizon is about 16 days,
    so anything further out comes back unavailable rather than invented.
    """
    stadium = STADIUMS.get(home_team)
    if not stadium:
        return {"status": "unknown stadium", "applies": False}
    lat, lon, roof = stadium
    if roof == "dome":
        return {"status": "indoors", "applies": False, "roof": roof}

    day = kickoff.date()
    if not 0 <= (day - datetime.now().date()).days <= 15:
        return {"status": "outside forecast horizon", "applies": True, "roof": roof}

    try:
        r = requests.get(WEATHER_URL, timeout=20, params={
            "latitude": lat, "longitude": lon,
            "hourly": "temperature_2m,precipitation,precipitation_probability,"
                      "wind_speed_10m,wind_gusts_10m",
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
            "precipitation_unit": "inch", "timezone": "America/New_York",
            "start_date": day.isoformat(), "end_date": day.isoformat(),
        })
        r.raise_for_status()
        h = r.json()["hourly"]
    except (requests.RequestException, KeyError, ValueError) as e:
        return {"status": "fetch failed: %s" % e, "applies": True, "roof": roof}

    target = kickoff.strftime("%Y-%m-%dT%H:00")
    i = h["time"].index(target) if target in h["time"] else len(h["time"]) // 2
    return {
        "status": "ok", "applies": True, "roof": roof,
        "temp": h["temperature_2m"][i],
        "wind": h["wind_speed_10m"][i],
        "gust": h["wind_gusts_10m"][i],
        "precip": h["precipitation"][i],
        "precip_pct": h["precipitation_probability"][i],
    }


def weather_flag(w):
    """One-word read on whether this forecast should change anything."""
    if not w.get("applies"):
        return "INDOORS"
    if w.get("status") != "ok":
        return w["status"].upper()
    bits = []
    if w["wind"] >= WIND_SEVERE:
        bits.append("HIGH WIND")
    elif w["wind"] >= WIND_NOTABLE:
        bits.append("wind")
    if w["precip_pct"] >= 60 and w["precip"] >= 0.05:
        bits.append("rain")
    if w["temp"] <= 20:
        bits.append("cold")
    if w.get("roof") == "retractable":
        bits.append("ROOF UNCERTAIN")
    return ", ".join(bits) if bits else "clear"


# ── Report ────────────────────────────────────────────────────────────────────

def report(players, with_odds=True):
    """
    One table per game: Vegas, implied team totals, and the weather read.

    `players` is the authoritative table from dfs_main_ingest — game and kickoff
    come from there so this can never disagree with what the solver saw.
    """
    games = {}
    for p in players:
        if p.game and p.kickoff:
            games.setdefault(p.game, p.kickoff)

    vegas = {}
    if with_odds:
        try:
            vegas = fetch_vegas()
        except Exception as e:                       # noqa: BLE001 - report, never abort
            print("WARN  Vegas fetch failed (%s) -- continuing without lines" % e)

    lines = ["%-10s %-8s %6s %7s %-15s %s"
             % ("game", "kickoff", "total", "spread", "implied", "weather")]
    for game, kick in sorted(games.items(), key=lambda kv: (kv[1], kv[0])):
        away, home = game.split("@")
        v = vegas.get((away, home), {})
        imp = v.get("implied", {})
        w = fetch_weather(home, kick)
        lines.append("%-10s %-8s %6s %7s %-15s %s" % (
            game, kick.strftime("%a %I:%M%p").replace(" 0", " "),
            v.get("total", "--"), v.get("spread_home", "--"),
            ("%s %.1f / %s %.1f" % (away, imp[away], home, imp[home])
             if imp else "--"),
            weather_flag(w)))
    return lines


def main(argv):
    import dfs_main_ingest as ING
    args = [a for a in argv[1:] if not a.startswith("--")]
    if len(args) >= 2:
        entries, salaries = args[0], args[1]
    else:
        entries = ING._newest(r"^DKEntries.*\.csv$")
        salaries = ING._newest(r"^DKSalaries.*\.csv$")
    if not entries or not salaries:
        print("FAIL  need DKEntries and DKSalaries in %s (or pass them as args)"
              % ING.SLATE_DIR)
        return 2
    _, dk_pool = ING.load_dk_entries(entries)
    players, _ = ING.join_players(dk_pool, [], ING.load_dk_salaries(salaries))
    for line in report(players, with_odds="--no-odds" not in argv):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
