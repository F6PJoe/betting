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
PERFORMANCE_ALL_TAB = "Performance (All Lines)"

PERFORMANCE_HEADER = [
    "Scope", "Bet Type", "Stars", "Bets", "Wins", "Losses", "Pushes",
    "Win %", "Units Staked", "Units Result", "ROI %",
    # "(pts)" is in the name deliberately: this column is an average LINE
    # movement in points — half a point better than the close is 0.5 — not a
    # percentage like its two neighbours. Without the unit it reads as one.
    "Avg CLV Line (pts)", "Avg CLV Price %",
]

# Columns H, K and M hold true percentages and are stored as FRACTIONS (0.473)
# with a percent display format. Sheets' percent format multiplies by 100, so a
# stored 47.3 would render as 4730%. Storing the fraction is also the honest
# representation: the cell really is a percentage, so anything the owner builds
# on top of it behaves.
PERFORMANCE_PCT_COLS = ["Win %", "ROI %", "Avg CLV Price %"]
PERFORMANCE_NUM_COLS = ["Units Staked", "Units Result", "Avg CLV Line (pts)"]


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


# Bet Type -> the actual stat we grade it against
PROP_STAT = {
    "Pass Yds": "passing_yards", "Pass TDs": "passing_tds",
    "Rush Yds": "rushing_yards", "Rec Yds": "receiving_yards",
    "Receptions": "receptions", "Anytime TD": "any_td",
}
PROP_TYPES = set(PROP_STAT)


def load_player_game_stats(season: int = 2026) -> tuple[dict, dict]:
    """
    Per-player, per-game actuals aggregated from play-by-play, plus the set of
    (game, player) pairs that actually took an offensive snap.

    Returns ({(nflverse_game_id, player_id): {stat: value}}, played_set).

    Built from PBP rather than the combined player_stats release because that
    release lags (it only ran through 2024 when checked) and PBP is the source
    everything else is derived from anyway.

    THE PLAYED SET MATTERS: a prop on a player who is inactive is VOIDED by the
    book, not lost. But a player who suits up and records nothing is a genuine
    Under win / anytime-TD loss. PBP alone cannot tell those apart — a WR who
    played but was never targeted simply has no rows — so snap counts are what
    separate "didn't play" from "played, did nothing".
    """
    import nfl_props_data as props_data
    try:
        pbp = props_data._load_pbp(season)
    except Exception as e:
        print(f"  [info] no {season} play-by-play yet ({e})")
        return {}, {}

    stats = {}

    def bump(gid, pid, key, val):
        if not pid or pid != pid:
            return
        # PBP leaves passing_yards / receiving_yards BLANK (NaN) on every
        # incomplete pass, and `val or 0` does not catch that: NaN is truthy.
        # One incompletion made a player's whole yardage total NaN — 35 QBs and
        # 146 receivers in Week 1. That crashed the Bet History write on 09-11
        # and, had it not crashed, would have graded every Over on those props
        # a Loss and every Under a Win (NaN > line is always False). Yards on
        # an incompletion are genuinely zero, so zero is the correct value.
        try:
            v = float(val)
        except (TypeError, ValueError):
            v = 0.0
        if v != v:          # NaN
            v = 0.0
        stats.setdefault((gid, pid), {}).setdefault(key, 0.0)
        stats[(gid, pid)][key] += v

    for _, p in pbp.iterrows():
        gid = p.get("game_id")
        if p.get("passer_player_id") == p.get("passer_player_id") and p.get("passer_player_id"):
            bump(gid, p["passer_player_id"], "passing_yards", p.get("passing_yards"))
            bump(gid, p["passer_player_id"], "passing_tds", p.get("pass_touchdown"))
        if p.get("rusher_player_id") == p.get("rusher_player_id") and p.get("rusher_player_id"):
            bump(gid, p["rusher_player_id"], "rushing_yards", p.get("rushing_yards"))
        if p.get("receiver_player_id") == p.get("receiver_player_id") and p.get("receiver_player_id"):
            bump(gid, p["receiver_player_id"], "receiving_yards", p.get("receiving_yards"))
            bump(gid, p["receiver_player_id"], "receptions", p.get("complete_pass"))
        td_pid = p.get("td_player_id")
        if td_pid and td_pid == td_pid:
            bump(gid, td_pid, "any_td", 1)

    played = {}
    try:
        snaps = nfl_data.import_snap_counts([season])
        roster = nfl_data.import_seasonal_rosters([season])
        pfr_to_gsis = {v: k for k, v in zip(roster["player_id"], roster["pfr_id"]) if v}
        for _, r in snaps.iterrows():
            if (r.get("offense_snaps") or 0) > 0:
                pid = pfr_to_gsis.get(r.get("pfr_player_id"))
                if pid:
                    played.setdefault(str(r.get("game_id")), set()).add(pid)
    except Exception as e:
        print(f"  [info] snap counts unavailable ({e}) — props will DEFER, not void")

    return stats, played


def build_nflverse_game_ids(season: int = 2026) -> dict:
    """{(home_abbr, away_abbr): nflverse game_id} for completed games."""
    s = nfl_data.import_schedules([season])
    s = s[(s["game_type"] == "REG") & s["home_score"].notna()]
    return {(r["home_team"], r["away_team"]): r["game_id"] for _, r in s.iterrows()}


def grade_prop(bet_type: str, side: str, line, nfl_gid: str, player_id: str,
               stats: dict, played: dict):
    """
    Grade one player prop.

    Returns (result, actual). result is Win/Loss/Push/Void, or None meaning
    DEFER — we genuinely cannot tell yet, so leave the row ungraded and let the
    lookback pick it up on a later run. Never guess: a wrongly-graded prop is
    silently wrong forever, whereas a deferred one self-heals.
    """
    stat_key = PROP_STAT.get(bet_type)
    if not stat_key or not player_id:
        return None, None

    game_played = played.get(nfl_gid)
    if game_played is None:
        return None, None                       # snaps not published yet -> defer

    if player_id not in game_played:
        return "Void", None                     # inactive: books refund the prop

    actual = float(stats.get((nfl_gid, player_id), {}).get(stat_key, 0.0))

    if bet_type == "Anytime TD":
        return ("Win" if actual >= 1 else "Loss"), actual

    if line is None:
        return None, None
    direction = str(side).rsplit(" ", 1)[-1].strip().lower()
    if abs(actual - line) < 1e-9:
        return "Push", actual
    over = actual > line
    if direction == "over":
        return ("Win" if over else "Loss"), actual
    if direction == "under":
        return ("Loss" if over else "Win"), actual
    return None, actual


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

    # Props need player-level actuals and a name->id map; only pay for them if
    # there is actually an ungraded prop waiting.
    ix_bt = ix["Bet Type"]
    need_props = any(str(r[ix_bt]) in PROP_TYPES and not str(r[ix["Result"]]).strip()
                     for r in rows)
    if need_props:
        import nfl_props_data as props_data
        pstats, played = load_player_game_stats()
        nfl_gids = build_nflverse_game_ids()
        name_map = props_data.build_name_to_id()
        name_map.pop("_ambiguous", None)
    else:
        pstats = played = nfl_gids = name_map = {}

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
        bet_type = str(r[ix["Bet Type"]])
        side = str(r[ix["Side"]])

        if bet_type in PROP_TYPES:
            # Side is "Player Over"/"Player Under", or a bare name for anytime TD
            player = side.rsplit(" ", 1)[0] if bet_type != "Anytime TD" else side
            pid = props_data.resolve_player_id(player, name_map) if name_map else None
            result, actual = grade_prop(bet_type, side, line,
                                        nfl_gids.get((home, away)), pid, pstats, played)
            if result is None:
                awaiting += 1        # defer, don't guess — the lookback retries
                continue
        else:
            result, actual = grade_one(bet_type, side, line, home, away, hs, as_)
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
            units_result = 0            # Push and Void both return the stake

        r[ix["Home Score"]] = hs
        r[ix["Away Score"]] = as_
        r[ix["Actual"]] = actual
        r[ix["Result"]] = result
        r[ix["Units Result"]] = units_result
        r[ix["Graded"]] = today
        graded += 1

    if graded:
        # Never clear-then-write — this exact line wiped Bet History on 09-11.
        # See tracking.safe_rewrite.
        tracking.safe_rewrite(ws, [header] + rows)
        tracking._pin_numeric_formats(ws, header, tracking.BET_HISTORY_NUMERIC_COLS)

    return {"graded": graded, "awaiting": awaiting, "unmatched": unmatched}


def _highlight_total_row(worksheet, row_idx: int | None) -> None:
    """
    Bold + yellow on the TOTAL row, applied by CODE rather than by hand.

    WHY IT CANNOT BE MANUAL (2026-09-28): the owner formatted it in the sheet,
    but cell formatting stays pinned to a row NUMBER while this row moves — the
    weekly ledger above it gains a row every week. Their highlight had already
    come adrift onto an ordinary data row by the next rebuild.

    So the whole data range is reset to plain first and the highlight re-applied
    wherever the row now sits. Resetting is the half that matters: without it
    every old position keeps its yellow and the tab ends up striped.

    Yellow is RGB(1,1,0) — read off the owner's own formatting rather than
    guessed at, so it matches what they chose.
    """
    if row_idx is None:
        return
    ncols = len(PERFORMANCE_HEADER)
    plain = {"backgroundColor": {"red": 1, "green": 1, "blue": 1},
             "textFormat": {"bold": False}}
    reqs = [
        {"repeatCell": {
            "range": {"sheetId": worksheet.id, "startRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": ncols},
            "cell": {"userEnteredFormat": plain},
            "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat.bold",
        }},
        {"repeatCell": {
            "range": {"sheetId": worksheet.id,
                      "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                      "startColumnIndex": 0, "endColumnIndex": ncols},
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 1, "green": 1, "blue": 0},
                "textFormat": {"bold": True}}},
            "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat.bold",
        }},
    ]
    try:
        worksheet.spreadsheet.batch_update({"requests": reqs})
    except Exception as e:
        print(f"  [warn] could not highlight the TOTAL row: {e}")


def rebuild_performance(gc) -> int:
    """
    Rebuild the performance tabs from graded rows.

    Written at TWO scopes so the correlation between same-game lines can never
    silently inflate the record:
      "Primary" — one bet per opinion group. THE HEADLINE RECORD.
      "All Lines" — every tracked line. Useful for CLV and for asking whether
                    later entries beat first ones; NOT a win-rate to quote.

    Each scope gets its OWN TAB, each opening with a TOTAL banner. See the
    comment at the bottom of this function for why they are not stacked.
    """
    ws = tracking._tab(gc, tracking.BET_HISTORY_TAB, tracking.BET_HISTORY_HEADER)
    rows = edges.sheet_to_dicts(ws)
    all_graded = [r for r in rows if str(r.get("Result", "")).strip()]

    # SCOPED TO THE CURRENT MODEL. See tracking.model_cohort: bets from before
    # the 2026-09-08 fixes came from a model we no longer run, and pooling them
    # describes nothing that exists. They stay in Bet History, graded and
    # tagged; they just do not count as this model's record.
    def cohort(r):
        c = str(r.get("Model", "")).strip()
        return c or tracking.model_cohort(r.get("Entry Date"), r.get("Entry Run"))

    graded = [r for r in all_graded if cohort(r) == tracking.CURRENT_MODEL]

    # ── Weekly ledger ────────────────────────────────────────────────────────
    # Grouped by the game's NFL WEEK, taken from the schedule — not by entry
    # date. A Week 3 game flagged on the Tuesday before it is still Week 3, and
    # NFL weeks straddle Thursday to Monday, so date arithmetic gets the
    # boundaries wrong (a Monday-night kickoff is already Tuesday in UTC).
    #
    # COVERS EVERY COHORT, unlike the by-type block below. This is a factual
    # ledger of what was recorded each week, and scoping it to the current model
    # would leave Weeks 1-2 blank when those results genuinely happened. The
    # cohort that produced each week is named in the row so the two are never
    # confused.
    try:
        s = nfl_data.import_schedules([2026])
        week_of = {(r["home_team"], r["away_team"]): int(r["week"])
                   for _, r in s[s["game_type"] == "REG"].iterrows()}
    except Exception as e:
        print(f"  [warn] weekly ledger unavailable ({e})")
        week_of = {}

    def weekly(subset):
        # SPLIT BY WEEK *AND* MODEL, not week alone. Blending them hid that
        # Week 3's +5.66 was mostly 12 bets from the retired model on the
        # Thursday game, while the current model's own first outing was roughly
        # flat. Dropping the old rows instead would blank Weeks 1-2 entirely —
        # there are no current-model bets in them — and lose real history for
        # no gain, since the headline record above already excludes them.
        buckets = {}
        for r in subset:
            home, away = _teams_from_label(r.get("Game", ""))
            wk = week_of.get((home, away))
            if wk is None:
                continue
            tag = ("current model" if cohort(r) == tracking.CURRENT_MODEL
                   else "retired model")
            b = buckets.setdefault((wk, tag), {"w": 0, "l": 0, "p": 0, "staked": 0.0,
                                               "res": 0.0, "models": set()})
            res = str(r.get("Result", ""))
            b["w"] += res == "Win"
            b["l"] += res == "Loss"
            b["p"] += res == "Push"
            # Void returns the stake: it is not a result, so it is left out of
            # the W/L/P counts and contributes 0 units, same as a push.
            if res in ("Win", "Loss"):
                b["staked"] += tracking._num(r.get("Entry Units"), 0) or 0
            b["res"] += tracking._num(r.get("Units Result"), 0) or 0
            b["models"].add(cohort(r))
        out = []
        # current model first within each week — it is the one that matters now
        for wk, tag in sorted(buckets, key=lambda k: (k[0], k[1] != "current model")):
            b = buckets[(wk, tag)]
            decided = b["w"] + b["l"]
            out.append([
                f"Week {wk}", f"{tag} ({', '.join(sorted(b['models']))})", "",
                decided + b["p"], b["w"], b["l"], b["p"],
                round(b["w"] / decided, 4) if decided else "",
                round(b["staked"], 2), round(b["res"], 3),
                round(b["res"] / b["staked"], 4) if b["staked"] else "",
                "", "",
            ])
        return out

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
            # Staked counts DECIDED bets only. A push or a void returns the
            # stake, so including it inflates the ROI denominator and quietly
            # understates ROI. The TOTAL banner and the weekly ledger already
            # counted it this way; this block did not, which put 69.30 against
            # the banner's 67.80 on the same tab for the same bets.
            if res in ("Win", "Loss"):
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
                round(b["w"] / decided, 4) if decided else "",
                round(b["staked"], 2), round(b["res"], 3),
                round(b["res"] / b["staked"], 4) if b["staked"] else "",
                # CLV is a per-bet RATE — average it, never sum it. Summing
                # scales with bet count and means nothing.
                round(sum(b["clv_l"]) / len(b["clv_l"]), 3) if b["clv_l"] else "",
                # Stored in Bet History as percentage POINTS (0.72 = 0.72%), so
                # /100 to make it the fraction a percent format expects.
                round(sum(b["clv_p"]) / len(b["clv_p"]) / 100, 4) if b["clv_p"] else "",
            ])
        return out

    def total_line(scope_name, subset):
        """One-line total for the banner row.

        The numbers go in the banner as TEXT, with every numeric column left
        EMPTY. That is deliberate: a totals row carrying real numbers makes
        summing the units column return double the true figure, which is the
        same trap this split is fixing. Blank numeric cells mean the column
        still adds up to exactly the scope's result.
        """
        w_ = sum(1 for r in subset if str(r.get("Result", "")) == "Win")
        l_ = sum(1 for r in subset if str(r.get("Result", "")) == "Loss")
        staked = sum(tracking._num(r.get("Entry Units"), 0) or 0 for r in subset
                     if str(r.get("Result", "")) in ("Win", "Loss"))
        res = sum(tracking._num(r.get("Units Result"), 0) or 0 for r in subset)
        clv = [v for r in subset if (v := tracking._num(r.get("CLV Line"))) is not None]
        txt = (f"{w_}-{l_} ({w_ / (w_ + l_) * 100:.1f}%) · {res:+.2f} units on "
               f"{staked:.1f} staked · ROI {res / staked * 100:+.1f}% · "
               f"avg CLV line {sum(clv) / len(clv):+.2f}" if (w_ + l_) and staked
               else "no graded bets yet")
        return ["TOTAL", txt] + [""] * (len(PERFORMANCE_HEADER) - 2)

    primary = [r for r in graded if str(r.get("Is Primary", "")).upper() == "TRUE"]

    def totals_row(body: list[list], subset: list[dict]) -> list:
        """
        Numeric total under the by-type block.

        D-G and I-J are summed straight off the by-type rows, so the row
        visibly adds up to what is printed. H and K are RECOMPUTED from those sums
        rather than averaged down the column — a mean of per-bucket win rates
        or ROIs weights a 1-bet bucket the same as a 40-bet one and is simply
        the wrong number.

        L-M likewise come from the underlying bets, not from averaging the
        per-row averages, for the same reason.
        """
        def col(i):
            return sum(float(r[i]) for r in body if str(r[i]).strip() != "")

        bets, wins, losses, pushes = col(3), col(4), col(5), col(6)
        staked, res = col(8), col(9)
        decided = wins + losses
        clv_l = [v for r in subset if (v := tracking._num(r.get("CLV Line"))) is not None]
        clv_p = [v for r in subset if (v := tracking._num(r.get("CLV Price %"))) is not None]
        return [
            "TOTAL", "all bet types below", "",
            int(bets), int(wins), int(losses), int(pushes),
            round(wins / decided, 4) if decided else "",
            round(staked, 2), round(res, 3),
            round(res / staked, 4) if staked else "",
            round(sum(clv_l) / len(clv_l), 3) if clv_l else "",
            round(sum(clv_p) / len(clv_p) / 100, 4) if clv_p else "",
        ]

    # TWO TABS, NOT TWO BLOCKS ON ONE TAB (2026-09-21). Both scopes used to be
    # stacked in a single sheet, so adding up the Units Result column returned
    # -82.7 when the real figure was -25.6: "All Lines" contains every "Primary"
    # bet a second time, and nothing on the tab said so. The owner hit exactly
    # that. A tab whose column does not add up is worse than no tab.
    written = 0
    for tab, scope, subset, note in (
        (PERFORMANCE_TAB, "Primary", primary,
         "THE RECORD — one bet per opinion. Lions -3.5/-4.5/-5.5 is one opinion, "
         "not three, so counting each line would flatter the record and make "
         "volatile-line weeks score higher than quiet ones."),
        (PERFORMANCE_ALL_TAB, "All Lines", graded,
         "DIAGNOSTIC ONLY — every tracked line, for CLV and for asking whether "
         "later entries beat first ones. It INCLUDES every bet on the "
         f"'{PERFORMANCE_TAB}' tab a second time, so it is NOT a win rate to "
         "quote and its total must never be added to that one."),
    ):
        w = tracking._tab(gc, tab, PERFORMANCE_HEADER)
        pad = [""] * (len(PERFORMANCE_HEADER) - 2)
        grid = [PERFORMANCE_HEADER,
                total_line(scope, subset),
                ["", note] + pad]

        # Weekly ledger, on the headline tab only (2026-09-28, owner request).
        # It covers EVERY cohort, so it does not match the TOTAL banner above,
        # which is current-model only — the section header says so rather than
        # leaving the reader to discover it by subtraction.
        if tab == PERFORMANCE_TAB:
            wk_rows = weekly([r for r in all_graded
                              if str(r.get("Is Primary", "")).upper() == "TRUE"])
            if wk_rows:
                grid.append(["", "BY WEEK — every model version, one bet per "
                                 "opinion. A game's week comes from the schedule, "
                                 "so a bet sits in the week it was PLAYED."] + pad)
                grid += wk_rows
                grid.append(["", "BY BET TYPE — current model only, so these rows "
                                 "and the weekly rows above are two cuts of "
                                 "different sets. Adding the units column down the "
                                 "whole tab double-counts; the TOTAL row is the "
                                 "figure to read."] + pad)
        # TOTAL sits at the TOP of the by-type block, not the bottom (owner
        # request 2026-09-28). The weekly ledger above gains a row every week,
        # so a total pinned to the bottom drifts further off-screen all season.
        body = summarise(scope, subset)
        total_at = None
        if body:
            total_at = len(grid)          # 0-based row index of the TOTAL row
            grid.append(totals_row(body, subset))
        grid += body
        tracking.safe_rewrite(w, grid)          # never clear-then-write
        # FIXED 2 DECIMALS on every money/rate column (owner request 2026-09-28).
        # The stored numbers keep full precision — only the display is pinned —
        # so Sheets' own =SUM() still matches the TOTAL banner exactly while
        # what you read is consistent. Units Staked is included with J-M
        # because the previous "0.####" pattern rendered a whole number as "6."
        # and it looked broken sitting next to the others.
        tracking._pin_numeric_formats(w, PERFORMANCE_HEADER,
                                      PERFORMANCE_NUM_COLS, pattern="0.00")
        tracking._pin_numeric_formats(w, PERFORMANCE_HEADER,
                                      PERFORMANCE_PCT_COLS, pattern="0.00%")
        _highlight_total_row(w, total_at)
        written += len(body)
    return written


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
