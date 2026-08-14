"""
nfl_bet_tracking.py — Bet History upsert, line logging, and CLV for the NFL model.

WHY THIS EXISTS SEPARATELY FROM MLB'S APPROACH
----------------------------------------------
MLB games turn over daily, so keying a snapshot on the DATE works: each morning
is a fresh slate. NFL games sit on the board for a week, so date-keying would
write the same Lions game seven times.

Design agreed with the owner 2026-08-12:

  Edges tab       = live view. Cleared and rewritten every run. Shows only what
                    qualifies right now. A bet that faded yesterday is gone.

  Bet History     = permanent record. Keyed on GAME + BET TYPE + SIDE + LINE.
                    Every distinct line the model ever endorsed gets its own
                    row, kept until the game is graded. Updated in place on
                    later runs, never duplicated, never overwritten.

Each distinct LINE is its own bet because they are genuinely different bets:
Lions -3.5 and Lions -5.5 disagree whenever the Lions win by 4 or 5. Grading
them as one would mean grading a line the model never actually flagged.

BUT they are NOT independent observations — same game, same opinion, they move
together. So every row carries an Opinion Group (game+type+side) and an
Is Primary flag on the first row of each group:

  * CLV analysis runs at the ROW level      — all rows, each measures a
                                               different number moving.
  * Win/ROI calibration runs at the GROUP level — primary rows only, because
                                               you would have placed the bet
                                               once, on first signal.

Entry values are FROZEN at first qualification and never rewritten. That is
what makes CLV meaningful, and overwriting them is precisely how MLB's CLV
silently read zero for its entire existence.
"""

from datetime import datetime, timezone

import gspread

import nfl_analyze_edges as edges


# ── Tab names ─────────────────────────────────────────────────────────────────
BET_HISTORY_TAB = "Bet History"
LINE_LOG_TAB = "Line Log"
PROJECTION_LOG_TAB = "Projection Log"

# Bets enter Bet History at 3 stars and up. MLB used 4+ because Bet History fed
# a public cheat sheet; the NFL model publishes nothing in year one, so the
# constraint is gone and the wider net roughly triples the calibration sample —
# which matters when a full NFL season yields a few hundred bets, not thousands.
MIN_STARS_TO_TRACK = 3


BET_HISTORY_HEADER = [
    # identity / grouping
    "Bet Key", "Opinion Group", "Is Primary",
    # when and what
    "Entry Date", "Entry Run", "Game", "Kickoff (ET)", "Bet Type", "Side", "Bet On",
    # FROZEN at first qualification — never rewritten
    "Entry Line", "Entry Consensus", "Books At Line",
    "Entry Price", "Entry Book", "Entry Stars", "Entry Units",
    "Entry Projection", "Entry Edge", "Entry Edge %",
    # refreshed every run
    "Last Seen", "Times Qualified", "Current Line", "Current Price", "Current Stars",
    # filled by the last pre-kickoff snapshot
    "Closing Line", "Closing Price", "Closing Captured",
    # CLV — signed so positive always means "we hold the better number/price"
    "CLV Line", "CLV Price %",
    # grading
    "Away Score", "Home Score", "Actual", "Result", "Units Result", "Graded",
]

LINE_LOG_HEADER = [
    "Snapshot", "Game ID", "Game", "Kickoff (ET)",
    "Bet Type", "Side", "Line", "Best Price", "Best Book", "Books",
]

PROJECTION_LOG_HEADER = [
    "Date", "Run", "Game ID", "Game", "Kickoff (ET)", "Bet Type", "Side",
    "Our Projection", "Consensus Line", "Edge", "Edge %", "Stars", "Units",
    "Qualified",
]

# Columns that must be pinned numeric on every write. Rows insert at the top and
# inherit their neighbours' formats, so an unpinned numeric column WILL drift —
# and a percent-formatted cell renders -113 as "-11300.00%", which then breaks
# any downstream parse. Pin the whole column, not just this run's rows.
BET_HISTORY_NUMERIC_COLS = [
    "Entry Line", "Entry Consensus", "Entry Price", "Entry Units",
    "Entry Projection", "Entry Edge",
    "Entry Edge %", "Current Line", "Current Price", "Closing Line", "Closing Price",
    "CLV Line", "CLV Price %", "Away Score", "Home Score", "Actual", "Units Result",
    "Times Qualified",
]


# ── Keys ──────────────────────────────────────────────────────────────────────
def _fmt_line(line) -> str:
    """Canonical string form of a handicap, so 3.5 and '3.5' key identically."""
    if line is None or line == "":
        return ""
    try:
        return f"{float(line):g}"
    except (TypeError, ValueError):
        return str(line).strip()


def opinion_group(game_id: str, bet_type: str, side: str) -> str:
    """One directional opinion on one game — the unit for win/ROI calibration."""
    return f"{game_id}|{bet_type}|{side}"


def bet_key(game_id: str, bet_type: str, side: str, line) -> str:
    """
    One tracked bet — the unit for CLV.

    Moneyline has no handicap, so its line is empty and there is exactly one row
    per game+side. Price movement on an ML is the CLV signal itself; keying on
    price would spawn a row for every few-cent wiggle.
    """
    return f"{opinion_group(game_id, bet_type, side)}|{_fmt_line(line)}"


# ── CLV maths ─────────────────────────────────────────────────────────────────
def clv_line_points(bet_type: str, side: str, entry_line, closing_line) -> float | None:
    """
    Line movement in POINTS, signed so POSITIVE always means we hold the better
    (easier to win) number.

    The two directions take OPPOSITE signs of the same delta, which is exactly
    what was inverted in the MLB model — it read +18 when the truth was -0.15:

        Over  44.5 -> closes 46.5 : we need 45+, market needs 47+  -> +2.0
        Under 44.5 -> closes 42.5 : we need 44-, market needs 42-  -> +2.0
        Lions -3.5 -> closes -5.5 : we need by 4, market by 6      -> +2.0
        Lions +3.5 -> closes +5.5 : market gets the cushion        -> -2.0

    Spreads use entry-minus-closing, which is correct for favourite and
    underdog alike without a special case.
    """
    try:
        e, c = float(entry_line), float(closing_line)
    except (TypeError, ValueError):
        return None

    s = (side or "").strip().lower()
    if bet_type in ("Game Total", "Team Total"):
        if s.endswith("over"):
            return round(c - e, 2)
        if s.endswith("under"):
            return round(e - c, 2)
        return None
    if bet_type == "Spread":
        return round(e - c, 2)
    return None  # Moneyline has no line to move


def clv_price_pct(entry_price, closing_price) -> float | None:
    """
    Price movement in PERCENTAGE POINTS of implied probability, signed so
    positive means we got the better price.

    Comparing American odds directly would be wrong — they are non-linear, so
    -110 to -105 and +200 to +205 are not comparable moves. Converting both to
    implied probability makes them so. If the market's implied probability
    closed ABOVE what we paid for, we bought it cheap.
    """
    try:
        e = edges.american_to_implied(float(entry_price))
        c = edges.american_to_implied(float(closing_price))
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return round((c - e) * 100, 2)


# ── Sheet helpers ─────────────────────────────────────────────────────────────
def _tab(gc, name: str, header: list[str]):
    """
    Open a tab and GUARANTEE its header row exists.

    edges.ws() only writes a header when it has to create the worksheet, so a
    tab that already exists but was emptied (a .clear(), a manual wipe) comes
    back headerless — and append_rows() then writes data straight into row 1.
    Every later sheet_to_dicts() would silently parse the first DATA row as the
    column names. Hit exactly this on Line Log and Projection Log in testing.
    """
    w = edges.ws(gc, edges.NFL_SHEET_ID, name, header=header)
    first = w.row_values(1)
    if not first or first[:len(header)] != header:
        if not first:
            w.update([header], value_input_option="RAW")
        else:
            w.insert_rows([header], row=1, value_input_option="RAW")
    return w


def _pin_numeric_formats(worksheet, header: list[str], cols: list[str]) -> None:
    """
    Pin whole columns to plain number format. Must run on every write: inserted
    rows inherit neighbouring formats, so a column left unpinned drifts and then
    parses wrong later.
    """
    reqs = []
    for name in cols:
        if name not in header:
            continue
        idx = header.index(name)
        reqs.append({
            "repeatCell": {
                "range": {"sheetId": worksheet.id, "startColumnIndex": idx,
                          "endColumnIndex": idx + 1, "startRowIndex": 1},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER",
                                                               "pattern": "0.####"}}},
                "fields": "userEnteredFormat.numberFormat",
            }
        })
    if reqs:
        try:
            worksheet.spreadsheet.batch_update({"requests": reqs})
        except Exception as e:
            print(f"  [warn] could not pin number formats: {e}")


def _num(v, default=None):
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ── Bet History upsert ────────────────────────────────────────────────────────
def upsert_bet_history(gc, candidates: list[dict]) -> dict:
    """
    Insert first-time bets, update ones already tracked. Never duplicates,
    never rewrites a frozen entry value.

    `candidates` is a list of dicts, one per qualifying bet this run, each with:
        game_id, game, kickoff_et, bet_type, side, bet_on,
        line, price, book, stars, units, projection, edge, edge_pct

    Returns counts for the run summary.
    """
    ws = _tab(gc, BET_HISTORY_TAB, BET_HISTORY_HEADER)
    existing = ws.get_all_values(
        value_render_option=gspread.utils.ValueRenderOption.unformatted)

    if not existing or not existing[0] or existing[0][0] != "Bet Key":
        header = BET_HISTORY_HEADER
        rows = []
    else:
        header = existing[0]
        rows = [list(r) + [""] * (len(header) - len(r)) for r in existing[1:]]

    ix = {h: i for i, h in enumerate(header)}
    by_key = {r[ix["Bet Key"]]: r for r in rows if r and r[ix["Bet Key"]]}
    groups_seen = {r[ix["Opinion Group"]] for r in rows if r and r[ix["Opinion Group"]]}

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    run_at = now.strftime("%H:%M")

    added = updated = 0
    for c in candidates:
        if c.get("stars", 0) < MIN_STARS_TO_TRACK:
            continue
        grp = opinion_group(c["game_id"], c["bet_type"], c["side"])
        key = bet_key(c["game_id"], c["bet_type"], c["side"], c.get("line"))

        if key in by_key:
            # Already tracked. Refresh only the "current" fields — entry values
            # stay frozen, which is the whole point.
            r = by_key[key]
            # Count one qualification per DAY, not per run — the morning run
            # plus game-day snapshots would otherwise inflate it. Read the
            # stored date BEFORE overwriting it.
            prev_seen = str(r[ix["Last Seen"]]).strip()
            prev_count = _num(r[ix["Times Qualified"]], 0) or 0
            if prev_seen != today:
                r[ix["Times Qualified"]] = prev_count + 1
            r[ix["Last Seen"]] = today
            r[ix["Current Line"]] = _num(c.get("line"), "")
            r[ix["Current Price"]] = _num(c.get("price"), "")
            r[ix["Current Stars"]] = c.get("stars", "")
            updated += 1
            continue

        is_primary = "FALSE" if grp in groups_seen else "TRUE"
        groups_seen.add(grp)
        new = [""] * len(header)

        def put(col, val):
            if col in ix:
                new[ix[col]] = val

        put("Bet Key", key)
        put("Opinion Group", grp)
        put("Is Primary", is_primary)
        put("Entry Date", today)
        put("Entry Run", run_at)
        put("Game", c.get("game", ""))
        put("Kickoff (ET)", c.get("kickoff_et", ""))
        put("Bet Type", c["bet_type"])
        put("Side", c["side"])
        put("Bet On", c.get("bet_on", ""))
        put("Entry Line", _num(c.get("line"), ""))
        put("Entry Consensus", _num(c.get("consensus_line"), ""))
        put("Books At Line", c.get("books_at_line", ""))
        put("Entry Price", _num(c.get("price"), ""))
        put("Entry Book", c.get("book", ""))
        put("Entry Stars", c.get("stars", ""))
        put("Entry Units", _num(c.get("units"), ""))
        put("Entry Projection", _num(c.get("projection"), ""))
        put("Entry Edge", _num(c.get("edge"), ""))
        put("Entry Edge %", _num(c.get("edge_pct"), ""))
        put("Last Seen", today)
        put("Times Qualified", 1)
        put("Current Line", _num(c.get("line"), ""))
        put("Current Price", _num(c.get("price"), ""))
        put("Current Stars", c.get("stars", ""))
        rows.append(new)
        by_key[key] = new
        added += 1

    # Newest first, so the tab opens on what just happened. str() coercion is
    # defensive: mixing a stored value with an in-memory one must never raise.
    rows.sort(key=lambda r: (str(r[ix["Entry Date"]]), str(r[ix["Entry Run"]])),
              reverse=True)

    ws.clear()
    # RAW, not USER_ENTERED. With USER_ENTERED, Sheets parses "2026-08-12" into
    # a DATE and an unformatted read then hands back a serial number, so every
    # later `stored_date == today` comparison silently fails — which is how the
    # "already counted today" guard below would have quietly stopped working.
    # RAW keeps text as text; numerics are already real floats via _num().
    ws.update([header] + rows, value_input_option="RAW")
    _pin_numeric_formats(ws, header, BET_HISTORY_NUMERIC_COLS)

    return {"added": added, "updated": updated, "total": len(rows)}


# ── Line log ──────────────────────────────────────────────────────────────────
def append_line_log(gc, games_by_id: dict, tracked_keys: set | None = None) -> int:
    """
    Append every DISTINCT LINE currently on the market for every not-yet-started
    game, with the best price at that line.

    Recording per-line rather than per-best-line is deliberate. MLB's CLV lookup
    held only the best line to bet, which for an Under means the HIGHEST number —
    so an Under 4.5 got priced against a book's alternate 5.5 and recorded a fake
    one-run move while four books still showed 4.5 unchanged.

    Runs across ALL games, not just qualifying ones. A bet that qualified Tuesday
    still needs its line tracked on Thursday when it no longer qualifies —
    filtering here is what left three of MLB's four bet types with no CLV at all.
    """
    ws = _tab(gc, LINE_LOG_TAB, LINE_LOG_HEADER)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    now_utc = datetime.now(timezone.utc)

    out = []
    for game_id, g in games_by_id.items():
        try:
            kickoff = datetime.fromisoformat(str(g.get("commence_time", "")).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            kickoff = None
        if kickoff and kickoff <= now_utc:
            continue  # already started; its closing line is whatever we last saw

        label = f"{g['away_team']} @ {g['home_team']}"
        kick_et = edges._fmt_time_et(g.get("commence_time", ""))

        def emit(bet_type, side, line, quotes):
            """quotes: {book: price} at this exact line."""
            if not quotes:
                return
            best_book, best_price = max(quotes.items(), key=lambda kv: kv[1])
            out.append([stamp, game_id, label, kick_et, bet_type, side,
                        _fmt_line(line), best_price, best_book, len(quotes)])

        # Totals — group books by the exact line they offer
        for direction, price_key in (("Over", "over_price"), ("Under", "under_price")):
            by_line = {}
            for book, v in g.get("totals", {}).items():
                if v.get("point") is None or v.get(price_key) is None:
                    continue
                by_line.setdefault(_fmt_line(v["point"]), {})[book] = v[price_key]
            for line, quotes in by_line.items():
                emit("Game Total", direction, line, quotes)

        # Spreads — one side per team, grouped by that team's number
        for side_key, team in (("home", g["home_team"]), ("away", g["away_team"])):
            by_line = {}
            for book, v in g.get("spreads", {}).items():
                pt, pr = v.get(f"{side_key}_point"), v.get(f"{side_key}_price")
                if pt is None or pr is None:
                    continue
                by_line.setdefault(_fmt_line(pt), {})[book] = pr
            for line, quotes in by_line.items():
                emit("Spread", team, line, quotes)

        # Moneyline — no handicap, so a single empty line per side
        for side_key, team in (("home", g["home_team"]), ("away", g["away_team"])):
            quotes = {b: v[f"{side_key}_price"] for b, v in g.get("h2h", {}).items()
                      if v.get(f"{side_key}_price") is not None}
            emit("Moneyline", team, "", quotes)

        # Team totals
        for side_key, team in (("home", g["home_team"]), ("away", g["away_team"])):
            for direction in ("over", "under"):
                by_line = {}
                for book, v in g.get("team_totals", {}).items():
                    pt = v.get(f"{side_key}_point")
                    pr = v.get(f"{side_key}_{direction}_price")
                    if pt is None or pr is None:
                        continue
                    by_line.setdefault(_fmt_line(pt), {})[book] = pr
                for line, quotes in by_line.items():
                    emit("Team Total", f"{team} {direction.title()}", line, quotes)

    if out:
        # RAW, not USER_ENTERED — Sheets parses "2026-08-12 11:00" into a
        # datetime and stores a SERIAL (46246.463...), so the snapshot stamp
        # comes back unparseable. Closing-line capture works by finding the
        # last snapshot before kickoff, so a corrupted stamp breaks CLV at the
        # source. Same failure Bet History hit; fixed there, missed here.
        ws.append_rows(out, value_input_option="RAW")
    return len(out)


# ── Projection log ────────────────────────────────────────────────────────────
def append_projection_log(gc, entries: list[dict]) -> int:
    """
    Log EVERY game's projection against the market each run — including the ones
    that never come close to qualifying.

    This is what makes the star thresholds testable. Without it you can only ever
    see bets that already cleared the bar, so you can never ask "would my
    2.8-point edges have won too?" — and thresholds get tuned on the survivors.
    """
    ws = _tab(gc, PROJECTION_LOG_TAB, PROJECTION_LOG_HEADER)
    now = datetime.now()
    today, run_at = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")

    rows = [[today, run_at, e.get("game_id", ""), e.get("game", ""),
             e.get("kickoff_et", ""), e.get("bet_type", ""), e.get("side", ""),
             _num(e.get("projection"), ""), _num(e.get("consensus_line"), ""),
             _num(e.get("edge"), ""), _num(e.get("edge_pct"), ""),
             e.get("stars", 0), _num(e.get("units"), ""),
             "TRUE" if e.get("qualified") else "FALSE"]
            for e in entries]
    if rows:
        ws.append_rows(rows, value_input_option="RAW")  # keep dates as text — see above
    return len(rows)
