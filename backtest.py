"""
backtest.py — Historical backtesting for the Fantasy Six Pack MLB model.

Pulls all 2026 completed regular-season games from the MLB Stats API,
runs our projection model on each game (using only static inputs like
park factors and league-average ERA, since we can't reconstruct daily
pitcher data historically), then reports:

  • Overall projection accuracy (mean/median error, std dev)
  • By park factor bucket (are our PF assignments working?)
  • Over/under hit rates at various edge thresholds
  • Systematic bias detection (are we consistently over/under projecting?)
  • Top missed games for manual review

Usage:
  python backtest.py [--season 2026] [--output results.txt]

This is a research tool — run it weekly or monthly to identify model drift.
It cannot perfectly replicate daily predictions (we don't store historical
SP data), but it shows park factor accuracy and total-scoring trends.
"""

import argparse
import requests
import math
import statistics
import os
import sys
from datetime import datetime, timedelta
from collections import defaultdict

# ── Import model constants from analyze_edges ─────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
from analyze_edges import (
    BASE_RUNS, LEAGUE_AVG_ERA, OFFENSE_WEIGHT, MIN_PROJ_TOTAL,
    PROJ_CAP_MULT, MAX_WIN_PCT, MIN_WIN_PCT, RUN_LINE_SD,
    PARK_FACTORS, SPECIAL_VENUES, TEAM_ABBREV,
    project_game, normal_cdf,
)

MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"


# ── Data fetcher ──────────────────────────────────────────────────────────────
def fetch_season_games(season: int = 2026) -> list[dict]:
    """
    Fetch all completed regular-season games for the given year.
    Returns list of dicts with away, home, away_score, home_score, venue, date.
    """
    start = f"{season}-03-01"
    end   = datetime.now().strftime("%Y-%m-%d")
    params = {
        "sportId":   1,
        "startDate": start,
        "endDate":   end,
        "gameType":  "R",
        "hydrate":   "linescore,venue",
    }
    print(f"  Fetching schedule {start} → {end} ...")
    resp = requests.get(MLB_SCHEDULE_URL, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    games = []
    for date_block in data.get("dates", []):
        date_str = date_block.get("date", "")
        for game in date_block.get("games", []):
            status = game.get("status", {}).get("abstractGameState", "")
            if status != "Final":
                continue
            teams     = game.get("teams", {})
            away_team = teams.get("away", {}).get("team", {}).get("name", "")
            home_team = teams.get("home", {}).get("team", {}).get("name", "")
            away_sc   = teams.get("away", {}).get("score")
            home_sc   = teams.get("home", {}).get("score")
            venue     = game.get("venue", {}).get("name", "").lower()

            if away_sc is None or home_sc is None:
                continue

            games.append({
                "date":       date_str,
                "away":       away_team,
                "home":       home_team,
                "away_score": int(away_sc),
                "home_score": int(home_sc),
                "actual":     int(away_sc) + int(home_sc),
                "venue":      venue,
            })

    return games


# ── Projection runner ─────────────────────────────────────────────────────────
def project_all(games: list[dict]) -> list[dict]:
    """
    Run our model projection on each game using:
    - League-average ERA for both SPs (no historical SP data stored)
    - League-average offense (0.0 adj) for both teams
    - Actual park factor from our PARK_FACTORS dict

    This tests park factor calibration specifically, stripping out pitcher noise.
    """
    results = []
    for g in games:
        home  = g["home"]
        away  = g["away"]
        venue = g["venue"]

        # Park factor: check special venues first
        special = next((f for vn, f in SPECIAL_VENUES.items() if vn in venue), None)
        park_factor = special if special else PARK_FACTORS.get(home, 100)

        proj = project_game(
            home, away,
            home_era=LEAGUE_AVG_ERA, away_era=LEAGUE_AVG_ERA,
            home_off=0.0, away_off=0.0,
            park_factor=park_factor,
        )
        proj_total = max(proj["proj_total_raw"], MIN_PROJ_TOTAL)

        results.append({
            **g,
            "park_factor": park_factor,
            "proj_total":  round(proj_total, 2),
            "error":       round(g["actual"] - proj_total, 2),
            "abs_error":   round(abs(g["actual"] - proj_total), 2),
        })

    return results


# ── Analysis ──────────────────────────────────────────────────────────────────
def analyze_results(results: list[dict]) -> str:
    out = []
    def p(s=""): out.append(s)

    p("=" * 65)
    p("FANTASY SIX PACK — MLB MODEL BACKTEST REPORT")
    p(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    p(f"Games analyzed: {len(results)}")
    p("=" * 65)

    errors = [r["error"] for r in results]
    abs_errors = [r["abs_error"] for r in results]

    p("\n── OVERALL PROJECTION ACCURACY ──────────────────────────────────")
    p(f"  Mean error (actual - proj):  {statistics.mean(errors):+.3f} runs")
    p(f"  Median error:                {statistics.median(errors):+.3f} runs")
    p(f"  Std dev:                     {statistics.stdev(errors):.3f} runs")
    p(f"  Mean absolute error (MAE):   {statistics.mean(abs_errors):.3f} runs")
    p(f"  Pct within 2 runs:           {sum(1 for e in abs_errors if e <= 2)/len(abs_errors)*100:.1f}%")
    p(f"  Pct within 3 runs:           {sum(1 for e in abs_errors if e <= 3)/len(abs_errors)*100:.1f}%")
    p()
    if abs(statistics.mean(errors)) > 0.40:
        direction = "UNDER-projecting" if statistics.mean(errors) > 0 else "OVER-projecting"
        p(f"  ⚠  SYSTEMATIC BIAS DETECTED: {direction} by {abs(statistics.mean(errors)):.2f} runs")
        p(f"     Consider adjusting BASE_RUNS (currently {BASE_RUNS:.2f})")
    else:
        p(f"  ✓  No significant systematic bias (mean error within ±0.40 runs)")

    # ── By park factor bucket ─────────────────────────────────────────────────
    p("\n── ACCURACY BY PARK FACTOR BUCKET ───────────────────────────────")
    pf_buckets = defaultdict(list)
    for r in results:
        pf = r["park_factor"]
        bucket = (
            "90-95 (pitcher)" if pf <= 95 else
            "96-99 (slight pitcher)" if pf <= 99 else
            "100-104 (neutral)" if pf <= 104 else
            "105-115 (hitter)" if pf <= 115 else
            "116+ (extreme hitter)"
        )
        pf_buckets[bucket].append(r["error"])
    p(f"  {'Bucket':<25} {'N':>4} {'Mean Err':>10} {'Std Dev':>8}")
    p("  " + "-" * 52)
    for bkt in sorted(pf_buckets.keys()):
        errs = pf_buckets[bkt]
        p(f"  {bkt:<25} {len(errs):>4} {statistics.mean(errs):>+10.3f} {statistics.stdev(errs) if len(errs)>1 else 0:>8.3f}")

    # ── By home team (park factor accuracy) ──────────────────────────────────
    p("\n── ACCURACY BY HOME TEAM (min 10 games) ─────────────────────────")
    team_data = defaultdict(list)
    for r in results:
        team_data[r["home"]].append((r["error"], r["park_factor"]))

    rows = []
    for team, data in team_data.items():
        if len(data) < 10:
            continue
        errs = [e for e, _ in data]
        pf   = data[0][1]
        rows.append((team, len(data), statistics.mean(errs), pf))

    rows.sort(key=lambda x: abs(x[2]), reverse=True)
    p(f"  {'Team':<30} {'Games':>5} {'Mean Err':>10} {'PF':>5} {'Status':>20}")
    p("  " + "-" * 75)
    for team, n, mean_err, pf in rows:
        if abs(mean_err) >= 0.60:
            status = ">>> REVIEW PF" if abs(mean_err) >= 1.0 else "  Watch"
        else:
            status = "  ~OK"
        err_dir = "under-proj" if mean_err > 0 else "over-proj"
        p(f"  {team:<30} {n:>5} {mean_err:>+10.3f} {pf:>5} {status:>20}")
    p()
    p("  Mean Err > 0 = model under-projecting (raise park factor or BASE_RUNS)")
    p("  Mean Err < 0 = model over-projecting (lower park factor or BASE_RUNS)")

    # ── Simulated edge hit rates ──────────────────────────────────────────────
    p("\n── SIMULATED OVER/UNDER HIT RATES AT VARIOUS EDGES ─────────────")
    p("  (Uses league-avg ERA projection vs actual total — shows if edges are predictive)")
    p(f"  {'Edge Threshold':>18} {'Over N':>7} {'Over W':>7} {'Over%':>6}   {'Under N':>7} {'Under W':>7} {'Under%':>6}")
    p("  " + "-" * 70)

    for threshold in [0.50, 0.75, 1.00, 1.25, 1.50, 1.75, 2.00]:
        over_n  = over_w  = 0
        under_n = under_w = 0
        for r in results:
            edge = r["proj_total"] - r.get("consensus_line", r["proj_total"])
            # Simulate: if our proj > park-neutral baseline by X, would an OVER hit?
            # We compute edge vs a neutral projection
            neutral = max(BASE_RUNS * 2, MIN_PROJ_TOTAL)  # league-avg game
            over_edge  = r["proj_total"] - neutral
            under_edge = neutral - r["proj_total"]

            # Check if actual would have beaten a hypothetical book line = neutral
            if over_edge >= threshold:
                over_n += 1
                if r["actual"] > neutral:
                    over_w += 1
            if under_edge >= threshold:
                under_n += 1
                if r["actual"] < neutral:
                    under_w += 1

        over_pct  = over_w  / over_n  * 100 if over_n  else 0
        under_pct = under_w / under_n * 100 if under_n else 0
        p(f"  {f'>= {threshold:.2f} runs':>18} {over_n:>7} {over_w:>7} {over_pct:>5.1f}%   "
          f"{under_n:>7} {under_w:>7} {under_pct:>5.1f}%")

    # ── Biggest misses ────────────────────────────────────────────────────────
    p("\n── TOP 15 BIGGEST MISSES ────────────────────────────────────────")
    big = sorted(results, key=lambda r: r["abs_error"], reverse=True)[:15]
    p(f"  {'Date':>10} {'Away':<25} {'Home':<25} {'Proj':>6} {'Actual':>7} {'Error':>7}")
    p("  " + "-" * 85)
    for r in big:
        p(f"  {r['date']:>10} {r['away']:<25} {r['home']:<25} "
          f"{r['proj_total']:>6.1f} {r['actual']:>7} {r['error']:>+7.1f}")

    # ── Monthly trend ─────────────────────────────────────────────────────────
    p("\n── MONTHLY ACCURACY TREND ───────────────────────────────────────")
    monthly = defaultdict(list)
    for r in results:
        month = r["date"][:7]  # YYYY-MM
        monthly[month].append(r["error"])
    p(f"  {'Month':>8} {'Games':>5} {'Mean Err':>10} {'Std Dev':>8}")
    p("  " + "-" * 38)
    for month in sorted(monthly.keys()):
        errs = monthly[month]
        p(f"  {month:>8} {len(errs):>5} {statistics.mean(errs):>+10.3f} {statistics.stdev(errs) if len(errs)>1 else 0:>8.3f}")

    p("\n" + "=" * 65)
    p("END OF REPORT")
    p("=" * 65)

    return "\n".join(out)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Fantasy Six Pack MLB Backtest")
    parser.add_argument("--season", type=int, default=2026, help="Season year")
    parser.add_argument("--output", type=str, default="backtest_results.txt",
                        help="Output file path")
    args = parser.parse_args()

    import io, sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    print("=" * 65)
    print("backtest.py - Fantasy Six Pack Historical Model Analysis")
    print(f"Season: {args.season}")
    print("=" * 65)

    print("\nFetching season game results ...")
    try:
        games = fetch_season_games(args.season)
    except Exception as e:
        print(f"ERROR fetching games: {e}")
        sys.exit(1)
    print(f"  {len(games)} completed games found")

    if not games:
        print("No games found — exiting.")
        sys.exit(0)

    print("Running model projections ...")
    results = project_all(games)
    print(f"  {len(results)} games projected")

    print("Analyzing results ...")
    report = analyze_results(results)

    print()
    print(report)

    # Save to file
    out_path = os.path.join(os.path.dirname(__file__), args.output)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\nReport saved to: {out_path}")


if __name__ == "__main__":
    main()
