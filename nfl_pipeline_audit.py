"""
nfl_pipeline_audit.py — Health check for the NFL pipeline, written to the SHEET.

WHY THE SHEET AND NOT LOGS: the owner does not and cannot log into GitHub, so
anything meant to alert them has to land somewhere they already look. A failure
that only exists in an Actions log is a failure nobody sees.

Two design rules carried over from the MLB audit:

  * A status that always says NEEDS ATTENTION is one nobody reads. Known-benign
    conditions go in ACCEPTED and are reported as notes, not problems.

  * STALE TIMESTAMPS ARE THE MOST IMPORTANT CHECK. If "last checked" is not
    today, the run did not finish — and a pipeline that silently stops looks
    exactly like a pipeline with nothing to report.
"""

import os
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv

import nfl_analyze_edges as edges
import nfl_bet_tracking as tracking

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

HEALTH_TAB = "Pipeline Health"
HEALTH_HEADER = ["Checked", "Status", "Check", "Finding", "Detail"]

# Conditions that are expected right now and must NOT turn the status red.
# Each entry is (check_name, reason_it_is_fine). Prune as the season starts.
ACCEPTED = {
    "Games awaiting kickoff": "normal before a slate is played",
    "Bets awaiting final score": "normal before a slate is played",
    "No team totals": "books post team totals close to kickoff, not weeks out",
    "No weather yet": "NWS only forecasts ~7 days out",
    "WR-CB matchup PDF": "only needed once the regular season starts",
    "Prop tracking gate": "deliberately gated until divisors are recalibrated pre-Week-1",
}


def _check(results, name, ok, finding, detail=""):
    status = "OK" if ok else ("NOTE" if name in ACCEPTED else "PROBLEM")
    if not ok and name in ACCEPTED:
        detail = f"{detail} [accepted: {ACCEPTED[name]}]".strip()
    results.append([status, name, finding, detail])


def audit(gc) -> list:
    results = []
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    now_utc = datetime.now(timezone.utc)
    sh = gc.open_by_key(edges.NFL_SHEET_ID)

    # ── Freshness: did today's run actually finish? ───────────────────────────
    try:
        log = edges.sheet_to_dicts(sh.worksheet(tracking.LINE_LOG_TAB))
        stamps = [tracking._parse_utc(r.get("Snapshot")) for r in log]
        stamps = [s for s in stamps if s]
        last = max(stamps) if stamps else None
        if last is None:
            _check(results, "Pipeline freshness", False, "no snapshot ever recorded")
        else:
            age_h = (now_utc - last).total_seconds() / 3600
            _check(results, "Pipeline freshness", age_h < 26,
                   f"last snapshot {age_h:.1f}h ago",
                   last.strftime("%Y-%m-%d %H:%M UTC"))
    except Exception as e:
        _check(results, "Pipeline freshness", False, "could not read Line Log", str(e))

    # ── Odds tab: present, one week, not stale ───────────────────────────────
    try:
        odds = edges.sheet_to_dicts(sh.worksheet("NFL Odds"))
        games = {(r["away_team"], r["home_team"]) for r in odds if r.get("home_team")}
        kicks = [tracking._parse_utc(r.get("commence_time")) for r in odds]
        kicks = [k for k in kicks if k]
        future = [k for k in kicks if k > now_utc]
        _check(results, "Odds loaded", bool(games), f"{len(games)} game(s) on the board")
        _check(results, "Odds are for upcoming games", bool(future),
               f"{len(future)}/{len(kicks)} quotes are for future kickoffs",
               "if 0, the fetch is holding a finished week")
        tt = [r for r in odds if r.get("market_key") == "team_totals"]
        _check(results, "No team totals", bool(tt), f"{len(tt)} team-total quote(s)")
    except Exception as e:
        _check(results, "Odds loaded", False, "could not read NFL Odds", str(e))

    # ── Bet History integrity ────────────────────────────────────────────────
    try:
        bh = edges.sheet_to_dicts(sh.worksheet(tracking.BET_HISTORY_TAB))
        _check(results, "Bets tracked", bool(bh), f"{len(bh)} tracked bet(s)")

        keys = [r.get("Bet Key") for r in bh]
        dupes = len(keys) - len(set(keys))
        _check(results, "No duplicate bets", dupes == 0,
               "no duplicates" if dupes == 0 else f"{dupes} duplicate Bet Key(s)",
               "the upsert should make duplicates impossible")

        missing_kick = [r for r in bh if not str(r.get("Kickoff UTC", "")).strip()]
        _check(results, "Kickoff timestamps present", not missing_kick,
               f"{len(missing_kick)} row(s) missing Kickoff UTC",
               "without it a bet can never be captured for CLV")

        past = [r for r in bh
                if (k := tracking._parse_utc(r.get("Kickoff UTC"))) and k < now_utc]
        uncaptured = [r for r in past if not str(r.get("Closing Captured", "")).strip()]
        _check(results, "Closing lines captured", not uncaptured,
               f"{len(uncaptured)} played game(s) with no closing line",
               "CLV cannot be computed for these")

        ungraded = [r for r in past if not str(r.get("Result", "")).strip()]
        _check(results, "Bets awaiting final score", not ungraded,
               f"{len(ungraded)} played bet(s) ungraded")

        awaiting = [r for r in bh
                    if (k := tracking._parse_utc(r.get("Kickoff UTC"))) and k >= now_utc]
        _check(results, "Games awaiting kickoff", not awaiting,
               f"{len(awaiting)} bet(s) on games not yet played")
    except Exception as e:
        _check(results, "Bets tracked", False, "could not read Bet History", str(e))

    # ── Credits ──────────────────────────────────────────────────────────────
    try:
        key = os.environ.get("ODDS_API_KEY") or os.environ.get("ODDS_API_KEY_NFL")
        r = requests.get("https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events",
                         params={"apiKey": key, "dateFormat": "iso"}, timeout=20)
        used = r.headers.get("x-requests-used")
        remain = r.headers.get("x-requests-remaining")
        # /events is free, so this check costs nothing to run.
        ok = remain is not None and int(remain) > 1500
        _check(results, "API credits", ok, f"{remain} remaining ({used} used)",
               "NFL needs ~19/run lines-only, ~115 with props")
    except Exception as e:
        _check(results, "API credits", False, "could not read credit headers", str(e))

    # ── Props readiness (not yet enabled, so informational) ──────────────────
    import nfl_props_data as props_data
    have_pdf = os.path.exists(props_data.WR_CB_PDF_PATH)
    _check(results, "WR-CB matchup PDF", have_pdf,
           "present" if have_pdf else "not uploaded",
           "weekly manual upload; overwrite nfl_wr_cb_matchup_current.pdf")

    # Props were approved by the owner 2026-08-30, so ENABLED is now the
    # expected state — flagging it as a problem would be crying wolf, and a
    # status that is always red is one nobody reads.
    import nfl_fetch_odds as fetch
    import nfl_props_model as props_model
    _check(results, "Player props fetch", fetch.FETCH_PLAYER_PROPS,
           "enabled (expected)" if fetch.FETCH_PLAYER_PROPS else "DISABLED",
           "FETCH_PLAYER_PROPS in nfl_fetch_odds.py")
    _check(results, "Prop tracking gate", props_model.PROPS_TRACKING_ENABLED,
           "props write to Bet History" if props_model.PROPS_TRACKING_ENABLED
           else "GATED — props analysed but not tracked",
           "pending per-stat divisor recalibration off all 16 games' prop menus")

    return [[today + " " + now.strftime("%H:%M")] + row for row in results]


def write_health(gc, rows) -> str:
    problems = [r for r in rows if r[1] == "PROBLEM"]
    status = "NEEDS ATTENTION" if problems else "OK"
    banner = [rows[0][0] if rows else "", status,
              "OVERALL", f"{len(problems)} problem(s), {len(rows)} check(s)",
              "; ".join(r[2] for r in problems) if problems else "all clear"]
    w = tracking._tab(gc, HEALTH_TAB, HEALTH_HEADER)
    w.clear()
    w.update([HEALTH_HEADER, banner] + rows, value_input_option="RAW")
    return status


def main():
    print("=" * 60)
    print("nfl_pipeline_audit.py — NFL Pipeline Health")
    print("=" * 60)
    gc = edges.get_client()
    rows = audit(gc)
    status = write_health(gc, rows)
    print()
    for r in rows:
        mark = {"OK": "  ok  ", "NOTE": " note ", "PROBLEM": " PROB "}[r[1]]
        print(f"[{mark}] {r[2]:<32} {r[3]}")
    print()
    print(f"OVERALL: {status}  (written to '{HEALTH_TAB}' tab)")


if __name__ == "__main__":
    main()
