"""
nfl_grade_bets.py — Grade tracked NFL bets and rebuild the Performance tab.

Run after games finish (the morning run does this automatically via
nfl_analyze_edges, but this can be run standalone too).

TWO RULES THIS FILE EXISTS TO ENFORCE
-------------------------------------
1. GRADING IS NEVER ONE-SHOT. Every MLB grader filtered on `date == yesterday`,
   so any row missed once — an API hiccup, a late game, a name mismatch, a
   crashed run — stayed ungraded forever. 1,033 rows had to be recovered by
   hand. This grades EVERY ungraded row whose game has a final score, however
   old, on every run.

2. THE W/L RECORD COUNTS ONE BET PER OPINION GROUP. Lions -3.5, -4.5 and -5.5
   are three tracked bets (three CLV observations, three individual grades) but
   ONE opinion. Counting three wins would flatter the record for bets nobody
   would place, and make weeks with volatile lines score higher than quiet ones
   — market noise, not model skill. Every row is graded individually; only
   `Is Primary` rows roll into the headline record.
"""

from datetime import datetime, timezone

import gspread
import nfl_data_py as nfl_data

import nfl_analyze_edges as edges
import nfl_bet_tracking as tracking

PERFORMANCE_TAB = "Performance"

PERFORMANCE_HEADER = [
    "Scope", "Bet Type", "Stars", "Bets", "Wins", "Losses", "Pushes",
    "Win %", "Units Staked", "Units Result", "ROI %", "Avg CLV Line", "Avg CLV Price %",
]


def american_payout(price) -> float | None:
    """Profit per 1 unit staked at American odds. +150 -> 1.5, -110 -> 0.909."""
    try:
        p = float(price)
    except (TypeError, ValueError):
        return None
    return p / 100.0 if p >= 100 else 100.0 / abs(p)


def load_final_scores(season: int = 2026) -> dict:
    """{(home_abbr, away_abbr): (home_score, away_score)} for completed games."""
    s = nfl_data.import_schedules([season])
    s = s[(s["game_type"] == "REG") & s["home_score"].notna()]
    return {(r["home_team"], r["away_team"]): (float(r["home_score"]), float(r["away_score"]))
            for _, r in s.iterrows()}


def _teams_from_label(label: str):
    """'Away Team @ Home Team' -> (home_abbr, away_abbr)."""
    if " @ " not in str(label):
        return None, None
    away, home = str(label).split(" @ ", 1)
    return (edges.TEAM_NAME_TO_ABBR.get(home.strip()),
            edges.TEAM_NAME_TO_ABBR.get(away.strip()))


def grade_one(bet_type: str, side: str, line, home_abbr, away_abbr,
              home_score: float, away_score: float):
    """
    Returns (result, actual) where result is Win/Loss/Push and `actual` is the
    number the bet was settled against (total, margin, or team score).
    """
    total = home_score + away_score
    s = str(side).strip()

    if bet_type == "Game Total":
        if line is None:
            return None, None
        actual = total
        if abs(actual - line) < 1e-9:
            return "Push", actual
        over = actual > line
        return ("Win" if over else "Loss") if s.lower() == "over" else ("Loss" if over else "Win"), actual

    if bet_type == "Moneyline":
        abbr = edges.TEAM_NAME_TO_ABBR.get(s)
        if abbr is None:
            return None, None
        if abs(home_score - away_score) < 1e-9:
            return "Push", home_score - away_score
        won = (abbr == home_abbr and home_score > away_score) or \
              (abbr == away_abbr and away_score > home_score)
        return ("Win" if won else "Loss"), home_score - away_score

    if bet_type == "Spread":
        abbr = edges.TEAM_NAME_TO_ABBR.get(s)
        if abbr is None or line is None:
            return None, None
        own, opp = ((home_score, away_score) if abbr == home_abbr
                    else (away_score, home_score))
        margin = own - opp            # from the bet side's perspective
        adj = margin + line           # line is that side's handicap
        if abs(adj) < 1e-9:
            return "Push", margin
        return ("Win" if adj > 0 else "Loss"), margin

    if bet_type == "Team Total":
        # Side looks like "Detroit Lions Over"
        parts = s.rsplit(" ", 1)
        if len(parts) != 2 or line is None:
            return None, None
        team, direction = parts[0].strip(), parts[1].strip().lower()
        abbr = edges.TEAM_NAME_TO_ABBR.get(team)
        if abbr is None:
            return None, None
        actual = home_score if abbr == home_abbr else away_score
        if abs(actual - line) < 1e-9:
            return "Push", actual
        over = actual > line
        return ("Win" if over else "Loss") if direction == "over" else ("Loss" if over else "Win"), actual

    return None, None


def grade_bet_history(gc) -> dict:
    """Grade every ungraded row that now has a final score. Backfills by design."""
    ws = tracking._tab(gc, tracking.BET_HISTORY_TAB, tracking.BET_HISTORY_HEADER)
    values = ws.get_all_values(
        value_render_option=gspread.utils.ValueRenderOption.unformatted)
    if len(values) < 2:
        return {"graded": 0, "awaiting": 0, "unmatched": 0}

    header = values[0]
    ix = {h: i for i, h in enumerate(header)}
    rows = [list(r) + [""] * (len(header) - len(r)) for r in values[1:]]
    scores = load_final_scores()

    graded = awaiting = unmatched = 0
    today = datetime.now().strftime("%Y-%m-%d")

    for r in rows:
        if str(r[ix["Result"]]).strip():
            continue                                    # already graded
        home, away = _teams_from_label(r[ix["Game"]])
        if not home or not away:
            unmatched += 1
            continue
        if (home, away) not in scores:
            awaiting += 1                               # not final yet
            continue

        hs, as_ = scores[(home, away)]
        line = tracking._num(r[ix["Entry Line"]])
        result, actual = grade_one(str(r[ix["Bet Type"]]), str(r[ix["Side"]]),
                                   line, home, away, hs, as_)
        if result is None:
            unmatched += 1
            continue

        units = tracking._num(r[ix["Entry Units"]], 0) or 0
        payout = american_payout(r[ix["Entry Price"]])
        if result == "Win":
            units_result = round(units * payout, 3) if payout else ""
        elif result == "Loss":
            units_result = -units
        else:
            units_result = 0

        r[ix["Home Score"]] = hs
        r[ix["Away Score"]] = as_
        r[ix["Actual"]] = actual
        r[ix["Result"]] = result
        r[ix["Units Result"]] = units_result
        r[ix["Graded"]] = today
        graded += 1

    if graded:
        ws.clear()
        ws.update([header] + rows, value_input_option="RAW")
        tracking._pin_numeric_formats(ws, header, tracking.BET_HISTORY_NUMERIC_COLS)

    return {"graded": graded, "awaiting": awaiting, "unmatched": unmatched}


def rebuild_performance(gc) -> int:
    """
    Rebuild the Performance tab from graded rows.

    Written at TWO scopes so the correlation between same-game lines can never
    silently inflate the record:
      "Primary" — one bet per opinion group. THE HEADLINE RECORD.
      "All Lines" — every tracked line. Useful for CLV and for asking whether
                    later entries beat first ones; NOT a win-rate to quote.
    """
    ws = tracking._tab(gc, tracking.BET_HISTORY_TAB, tracking.BET_HISTORY_HEADER)
    rows = edges.sheet_to_dicts(ws)
    graded = [r for r in rows if str(r.get("Result", "")).strip()]

    def summarise(scope_name, subset):
        buckets = {}
        for r in subset:
            key = (str(r.get("Bet Type", "")), str(r.get("Entry Stars", "")))
            b = buckets.setdefault(key, {"w": 0, "l": 0, "p": 0, "staked": 0.0,
                                         "res": 0.0, "clv_l": [], "clv_p": []})
            res = str(r.get("Result", ""))
            b["w"] += res == "Win"
            b["l"] += res == "Loss"
            b["p"] += res == "Push"
            b["staked"] += tracking._num(r.get("Entry Units"), 0) or 0
            b["res"] += tracking._num(r.get("Units Result"), 0) or 0
            for col, dest in (("CLV Line", "clv_l"), ("CLV Price %", "clv_p")):
                v = tracking._num(r.get(col))
                if v is not None:
                    b[dest].append(v)
        out = []
        for (bt_, stars), b in sorted(buckets.items()):
            decided = b["w"] + b["l"]
            n = decided + b["p"]
            out.append([
                scope_name, bt_, stars, n, b["w"], b["l"], b["p"],
                round(b["w"] / decided * 100, 1) if decided else "",
                round(b["staked"], 2), round(b["res"], 3),
                round(b["res"] / b["staked"] * 100, 1) if b["staked"] else "",
                # CLV is a per-bet RATE — average it, never sum it. Summing
                # scales with bet count and means nothing.
                round(sum(b["clv_l"]) / len(b["clv_l"]), 3) if b["clv_l"] else "",
                round(sum(b["clv_p"]) / len(b["clv_p"]), 3) if b["clv_p"] else "",
            ])
        return out

    primary = [r for r in graded if str(r.get("Is Primary", "")).upper() == "TRUE"]
    perf = summarise("Primary", primary) + summarise("All Lines", graded)

    w = tracking._tab(gc, PERFORMANCE_TAB, PERFORMANCE_HEADER)
    w.clear()
    w.update([PERFORMANCE_HEADER] + perf, value_input_option="RAW")
    return len(perf)


def main():
    print("=" * 60)
    print("nfl_grade_bets.py — Fantasy Six Pack NFL Bet Grader")
    print(f"Run time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    gc = edges.get_client()

    print("\nCapturing any closing lines still outstanding ...")
    cap = tracking.capture_closing_and_clv(gc)
    print(f"  {cap['captured']} captured, {cap['pending']} awaiting kickoff")

    print("Grading (full lookback — never one-shot) ...")
    g = grade_bet_history(gc)
    print(f"  {g['graded']} graded, {g['awaiting']} awaiting final score"
          + (f", {g['unmatched']} unmatched" if g["unmatched"] else ""))

    print("Rebuilding Performance tab ...")
    n = rebuild_performance(gc)
    print(f"  {n} summary row(s)")
    print("\nDone.")


if __name__ == "__main__":
    main()
