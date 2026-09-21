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

import math
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


# ── Model cohorts ────────────────────────────────────────────────────────────
# A bet is only evidence about the model that MADE it. On 2026-09-08 two fixes
# landed within eleven minutes: the prop level calibration moved to runtime
# (the sheet had silently re-levelled and the board had gone 94% Unders), and
# ROLE_MISMATCH_BAND tightened to 0.80-1.35. Bets placed before that came from
# a model we no longer run, and pooling them hides what the current one does —
# measured 2026-09-21, they were -15.8 units of the -25.6 total, and they were
# the entire reason the edge signal looked inverted.
#
# Tagged rather than deleted. Deleting would have destroyed exactly the data
# that made that diagnosis possible, and a record you prune when it looks bad
# is not a record. The headline Performance tab scopes to CURRENT_MODEL; the
# older cohorts stay in Bet History, still graded, still analysable.
#
# Cutoff is the entry RUN, not the date: the 09-08 morning run (11:32) was the
# old model and the 21:48 run that evening was the new one.
MODEL_PREFIX = "pre-fix"        # broken prop calibration, loose role band
MODEL_CALIB = "calib-only"      # calibration fixed, band not yet tightened
CURRENT_MODEL = "current"       # both fixes live — the model we run today


def model_cohort(entry_date: str, entry_run: str) -> str:
    """Which model version produced a bet, from when it was entered."""
    stamp = f"{str(entry_date)[:10]} {str(entry_run)[:5]}"
    if stamp < "2026-09-08 21:48":
        return MODEL_PREFIX
    if stamp < "2026-09-08 21:58":
        return MODEL_CALIB
    return CURRENT_MODEL


BET_HISTORY_HEADER = [
    # identity / grouping
    "Bet Key", "Opinion Group", "Is Primary", "Model",
    # when and what
    "Entry Date", "Entry Run", "Game", "Kickoff (ET)", "Kickoff UTC",
    "Bet Type", "Side", "Bet On",
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
    "Snapshot", "Game ID", "Game", "Kickoff (ET)", "Kickoff UTC",
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

    # Drive off the SIDE, not a hardcoded list of bet types. Every over/under
    # market behaves identically — game totals, team totals, and all five
    # over/under player props ("Drake Maye Under") — and listing bet types
    # meant props silently returned None and got no line CLV at all, which is
    # precisely the "tracked but never measured" failure this file exists to
    # prevent.
    s = (side or "").strip().lower()
    if s.endswith("over"):
        return round(c - e, 2)
    if s.endswith("under"):
        return round(e - c, 2)
    if bet_type == "Spread":
        return round(e - c, 2)
    # Moneyline and Anytime TD have no handicap — price movement is the whole
    # signal for those, carried by clv_price_pct().
    return None


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
def _clean_cell(v):
    """A NaN or infinity cannot be sent to Sheets at all — the JSON encoder
    refuses it and the whole write fails. Blank it instead."""
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return ""
    return v


def safe_rewrite(worksheet, grid: list[list]) -> None:
    """
    Replace a tab's contents WITHOUT ever leaving it empty.

    WHY THIS EXISTS (2026-09-11): every rewrite of Bet History used to be
    `clear()` then `update()`. On the Friday morning run the grader built a row
    holding a NaN, the update raised after the clear had already succeeded, and
    Bet History was left EMPTY — every Week 1 bet entered before Saturday was
    lost, including all bets on the two games already played. A clear-first
    rewrite turns ANY write failure (a NaN, a 429, a network blip, a grid-limit
    error) into total loss of the permanent record.

    Order is now: sanitise -> grow the grid if needed -> overwrite in place from
    A1 -> only THEN blank any leftover rows below. If the overwrite fails, the
    old contents are still there. If the trailing blank fails, the worst case is
    stale rows below the new data, never missing ones.
    """
    grid = [[_clean_cell(c) for c in row] for row in grid]
    n_rows = len(grid)
    n_cols = max((len(r) for r in grid), default=0)
    grid = [r + [""] * (n_cols - len(r)) for r in grid]

    # values.update does not grow the sheet; writing past its edge is an error.
    if n_rows > worksheet.row_count or n_cols > worksheet.col_count:
        worksheet.resize(rows=max(n_rows, worksheet.row_count),
                         cols=max(n_cols, worksheet.col_count))

    worksheet.update(grid, "A1", value_input_option="RAW")

    if worksheet.row_count > n_rows:
        last_col = gspread.utils.rowcol_to_a1(1, max(n_cols, worksheet.col_count))
        last_col = "".join(ch for ch in last_col if ch.isalpha())
        worksheet.batch_clear([f"A{n_rows + 1}:{last_col}{worksheet.row_count}"])


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
    values = w.get_all_values(
        value_render_option=gspread.utils.ValueRenderOption.unformatted)

    if not values or not any(any(str(c).strip() for c in r) for r in values):
        w.update([header], value_input_option="RAW")       # bare tab
        return w

    stored = [str(c) for c in values[0]]
    if stored[:len(header)] == header:
        return w                                            # already correct

    # Header differs => schema changed. MIGRATE by column NAME and rewrite the
    # tab. Do NOT insert the new header above the old one: that shoves the old
    # header down into row 1 as a data row (hit exactly this — a "Bet Key"
    # row appeared in Bet History), and appending against a stale header
    # misaligns every field.
    old_ix = {h: i for i, h in enumerate(stored)}
    migrated = []
    already_new = 0
    for r in values[1:]:
        r = [str(c) for c in r]
        if r and r[0] in ("Bet Key", "Snapshot", "Date"):
            continue                                        # stray header row
        # A row WIDER than the stored header was already written in the new
        # shape — appended after the code changed but before this migration
        # ran. Remapping it by old-header NAME shifts every field past the
        # inserted column. That corrupted 704 Line Log rows on 2026-08-30
        # (kickoff timestamps landed in "Bet Type"), so treat width as the
        # signal and map those positionally against the NEW header instead.
        if len(r) > len(stored):
            already_new += 1
            migrated.append((r + [""] * len(header))[:len(header)])
            continue
        r = r + [""] * (len(stored) - len(r))
        migrated.append([r[old_ix[h]] if h in old_ix else "" for h in header])
    if already_new:
        print(f"  [{name}] {already_new} row(s) were already in the new shape "
              f"— mapped positionally, not remapped by name")
    added = [h for h in header if h not in old_ix]
    print(f"  [{name}] schema migrated: +{added or 'none'} ({len(migrated)} rows remapped)")
    safe_rewrite(w, [header] + migrated)
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
    elif existing[0] != BET_HISTORY_HEADER:
        # SCHEMA MIGRATION. Remap by COLUMN NAME, never by position — the
        # schema will keep evolving across the season, and blindly padding
        # rows to a new width silently shifts every value right of the
        # inserted column into the wrong field.
        old = existing[0]
        old_ix = {h: i for i, h in enumerate(old)}
        header = BET_HISTORY_HEADER
        rows = []
        for r in existing[1:]:
            r = list(r) + [""] * (len(old) - len(r))
            rows.append([r[old_ix[h]] if h in old_ix else "" for h in header])
        added = [h for h in header if h not in old_ix]
        dropped = [h for h in old if h not in header]
        print(f"  Bet History schema migrated: +{added or 'none'} -{dropped or 'none'}")
    else:
        header = existing[0]
        rows = [list(r) + [""] * (len(header) - len(r)) for r in existing[1:]]

    ix = {h: i for i, h in enumerate(header)}

    # Backfill the cohort tag on any row that predates the column. A schema
    # migration creates it EMPTY and entry fields are frozen, so nothing else
    # would ever fill it — the same trap that left Kickoff UTC blank forever
    # on faded bets. Self-heals on every run.
    if "Model" in ix:
        for r in rows:
            if not str(r[ix["Model"]]).strip():
                r[ix["Model"]] = model_cohort(r[ix["Entry Date"]], r[ix["Entry Run"]])

    by_key = {r[ix["Bet Key"]]: r for r in rows if r and r[ix["Bet Key"]]}
    groups_seen = {r[ix["Opinion Group"]] for r in rows if r and r[ix["Opinion Group"]]}

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    run_at = now.strftime("%H:%M")

    added = updated = skipped_started = 0
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
            # Backfill fields added by a later schema change. A migration
            # creates them EMPTY, and entry fields are frozen so nothing else
            # would ever populate them — leaving Kickoff UTC blank forever,
            # which silently makes the row uncapturable for CLV.
            for col, val in (("Kickoff UTC", c.get("kickoff_utc", "")),
                             ("Kickoff (ET)", c.get("kickoff_et", ""))):
                if col in ix and not str(r[ix[col]]).strip() and val:
                    r[ix[col]] = val
            updated += 1
            continue

        # NEVER OPEN A NEW BET ON A GAME THAT HAS ALREADY KICKED OFF.
        # Found 2026-09-21: a run against a stale odds tab created 30 bets on
        # games played the day before. They are unbettable by definition, they
        # can never have a closing line (no snapshot exists between entry and a
        # kickoff already past), and grading them would record results for bets
        # nobody could have placed. The scheduled runs normally fire before the
        # day's games, which is the only reason this had not bitten yet — a
        # late Monday run would do exactly the same thing.
        #
        # Only NEW rows are blocked. Existing bets keep refreshing normally,
        # and a missing or unparseable kickoff fails OPEN so a timestamp gap
        # can never silently stop tracking.
        ko = _parse_utc(c.get("kickoff_utc"))
        if ko and ko < datetime.now(timezone.utc):
            skipped_started += 1
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
        put("Model", CURRENT_MODEL)
        put("Entry Date", today)
        put("Entry Run", run_at)
        put("Game", c.get("game", ""))
        put("Kickoff (ET)", c.get("kickoff_et", ""))
        put("Kickoff UTC", c.get("kickoff_utc", ""))
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

    # RAW, not USER_ENTERED (safe_rewrite always writes RAW). With USER_ENTERED,
    # Sheets parses "2026-08-12" into a DATE and an unformatted read then hands
    # back a serial number, so every later `stored_date == today` comparison
    # silently fails — which is how the "already counted today" guard below
    # would have quietly stopped working. RAW keeps text as text; numerics are
    # already real floats via _num(). Never clear-then-write: see safe_rewrite.
    safe_rewrite(ws, [header] + rows)
    _pin_numeric_formats(ws, header, BET_HISTORY_NUMERIC_COLS)

    if skipped_started:
        print(f"  [guard] skipped {skipped_started} new bet(s) on games already "
              f"kicked off — stale odds tab")
    return {"added": added, "updated": updated, "total": len(rows),
            "skipped_started": skipped_started}


# ── Line log ──────────────────────────────────────────────────────────────────
def _prop_log_rows(prop_rows: list[dict], games_by_id: dict, stamp: str,
                   now_utc) -> list[list]:
    """
    Line Log rows for player props.

    Sides MUST match exactly what analyze_player_props() writes into Bet
    History ("Josh Allen Over" for O/U, bare "Josh Allen" for anytime TD),
    otherwise closing capture looks up a key that does not exist and every
    prop silently gets no CLV — the same class of failure as MLB's CLV bug #1.
    """
    import nfl_props_model as props_model

    by_key = {}
    for r in prop_rows:
        prop = props_model.MARKET_TO_PROP.get(str(r.get("market_key")))
        if not prop:
            continue
        player = str(r.get("player", "")).strip()
        if not player or props_model._is_team_entry(player):
            continue
        gid = str(r.get("game_id"))
        g = games_by_id.get(gid)
        if not g:
            continue
        try:
            kickoff = datetime.fromisoformat(str(g.get("commence_time", "")).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            kickoff = None
        if kickoff and kickoff <= now_utc:
            continue
        price = _num(r.get("price"))
        if price is None:
            continue
        direction = str(r.get("direction", "")).strip()
        line = _num(r.get("point"))
        side = player if direction == "Yes" else f"{player} {direction}"
        key = (gid, props_model.PROP_LABEL[prop], side, _fmt_line(line))
        by_key.setdefault(key, {})[str(r.get("sportsbook"))] = price

    out = []
    for (gid, bet_type, side, line), quotes in by_key.items():
        g = games_by_id[gid]
        best_book, best_price = max(quotes.items(), key=lambda kv: kv[1])
        out.append([stamp, gid, f"{g['away_team']} @ {g['home_team']}",
                    edges._fmt_time_et(g.get("commence_time", "")),
                    str(g.get("commence_time", "")),
                    bet_type, side, line, best_price, best_book, len(quotes)])
    return out


def append_line_log(gc, games_by_id: dict, tracked_keys: set | None = None,
                    prop_rows: list[dict] | None = None) -> int:
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
    # ISO UTC, not local — closing-line capture compares this against a
    # kickoff timestamp, and a naive local stamp cannot be compared safely to a
    # UTC kickoff (an 8:20pm ET Wednesday game is 00:20 UTC Thursday).
    now_utc = datetime.now(timezone.utc)
    stamp = now_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00")

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
        kick_utc = str(g.get("commence_time", ""))

        def emit(bet_type, side, line, quotes):
            """quotes: {book: price} at this exact line."""
            if not quotes:
                return
            best_book, best_price = max(quotes.items(), key=lambda kv: kv[1])
            out.append([stamp, game_id, label, kick_et, kick_utc, bet_type, side,
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

    if prop_rows:
        out.extend(_prop_log_rows(prop_rows, games_by_id, stamp, now_utc))

    if out:
        # RAW, not USER_ENTERED — Sheets parses "2026-08-12 11:00" into a
        # datetime and stores a SERIAL (46246.463...), so the snapshot stamp
        # comes back unparseable. Closing-line capture works by finding the
        # last snapshot before kickoff, so a corrupted stamp breaks CLV at the
        # source. Same failure Bet History hit; fixed there, missed here.
        ws.append_rows(out, value_input_option="RAW")
    return len(out)


# ── Closing line capture + CLV ────────────────────────────────────────────────
def _parse_utc(v):
    """
    Parse a timestamp and ALWAYS return a timezone-aware UTC datetime.

    Older Line Log rows were written as naive local strings ("2026-08-13 16:54")
    before the switch to ISO UTC. Comparing a naive datetime against an aware
    kickoff raises TypeError — which would crash closing-line capture mid-season
    on exactly the historical rows we most need. Naive values are assumed UTC.
    """
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def capture_closing_and_clv(gc) -> dict:
    """
    Fill Closing Line / Closing Price / CLV for any tracked bet whose game has
    kicked off and which hasn't been captured yet.

    THE CLOSING LINE IS THE LAST SNAPSHOT STRICTLY BEFORE KICKOFF. For NFL that
    matters more than in MLB: inactive lists drop at T-90 and move lines
    materially — a QB scratch is worth points, not cents — so the number worth
    measuring against is the one AFTER that news. Snapshot density before
    kickoff is what makes this accurate; this function just takes the latest
    one available.

    Idempotent and backfilling by design: it re-scans every uncaptured row on
    every run, so a game missed once (API hiccup, late run, crash) is picked up
    later instead of being lost forever. That one-shot assumption is what left
    1,033 MLB rows permanently ungraded.
    """
    bh = _tab(gc, BET_HISTORY_TAB, BET_HISTORY_HEADER)
    existing = bh.get_all_values(
        value_render_option=gspread.utils.ValueRenderOption.unformatted)
    if len(existing) < 2:
        return {"captured": 0, "pending": 0, "no_snapshot": 0}

    header = existing[0]
    ix = {h: i for i, h in enumerate(header)}
    rows = [list(r) + [""] * (len(header) - len(r)) for r in existing[1:]]

    ll = _tab(gc, LINE_LOG_TAB, LINE_LOG_HEADER)
    log = ll.get_all_values(
        value_render_option=gspread.utils.ValueRenderOption.unformatted)
    log_rows = [dict(zip(log[0], r)) for r in log[1:]] if len(log) > 1 else []

    # (game_id, bet_type, side) -> [(snapshot_dt, line, price, books)]
    quotes = {}
    for q in log_rows:
        dt = _parse_utc(q.get("Snapshot"))
        if dt is None:
            continue
        key = (str(q.get("Game ID")), str(q.get("Bet Type")), str(q.get("Side")))
        quotes.setdefault(key, []).append(
            (dt, _num(q.get("Line")), _num(q.get("Best Price")), _num(q.get("Books"), 1) or 1))

    # game_id -> kickoff, straight from the Line Log, which records every game
    # every run regardless of whether anything qualified.
    kickoff_by_game = {}
    for q in log_rows:
        k = _parse_utc(q.get("Kickoff UTC"))
        if k:
            kickoff_by_game.setdefault(str(q.get("Game ID")), k)

    now = datetime.now(timezone.utc)
    captured = pending = no_snapshot = backfilled = 0

    for r in rows:
        if str(r[ix["Closing Captured"]]).strip():
            continue
        game_id = str(r[ix["Bet Key"]]).split("|")[0]

        # Self-heal a missing kickoff. The upsert only backfills rows that
        # RE-QUALIFY, so a bet whose edge faded keeps a blank Kickoff UTC and
        # would never be capturable — and a faded bet is exactly the row whose
        # CLV is most informative. Same shape as MLB CLV bug #2 (filtering
        # before recording); the Line Log is filter-free, so use it.
        if not str(r[ix["Kickoff UTC"]]).strip() and game_id in kickoff_by_game:
            r[ix["Kickoff UTC"]] = kickoff_by_game[game_id].strftime("%Y-%m-%dT%H:%M:%S+00:00")
            backfilled += 1

        kickoff = _parse_utc(r[ix["Kickoff UTC"]])
        if kickoff is None or kickoff > now:
            pending += 1
            continue

        key = (game_id, str(r[ix["Bet Type"]]), str(r[ix["Side"]]))
        pre = [q for q in quotes.get(key, []) if q[0] < kickoff]
        if not pre:
            no_snapshot += 1
            continue

        last_ts = max(q[0] for q in pre)
        at_close = [q for q in pre if q[0] == last_ts]

        bet_type = str(r[ix["Bet Type"]])
        entry_line = _num(r[ix["Entry Line"]])
        entry_price = _num(r[ix["Entry Price"]])

        # Closing LINE is a books-weighted consensus. Unlike an ENTRY line it
        # is a reference point, not something we bet, so an average is fine —
        # it is the market's centre of gravity at the close.
        closing_line = None
        if bet_type != "Moneyline":
            num = sum(q[1] * q[3] for q in at_close if q[1] is not None)
            den = sum(q[3] for q in at_close if q[1] is not None)
            if den:
                closing_line = round(num / den, 2)

        # Closing PRICE must be like-for-like: the price at OUR line. If no book
        # still offers that number, price CLV is not comparable and the line
        # movement carries the signal instead.
        if bet_type == "Moneyline":
            closing_price = max((q[2] for q in at_close if q[2] is not None), default=None)
        else:
            same = [q[2] for q in at_close
                    if q[1] is not None and entry_line is not None
                    and abs(q[1] - entry_line) < 1e-9 and q[2] is not None]
            closing_price = max(same) if same else None

        r[ix["Closing Line"]] = closing_line if closing_line is not None else ""
        r[ix["Closing Price"]] = closing_price if closing_price is not None else ""
        r[ix["Closing Captured"]] = last_ts.strftime("%Y-%m-%dT%H:%M:%S+00:00")

        cl = clv_line_points(bet_type, str(r[ix["Side"]]), entry_line, closing_line)
        cp = clv_price_pct(entry_price, closing_price)
        r[ix["CLV Line"]] = cl if cl is not None else ""
        r[ix["CLV Price %"]] = cp if cp is not None else ""
        captured += 1

    if captured or backfilled:
        safe_rewrite(bh, [header] + rows)          # never clear-then-write
        _pin_numeric_formats(bh, header, BET_HISTORY_NUMERIC_COLS)

    return {"captured": captured, "pending": pending,
            "no_snapshot": no_snapshot, "backfilled": backfilled}


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
