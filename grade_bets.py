"""
grade_bets.py — Grade yesterday's bets and rebuild Performance tab.
Run every morning after fetch_odds.py.
"""

import requests
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta, timezone
import os
import time


# ── Rate-limit retry wrapper ──────────────────────────────────────────────────
def sheets_call(fn, *args, retries=5, **kwargs):
    """Call a gspread function, retrying on 429 with exponential backoff."""
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            if "429" in str(e) and attempt < retries - 1:
                wait = 15 * (attempt + 1)
                print(f"  [Rate limit hit — waiting {wait}s before retry {attempt+1}/{retries-1}]")
                time.sleep(wait)
            else:
                raise

# ── Config ────────────────────────────────────────────────────────────────────
ODDS_SHEET_ID = "1RaSm1ogJtNykM7WbYfQ3b9L7MUePcRBqlFMKuQfA_I4"
CREDS_FILE    = os.path.join(os.path.dirname(__file__), "google_credentials.json")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

MLB_STATS_BASE    = "https://statsapi.mlb.com/api/v1/schedule"
MLB_BOXSCORE_BASE = "https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore"


# ── Google helpers ────────────────────────────────────────────────────────────
def auth():
    creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


def get_ws(gc, tab):
    return gc.open_by_key(ODDS_SHEET_ID).worksheet(tab)


# ── MLB Stats API ─────────────────────────────────────────────────────────────
def fetch_scores(date_str: str) -> dict:
    """
    Returns {normalized_game_key: {away_score, home_score, away_team, home_team}}
    date_str format: YYYY-MM-DD
    """
    params = {"sportId": 1, "date": date_str, "gameType": "R"}
    resp   = requests.get(MLB_STATS_BASE, params=params, timeout=30)
    resp.raise_for_status()
    data   = resp.json()

    scores = {}
    for date_block in data.get("dates", []):
        for game in date_block.get("games", []):
            status = game.get("status", {}).get("abstractGameState", "")
            if status != "Final":
                continue
            away_team  = game["teams"]["away"]["team"]["name"]
            home_team  = game["teams"]["home"]["team"]["name"]
            away_score = game["teams"]["away"].get("score")
            home_score = game["teams"]["home"].get("score")

            if away_score is None or home_score is None:
                continue

            key = f"{away_team} @ {home_team}".lower()
            scores[key] = {
                "away_team":  away_team,
                "home_team":  home_team,
                "away_score": int(away_score),
                "home_score": int(home_score),
            }
    return scores


def fetch_player_stats(date_str: str) -> dict:
    """
    Returns per-player stat lines for all completed games on date_str.
    Structure: {normalized_player_name: {"strikeouts": int, "total_bases": int,
                                          "home_runs": int, "h_r_rbi": int}}
    Pulls MLB Stats API boxscore for each game. Names are lowercased+stripped for matching.
    """
    # First get game PKs for the date
    params = {"sportId": 1, "date": date_str, "gameType": "R"}
    resp   = requests.get(MLB_STATS_BASE, params=params, timeout=30)
    resp.raise_for_status()
    data   = resp.json()

    game_pks = []
    for date_block in data.get("dates", []):
        for game in date_block.get("games", []):
            if game.get("status", {}).get("abstractGameState") == "Final":
                game_pks.append(game["gamePk"])

    player_stats = {}
    for pk in game_pks:
        url  = MLB_BOXSCORE_BASE.format(game_pk=pk)
        resp = requests.get(url, timeout=30)
        if resp.status_code != 200:
            continue
        box  = resp.json()

        for side in ("away", "home"):
            players = box.get("teams", {}).get(side, {}).get("players", {})
            for pid, pdata in players.items():
                name = pdata.get("person", {}).get("fullName", "").strip().lower()
                if not name:
                    continue

                # Pitching stats — strikeouts (0 is a valid result, not Pending)
                p_stats     = pdata.get("stats", {}).get("pitching", {})
                has_pitching = bool(p_stats)
                k = p_stats.get("strikeOuts", 0) if has_pitching else None

                # Batting stats (0 is a valid result for all batting props)
                b_stats     = pdata.get("stats", {}).get("batting", {})
                has_batting  = bool(b_stats)
                tb   = b_stats.get("totalBases", 0) if has_batting else None
                hr   = b_stats.get("homeRuns",   0) if has_batting else None
                h    = b_stats.get("hits",  0) if has_batting else 0
                r    = b_stats.get("runs",  0) if has_batting else 0
                rbi  = b_stats.get("rbi",   0) if has_batting else 0
                hrbi = (h + r + rbi) if has_batting else None

                player_stats[name] = {
                    "strikeouts":  k,
                    "total_bases": tb,
                    "home_runs":   hr,
                    "h_r_rbi":     hrbi,
                }

    return player_stats


# ── Bet History grading ───────────────────────────────────────────────────────

def col(header: list, name: str) -> int:
    """Return 0-based index of column by name, -1 if not found."""
    try:
        return header.index(name)
    except ValueError:
        return -1


def grade_history(ws_hist, scores: dict, yesterday: str) -> int:
    all_vals = ws_hist.get_all_values()
    if len(all_vals) < 2:
        return 0

    header  = all_vals[0]
    updates = []
    graded  = 0

    # Locate columns by name — works regardless of column order
    c_date      = col(header, "Date")
    c_game      = col(header, "Game")
    c_dir       = col(header, "Direction")
    c_units     = col(header, "Units Bet")
    c_line      = col(header, "Book Line")
    c_result    = col(header, "Result")
    c_away_sc   = col(header, "Away Score")
    c_home_sc   = col(header, "Home Score")
    c_actual    = col(header, "Actual Total")
    c_units_res = col(header, "Units Result")
    c_bet_type  = col(header, "Bet Type")
    c_bet_on    = col(header, "Bet On")

    for i, row in enumerate(all_vals[1:], start=2):
        while len(row) < len(header):
            row.append("")

        date      = row[c_date]
        game      = row[c_game].lower()
        direction = row[c_dir].upper() if c_dir >= 0 else ""
        units_bet = row[c_units] if c_units >= 0 else ""
        book_line = row[c_line] if c_line >= 0 else ""
        result    = row[c_result] if c_result >= 0 else ""
        bet_type  = row[c_bet_type].strip() if c_bet_type >= 0 else "Game Total"
        bet_on    = row[c_bet_on].strip() if c_bet_on >= 0 else ""

        if date != yesterday or result in {"Win", "Loss", "Push"}:
            continue

        score_data = scores.get(game)
        if not score_data:
            print(f"  [no score] {row[c_game]}")
            continue

        away_s = score_data["away_score"]
        home_s = score_data["home_score"]
        actual = away_s + home_s

        # Parse game label "Away @ Home" to extract team names
        game_label = row[c_game]
        parts      = game_label.split(" @ ")
        game_away  = parts[0].strip() if len(parts) == 2 else ""
        game_home  = parts[1].strip() if len(parts) == 2 else ""

        if bet_type == "Moneyline":
            win  = (bet_on == game_home and home_s > away_s) or \
                   (bet_on == game_away and away_s > home_s)
            push = away_s == home_s
        elif bet_type == "Run Line":
            try:
                spread = float(book_line)
            except (ValueError, TypeError):
                continue
            if game_home and game_home in bet_on:
                margin = home_s - away_s
            else:
                margin = away_s - home_s
            win  = margin + spread > 0
            push = abs(margin + spread) < 0.01
        else:  # Game Total
            try:
                line = float(book_line)
            except (ValueError, TypeError):
                continue
            if direction in ("OVER", "O"):
                win = actual > line
            elif direction in ("UNDER", "U"):
                win = actual < line
            else:
                win = False
            push = abs(actual - line) < 0.01

        result_grade = "Push" if push else ("Win" if win else "Loss")

        try:
            u = float(units_bet)
        except (ValueError, TypeError):
            u = 0.0

        units_result = 0.0 if push else (u if win else -u)

        updates.append({
            "row":          i,
            "away_score":   away_s,
            "home_score":   home_s,
            "actual_total": actual,
            "result":       result_grade,
            "units_result": round(units_result, 2),
            "c_away_sc":    c_away_sc + 1,   # gspread uses 1-based columns
            "c_home_sc":    c_home_sc + 1,
            "c_actual":     c_actual + 1,
            "c_result":     c_result + 1,
            "c_units_res":  c_units_res + 1,
        })
        graded += 1

    # Batch update — one API call per row instead of one per cell
    for u in updates:
        r = u["row"]
        sheets_call(ws_hist.batch_update, [
            {"range": f"R{r}C{u['c_away_sc']}",   "values": [[u["away_score"]]]},
            {"range": f"R{r}C{u['c_home_sc']}",   "values": [[u["home_score"]]]},
            {"range": f"R{r}C{u['c_actual']}",    "values": [[u["actual_total"]]]},
            {"range": f"R{r}C{u['c_result']}",    "values": [[u["result"]]]},
            {"range": f"R{r}C{u['c_units_res']}", "values": [[u["units_result"]]]},
        ], value_input_option="USER_ENTERED")

    return graded, updates, all_vals[0], all_vals[1:]


# ── Shadow tab grading ────────────────────────────────────────────────────────
def grade_shadow(ws_shadow, scores: dict, yesterday: str) -> tuple[int, float]:
    all_vals = ws_shadow.get_all_values()
    if len(all_vals) < 2:
        return 0, 0.0

    # ── Use header-based column lookups — never hardcode indices ─────────────
    header = all_vals[0]
    def sc(name):
        try:    return header.index(name)
        except: return None

    c_date      = sc("Date")
    c_game      = sc("Game")
    c_away      = sc("Away Team")
    c_home      = sc("Home Team")
    c_bet_type  = sc("Bet Type")
    c_bet_team  = sc("Bet Team")
    c_bet_side  = sc("Bet Side")
    c_our_win   = sc("Our Win%")
    c_units     = sc("Units Would Bet")
    c_away_sc   = sc("Away Score")
    c_home_sc   = sc("Home Score")
    c_winner    = sc("Actual Winner")
    c_run_diff  = sc("Actual Run Diff")
    c_fav_won   = sc("Did Favorite Win")
    c_result    = sc("Bet Result")
    c_units_res = sc("Units Result")
    c_outcome   = sc("Actual Outcome")
    c_pred_err  = sc("Prediction Error")
    c_overconf  = sc("Was Overconfident")

    # Verify critical columns exist
    for name, idx in [("Away Score", c_away_sc), ("Bet Result", c_result),
                       ("Units Would Bet", c_units)]:
        if idx is None:
            print(f"  [Shadow grading: missing column '{name}' — skipping]")
            return 0, 0.0

    n_cols = len(header)
    updates     = []
    graded      = 0
    pred_errors = []

    for i, row in enumerate(all_vals[1:], start=2):
        while len(row) < n_cols:
            row.append("")

        date      = row[c_date]  if c_date  is not None else ""
        game      = row[c_game].lower() if c_game is not None else ""
        away_team = row[c_away]  if c_away  is not None else ""
        home_team = row[c_home]  if c_home  is not None else ""
        bet_type  = row[c_bet_type] if c_bet_type is not None else ""
        bet_team  = row[c_bet_team] if c_bet_team is not None else ""
        bet_side  = row[c_bet_side] if c_bet_side is not None else ""
        our_win_s = row[c_our_win].replace("%","").strip() if c_our_win is not None else ""
        units_s   = row[c_units]    if c_units is not None else ""

        # Skip if wrong date or already graded (Bet Result filled = graded)
        if date != yesterday or row[c_result]:
            continue

        score_data = scores.get(game)
        if not score_data:
            continue

        away_s   = score_data["away_score"]
        home_s   = score_data["home_score"]
        run_diff = home_s - away_s
        winner   = home_team if run_diff > 0 else (away_team if run_diff < 0 else "Tie")
        fav_won  = "Yes" if winner == home_team else "No"

        try:    our_win = float(our_win_s) / 100
        except: our_win = 0.5

        if bet_type == "Moneyline":
            bet_win = (winner == bet_team)
        elif bet_type == "Run Line":
            bet_win = (run_diff > 1.5) if bet_side == "Home" else (run_diff < -1.5)
        else:
            bet_win = False

        result = "Win" if bet_win else "Loss"

        try:    u = float(units_s)
        except: u = 0.0
        units_result = round(u if bet_win else -u, 2)

        actual_win    = 1.0 if bet_win else 0.0
        pred_error    = abs(our_win - actual_win)
        overconfident = "Yes" if our_win > 0.60 and not bet_win else "No"
        pred_errors.append(pred_error)

        updates.append({
            "row": i,
            "away_score":    away_s,    "c_away_sc":   c_away_sc  + 1,
            "home_score":    home_s,    "c_home_sc":   c_home_sc  + 1,
            "actual_winner": winner,    "c_winner":    c_winner   + 1,
            "run_diff":      run_diff,  "c_run_diff":  c_run_diff + 1,
            "fav_won":       fav_won,   "c_fav_won":   c_fav_won  + 1,
            "result":        result,    "c_result":    c_result   + 1,
            "units_result":  units_result, "c_units_res": c_units_res + 1,
            "actual_outcome": f"{away_team} {away_s}, {home_team} {home_s}",
            "c_outcome":     c_outcome  + 1,
            "pred_error":    round(pred_error, 2), "c_pred_err": c_pred_err + 1,
            "overconfident": overconfident, "c_overconf": c_overconf + 1,
        })
        graded += 1

    for u in updates:
        r = u["row"]
        sheets_call(ws_shadow.batch_update, [
            {"range": f"R{r}C{u['c_away_sc']}",   "values": [[u["away_score"]]]},
            {"range": f"R{r}C{u['c_home_sc']}",   "values": [[u["home_score"]]]},
            {"range": f"R{r}C{u['c_winner']}",    "values": [[u["actual_winner"]]]},
            {"range": f"R{r}C{u['c_run_diff']}",  "values": [[u["run_diff"]]]},
            {"range": f"R{r}C{u['c_fav_won']}",   "values": [[u["fav_won"]]]},
            {"range": f"R{r}C{u['c_result']}",    "values": [[u["result"]]]},
            {"range": f"R{r}C{u['c_units_res']}", "values": [[u["units_result"]]]},
            {"range": f"R{r}C{u['c_outcome']}",   "values": [[u["actual_outcome"]]]},
            {"range": f"R{r}C{u['c_pred_err']}",  "values": [[u["pred_error"]]]},
            {"range": f"R{r}C{u['c_overconf']}",  "values": [[u["overconfident"]]]},
        ], value_input_option="USER_ENTERED")

    avg_err = round(sum(pred_errors) / len(pred_errors), 4) if pred_errors else 0.0
    return graded, avg_err


def grade_gt_shadow(ws_gt_shadow, scores: dict, yesterday: str) -> int:
    """Grade yesterday's Game Total Shadow rows using actual scores."""
    all_vals = ws_gt_shadow.get_all_values()
    if len(all_vals) < 2:
        return 0

    header = all_vals[0]
    def sc(name):
        try:    return header.index(name)
        except: return None

    c_date      = sc("Date")
    c_game      = sc("Game")
    c_away      = sc("Away Team")
    c_home      = sc("Home Team")
    c_direction = sc("Direction")
    c_line      = sc("Book Line")
    c_units     = sc("Units Would Bet")
    c_away_sc   = sc("Away Score")
    c_home_sc   = sc("Home Score")
    c_actual    = sc("Actual Total")
    c_result    = sc("Result")
    c_units_res = sc("Units Result")

    for name, idx in [("Away Score", c_away_sc), ("Result", c_result),
                       ("Book Line", c_line)]:
        if idx is None:
            print(f"  [GT Shadow grading: missing column '{name}' — skipping]")
            return 0

    n_cols  = len(header)
    updates = []
    graded  = 0

    for i, row in enumerate(all_vals[1:], start=2):
        while len(row) < n_cols:
            row.append("")

        date = row[c_date] if c_date is not None else ""
        if date != yesterday or row[c_away_sc]:
            continue

        game_label = row[c_game] if c_game is not None else ""
        game_key   = game_label.lower()
        score_data = scores.get(game_key)
        if not score_data:
            continue

        away_s  = score_data["away_score"]
        home_s  = score_data["home_score"]
        actual  = away_s + home_s
        direction = row[c_direction] if c_direction is not None else ""
        try:    line = float(row[c_line])
        except: continue

        if actual > line:
            outcome_dir = "Over"
        elif actual < line:
            outcome_dir = "Under"
        else:
            outcome_dir = "Push"

        if outcome_dir == "Push":
            result = "Push"
            units_result = 0.0
        elif outcome_dir.lower() == direction.strip().lower():
            result = "Win"
        else:
            result = "Loss"

        try:    u = float(row[c_units])
        except: u = 0.0
        if result == "Win":
            units_result = round(u, 2)
        elif result == "Loss":
            units_result = round(-u, 2)
        else:
            units_result = 0.0

        cells = [
            {"range": f"R{i}C{c_away_sc + 1}",   "values": [[away_s]]},
            {"range": f"R{i}C{c_home_sc + 1}",   "values": [[home_s]]},
            {"range": f"R{i}C{c_actual + 1}",    "values": [[actual]]},
            {"range": f"R{i}C{c_result + 1}",    "values": [[result]]},
            {"range": f"R{i}C{c_units_res + 1}", "values": [[units_result]]},
        ]
        sheets_call(ws_gt_shadow.batch_update, cells, value_input_option="USER_ENTERED")
        graded += 1

    return graded


def parse_stars(val: str) -> int:
    """Convert star value to int — handles both '3' and emoji '⭐⭐⭐'."""
    val = str(val).strip()
    if val.isdigit():
        return int(val)
    # Count star emoji characters
    count = val.count("⭐")
    if count:
        return count
    # Try stripping non-digits
    digits = "".join(c for c in val if c.isdigit())
    return int(digits) if digits else 0


# ── Performance tab rebuild ───────────────────────────────────────────────────
MODEL_FIX_DATE       = "2026-06-20"  # original ML/RL/GT fix date — kept for ML and RL continuity
MODEL_FIX_DATE_GT    = "2026-07-12"  # park factor full recalibration (1,428-game 2026 dataset) + Under threshold raised to 20%
PROPS_MODEL_START    = "2026-07-10"  # First day props used Poisson probability edge.
PROPS_MODEL_START_BY_TYPE = {
    "Total Bases": "2026-07-12",   # 2026-07-12: pitcher ERA adjustment added; prior data Over-biased on 1.5 lines (43% win rate)
    "H+R+RBI":     "2026-07-12",   # 2026-07-12: pitcher ERA adjustment added; prior data Over-biased on 1.5 lines (39% win rate)
    "Home Run":    "2026-07-12",   # shadow-only, no betting data worth keeping
    # SP Strikeouts uses PROPS_MODEL_START (2026-07-10) — Poisson fix is sufficient
}
# All-time aggregates below are permanently a blend of pre-fix (biased) and
# post-fix (corrected) data — that blend dilutes slowly, so a separate
# "since fix" table is tracked alongside the all-time one so nobody draws
# conclusions from the contaminated combined number while it's still diluting.


def rebuild_performance(gc):
    ws_hist   = get_ws(gc, "Bet History")
    ws_shadow = get_ws(gc, "ML RL")
    try:
        ws_tt = get_ws(gc, "Team Totals")
    except Exception:
        ws_tt = None
    try:
        ws_props = get_ws(gc, "Player Props Shadow")
    except Exception:
        ws_props = None
    ws_perf   = get_ws(gc, "Performance")

    # ── Game Totals (official bets from Bet History) ──────────────────────────
    hist_rows = ws_hist.get_all_values()
    if len(hist_rows) < 2:
        return

    hh = hist_rows[0]
    hdata = hist_rows[1:]
    c_stars     = col(hh, "Stars")
    c_result    = col(hh, "Result")
    c_units_res = col(hh, "Units Result")
    c_date      = col(hh, "Date")

    gt_buckets = {4: [], 5: []}
    gt_buckets_fix = {4: [], 5: []}
    for row in hdata:
        while len(row) < len(hh): row.append("")
        result = row[c_result] if c_result >= 0 else ""
        if result not in ("Win", "Loss", "Push"):
            continue
        try:
            stars = parse_stars(row[c_stars]) if c_stars >= 0 else 0
            units = float(row[c_units_res]) if (c_units_res >= 0 and row[c_units_res]) else 0.0
        except (ValueError, TypeError):
            continue
        if stars in gt_buckets:
            gt_buckets[stars].append((stars, units, result))
            date = row[c_date] if c_date >= 0 else ""
            if date >= MODEL_FIX_DATE_GT:
                gt_buckets_fix[stars].append((stars, units, result))

    # Daily P/L from official bets (4+ stars) — GT, ML, and RL separately
    daily_gt = {}
    daily_ml_off = {}
    daily_rl_off = {}
    c_btype = col(hh, "Bet Type")
    for row in hdata:
        while len(row) < len(hh): row.append("")
        date   = row[c_date] if c_date >= 0 else row[0]
        result = row[c_result] if c_result >= 0 else ""
        if result not in ("Win", "Loss", "Push"):
            continue
        try:
            stars = parse_stars(row[c_stars]) if c_stars >= 0 else 0
            u = float(row[c_units_res]) if (c_units_res >= 0 and row[c_units_res]) else 0.0
        except (ValueError, TypeError):
            continue
        if stars < 4:
            continue
        btype = row[c_btype].strip() if c_btype >= 0 else ""
        if btype == "Game Total":
            daily_gt[date] = daily_gt.get(date, 0.0) + u
        elif btype == "Moneyline":
            daily_ml_off[date] = daily_ml_off.get(date, 0.0) + u
        elif btype == "Run Line":
            daily_rl_off[date] = daily_rl_off.get(date, 0.0) + u

    # ── ML / RL Shadow (excluding Missing SP rows) ────────────────────────────
    sh_rows = ws_shadow.get_all_values()
    sh_hdr  = sh_rows[0] if sh_rows else []
    sh_data = sh_rows[1:] if len(sh_rows) > 1 else []

    sc_date    = col(sh_hdr, "Date")
    sc_type    = col(sh_hdr, "Bet Type")
    sc_stars   = col(sh_hdr, "Stars")
    sc_units   = col(sh_hdr, "Units Would Bet")
    sc_result  = col(sh_hdr, "Bet Result")
    sc_units_r = col(sh_hdr, "Units Result")
    sc_flag    = col(sh_hdr, "Pitcher Flag")
    if sc_result < 0 or sc_units_r < 0:
        print(f"  [WARN] ML RL Shadow missing columns — Bet Result:{sc_result}, Units Result:{sc_units_r}")
        print(f"  [WARN] Actual header: {sh_hdr}")

    # buckets: ml_b[stars] = list of (stars, units, result)
    ml_b = {3: [], 4: [], 5: []}
    rl_b = {3: [], 4: [], 5: []}
    ml_b_fix = {3: [], 4: [], 5: []}
    rl_b_fix = {3: [], 4: [], 5: []}

    for row in sh_data:
        while len(row) < len(sh_hdr): row.append("")
        flag = row[sc_flag].strip() if sc_flag >= 0 else ""
        if flag == "Missing SP":
            continue
        result = row[sc_result].strip() if sc_result >= 0 else ""
        if result not in ("Win", "Loss", "Push"):
            continue
        try:
            stars = parse_stars(row[sc_stars]) if sc_stars >= 0 else 0
            u     = float(row[sc_units_r]) if (sc_units_r >= 0 and row[sc_units_r]) else 0.0
        except (ValueError, TypeError):
            continue
        bet_type = row[sc_type].strip() if sc_type >= 0 else ""
        date     = row[sc_date].strip() if sc_date >= 0 else ""
        if stars not in (3, 4, 5):
            continue
        is_fix = date >= MODEL_FIX_DATE
        if bet_type == "Moneyline":
            ml_b[stars].append((stars, u, result))
            if is_fix:
                ml_b_fix[stars].append((stars, u, result))
        elif bet_type == "Run Line":
            rl_b[stars].append((stars, u, result))
            if is_fix:
                rl_b_fix[stars].append((stars, u, result))

    def summary(bets):
        """Return [Bets, W, L, Push, Win%, Units P/L] for a list of (stars, units, result)."""
        if not bets:
            return ["—", "—", "—", "—", "—", "—"]
        wins   = sum(1 for _, _, r in bets if r == "Win")
        losses = sum(1 for _, _, r in bets if r == "Loss")
        pushes = sum(1 for _, _, r in bets if r == "Push")
        total  = wins + losses + pushes
        roi    = round(sum(u for _, u, _ in bets), 3)
        wp     = f"{wins/(wins+losses)*100:.1f}%" if (wins+losses) > 0 else "—"
        return [total, wins, losses, pushes, wp, roi]

    gt_official = gt_buckets[4] + gt_buckets[5]
    ml_all      = ml_b[3] + ml_b[4] + ml_b[5]
    rl_all      = rl_b[3] + rl_b[4] + rl_b[5]
    combo_b     = {s: ml_b[s] + rl_b[s] for s in (3, 4, 5)}
    combo_all   = ml_all + rl_all

    gt_official_fix = gt_buckets_fix[4] + gt_buckets_fix[5]
    ml_all_fix      = ml_b_fix[3] + ml_b_fix[4] + ml_b_fix[5]
    rl_all_fix      = rl_b_fix[3] + rl_b_fix[4] + rl_b_fix[5]
    combo_b_fix     = {s: ml_b_fix[s] + rl_b_fix[s] for s in (3, 4, 5)}
    combo_all_fix   = ml_all_fix + rl_all_fix

    # ── Team Totals (from Team Totals tab) ───────────────────────────────────
    tt_all_rows = ws_tt.get_all_values() if ws_tt else []
    tt_hdr  = tt_all_rows[0] if tt_all_rows else []
    tt_data = tt_all_rows[1:] if len(tt_all_rows) > 1 else []

    tc_date    = col(tt_hdr, "Date")
    tc_stars   = col(tt_hdr, "Stars")
    tc_result  = col(tt_hdr, "Result")
    tc_units_r = col(tt_hdr, "Units Result")

    tt_b     = {3: [], 4: [], 5: []}
    tt_b_fix = {3: [], 4: [], 5: []}
    daily_tt = {}

    for trow in tt_data:
        while len(trow) < len(tt_hdr): trow.append("")
        result = trow[tc_result].strip() if tc_result >= 0 else ""
        if result not in ("Win", "Loss", "Push"):
            continue
        date = trow[tc_date].strip() if tc_date >= 0 else ""
        try:
            stars = parse_stars(trow[tc_stars]) if tc_stars >= 0 else 0
            u     = float(trow[tc_units_r]) if (tc_units_r >= 0 and trow[tc_units_r]) else 0.0
        except (ValueError, TypeError):
            continue
        if stars not in (3, 4, 5):
            continue
        tt_b[stars].append((stars, u, result))
        daily_tt[date] = daily_tt.get(date, 0.0) + u
        if date >= MODEL_FIX_DATE:
            tt_b_fix[stars].append((stars, u, result))

    tt_all     = tt_b[3] + tt_b[4] + tt_b[5]
    tt_all_fix = tt_b_fix[3] + tt_b_fix[4] + tt_b_fix[5]


    # ── Player Props (SP Strikeouts, Total Bases, Home Run, H+R+RBI) ─────────
    PROP_TYPES = ["SP Strikeouts", "Total Bases", "Home Run", "H+R+RBI"]
    props_all_rows = ws_props.get_all_values() if ws_props else []
    props_hdr  = props_all_rows[0] if props_all_rows else []
    props_data = props_all_rows[1:] if len(props_all_rows) > 1 else []

    pc_date    = col(props_hdr, "Date")
    pc_prop    = col(props_hdr, "Prop Type")
    pc_stars   = col(props_hdr, "Stars")
    pc_result  = col(props_hdr, "Result")
    pc_units_r = col(props_hdr, "Units Result")

    prop_b     = {pt: {3: [], 4: [], 5: []} for pt in PROP_TYPES}
    prop_b_fix = {pt: {3: [], 4: [], 5: []} for pt in PROP_TYPES}
    daily_props = {pt: {} for pt in PROP_TYPES}

    for prow in props_data:
        while len(prow) < len(props_hdr): prow.append("")
        result = prow[pc_result].strip() if pc_result >= 0 else ""
        if result not in ("Win", "Loss", "Push"):
            continue
        pt   = prow[pc_prop].strip() if pc_prop >= 0 else ""
        date = prow[pc_date].strip() if pc_date >= 0 else ""
        if pt not in PROP_TYPES:
            continue
        try:
            stars = parse_stars(prow[pc_stars]) if pc_stars >= 0 else 0
            u     = float(prow[pc_units_r]) if (pc_units_r >= 0 and prow[pc_units_r]) else 0.0
        except (ValueError, TypeError):
            continue
        if stars not in (3, 4, 5):
            continue
        cutoff = PROPS_MODEL_START_BY_TYPE.get(pt, PROPS_MODEL_START)
        if date < cutoff:
            continue  # exclude pre-calibration data for this prop type
        prop_b[pt][stars].append((stars, u, result))
        daily_props[pt][date] = daily_props[pt].get(date, 0.0) + u
        if date >= MODEL_FIX_DATE:
            prop_b_fix[pt][stars].append((stars, u, result))

    # ── Unified side-by-side layout (all bet types across the top) ────────────
    # Each section: [Bets, W, L, Push, Win%, Units P/L] + one gap column
    # Sections: GT | ML | RL | ML+RL | TT | SP K | TB | HR | H+R+RBI
    S   = ["Bets", "W", "L", "Push", "Win%", "Units P/L"]
    GAP = [""]

    def build_sec_hdr(labels):
        out = ["Star Level"]
        for label in labels:
            out += [label] + [""] * 5 + GAP
        return out[:-1]  # drop trailing gap

    def build_col_hdr(n_sections):
        out = ["Star Level"]
        for _ in range(n_sections):
            out += S + GAP
        return out[:-1]

    def build_data_row(label, *bets_lists):
        out = [label]
        for bets in bets_lists:
            out += summary(bets) + GAP
        return out[:-1]

    SECTION_LABELS = [
        "GAME TOTALS (Official)",
        "MONEYLINE SHADOW",
        "RUN LINE SHADOW",
        "ML+RL COMBINED",
        "TEAM TOTALS",
        "SP STRIKEOUTS",
        "TOTAL BASES",
        "HOME RUNS",
        "H+R+RBI",
    ]

    prop_all  = {pt: prop_b[pt][3] + prop_b[pt][4] + prop_b[pt][5] for pt in PROP_TYPES}
    prop_all_fix = {pt: prop_b_fix[pt][3] + prop_b_fix[pt][4] + prop_b_fix[pt][5] for pt in PROP_TYPES}

    def full_row(label, gt, ml, rl, co, tt, *prop_lists):
        return build_data_row(label, gt, ml, rl, co, tt, *prop_lists)

    sec_hdr = build_sec_hdr(SECTION_LABELS)
    col_hdr = build_col_hdr(len(SECTION_LABELS))

    perf_rows = [
        sec_hdr,
        col_hdr,
        full_row("3-Star",  [],             ml_b[3],  rl_b[3],  combo_b[3],  tt_b[3],
                 prop_b["SP Strikeouts"][3], prop_b["Total Bases"][3], prop_b["Home Run"][3], prop_b["H+R+RBI"][3]),
        full_row("4-Star",  gt_buckets[4],  ml_b[4],  rl_b[4],  combo_b[4],  tt_b[4],
                 prop_b["SP Strikeouts"][4], prop_b["Total Bases"][4], prop_b["Home Run"][4], prop_b["H+R+RBI"][4]),
        full_row("5-Star",  gt_buckets[5],  ml_b[5],  rl_b[5],  combo_b[5],  tt_b[5],
                 prop_b["SP Strikeouts"][5], prop_b["Total Bases"][5], prop_b["Home Run"][5], prop_b["H+R+RBI"][5]),
        full_row("All",     gt_official,    ml_all,   rl_all,   combo_all,   tt_all,
                 prop_all["SP Strikeouts"], prop_all["Total Bases"], prop_all["Home Run"], prop_all["H+R+RBI"]),
        [""],
        [f"SINCE MODEL FIX (ML/RL/TT: {MODEL_FIX_DATE}+  |  GT: {MODEL_FIX_DATE_GT}+  |  Props: see type)"],
        sec_hdr,
        col_hdr,
        full_row("3-Star",  [],                 ml_b_fix[3],  rl_b_fix[3],  combo_b_fix[3],  tt_b_fix[3],
                 prop_b_fix["SP Strikeouts"][3], prop_b_fix["Total Bases"][3], prop_b_fix["Home Run"][3], prop_b_fix["H+R+RBI"][3]),
        full_row("4-Star",  gt_buckets_fix[4],  ml_b_fix[4],  rl_b_fix[4],  combo_b_fix[4],  tt_b_fix[4],
                 prop_b_fix["SP Strikeouts"][4], prop_b_fix["Total Bases"][4], prop_b_fix["Home Run"][4], prop_b_fix["H+R+RBI"][4]),
        full_row("5-Star",  gt_buckets_fix[5],  ml_b_fix[5],  rl_b_fix[5],  combo_b_fix[5],  tt_b_fix[5],
                 prop_b_fix["SP Strikeouts"][5], prop_b_fix["Total Bases"][5], prop_b_fix["Home Run"][5], prop_b_fix["H+R+RBI"][5]),
        full_row("All",     gt_official_fix,    ml_all_fix,   rl_all_fix,   combo_all_fix,   tt_all_fix,
                 prop_all_fix["SP Strikeouts"], prop_all_fix["Total Bases"], prop_all_fix["Home Run"], prop_all_fix["H+R+RBI"]),
        [""],
    ]

    # ── Daily P/L table ──────────────────────────────────────────────────────
    # Each P/L value aligned under the "Bets" column of its section (7 cols per section)
    # Sections at offsets: GT=1, ML=8, RL=15, Combined=22, TT=29, SPK=36, TB=43, HR=50, HRR=57
    all_dates = sorted(
        set(list(daily_gt.keys()) + list(daily_ml_off.keys()) + list(daily_rl_off.keys()) +
            list(daily_tt.keys()) + [d for pt in PROP_TYPES for d in daily_props[pt]]),
        reverse=True
    )
    # Header row — P/L label at col B of each section (col index 1, 8, 15, 22, 29, 36, 43, 50, 57)
    pl_hdr = ["Date",
              "GT P/L (4★+)", "", "", "", "", "", "",
              "ML P/L (4★+)", "", "", "", "", "", "",
              "RL P/L (4★+)", "", "", "", "", "", "",
              "ML+RL P/L (4★+)", "", "", "", "", "", "",
              "TT P/L (4★+)", "", "", "", "", "", "",
              "SP K P/L (4★+)", "", "", "", "", "", "",
              "TB P/L (4★+)", "", "", "", "", "", "",
              "HR P/L (4★+)", "", "", "", "", "", "",
              "H+R+RBI P/L (4★+)"]
    perf_rows.append(pl_hdr)

    for d in all_dates:
        gt_pl   = round(daily_gt.get(d, 0.0), 3)
        ml_pl   = round(daily_ml_off.get(d, 0.0), 3)
        rl_pl   = round(daily_rl_off.get(d, 0.0), 3)
        com_pl  = round(ml_pl + rl_pl, 3)
        tt_pl   = round(daily_tt.get(d, 0.0), 3)
        spk_pl  = round(daily_props["SP Strikeouts"].get(d, 0.0), 3)
        tb_pl   = round(daily_props["Total Bases"].get(d, 0.0), 3)
        hr_pl   = round(daily_props["Home Run"].get(d, 0.0), 3)
        hrr_pl  = round(daily_props["H+R+RBI"].get(d, 0.0), 3)
        perf_rows.append([
            d,
            gt_pl,  "", "", "", "", "", "",
            ml_pl,  "", "", "", "", "", "",
            rl_pl,  "", "", "", "", "", "",
            com_pl, "", "", "", "", "", "",
            tt_pl,  "", "", "", "", "", "",
            spk_pl, "", "", "", "", "", "",
            tb_pl,  "", "", "", "", "", "",
            hr_pl,  "", "", "", "", "", "",
            hrr_pl,
        ])

    sheets_call(ws_perf.clear)
    time.sleep(2)
    sheets_call(ws_perf.update, perf_rows, value_input_option="USER_ENTERED")
    print(f"Performance tab rebuilt ({len(all_dates)} days of history)")


def grade_team_totals(ws_tt, scores: dict, yesterday: str) -> int:
    """Grade yesterday's Team Totals rows from actual team scores."""
    all_vals = ws_tt.get_all_values()
    if len(all_vals) < 2:
        return 0

    header = all_vals[0]
    def ci(name):
        try:    return header.index(name)
        except: return -1

    c_date    = ci("Date")
    c_game    = ci("Game")
    c_team    = ci("Team")
    c_dir     = ci("Direction")
    c_line    = ci("Book Line")
    c_units   = ci("Units")
    c_result  = ci("Result")
    c_away_sc = ci("Away Score")
    c_home_sc = ci("Home Score")
    c_actual  = ci("Actual Team Total")
    c_units_r = ci("Units Result")

    updates = []
    graded  = 0

    for i, row in enumerate(all_vals[1:], start=2):
        while len(row) < len(header):
            row.append("")
        if c_date >= 0 and row[c_date] != yesterday:
            continue
        if c_result >= 0 and row[c_result] in {"Win", "Loss", "Push"}:
            continue

        game_str  = row[c_game] if c_game >= 0 else ""
        team      = row[c_team].strip() if c_team >= 0 else ""
        direction = row[c_dir].strip() if c_dir >= 0 else ""
        game_key  = game_str.lower()
        score_data = scores.get(game_key)
        if not score_data:
            continue

        parts     = game_str.split(" @ ")
        away_team = parts[0].strip() if len(parts) == 2 else ""
        home_team = parts[1].strip() if len(parts) == 2 else ""
        away_s    = score_data["away_score"]
        home_s    = score_data["home_score"]

        if team == home_team:
            actual = home_s
        elif team == away_team:
            actual = away_s
        else:
            continue

        try:
            line_val = float(row[c_line]) if c_line >= 0 else None
            u        = float(row[c_units]) if c_units >= 0 and row[c_units] else 0.0
            actual_f = float(actual)
        except (ValueError, TypeError):
            continue
        if line_val is None:
            continue

        diff = actual_f - line_val
        if direction == "Under":
            diff = -diff
        if abs(diff) < 0.01:
            result, units_result = "Push", 0.0
        elif diff > 0:
            result, units_result = "Win", round(u, 2)
        else:
            result, units_result = "Loss", round(-u, 2)

        updates.append({
            "row": i,
            "away_s": away_s, "home_s": home_s,
            "actual": actual_f, "result": result, "units_result": units_result,
        })
        graded += 1

    for u in updates:
        r = u["row"]
        batch = []
        if c_away_sc >= 0: batch.append({"range": f"R{r}C{c_away_sc+1}", "values": [[u["away_s"]]]})
        if c_home_sc >= 0: batch.append({"range": f"R{r}C{c_home_sc+1}", "values": [[u["home_s"]]]})
        if c_actual  >= 0: batch.append({"range": f"R{r}C{c_actual+1}",  "values": [[u["actual"]]]})
        if c_result  >= 0: batch.append({"range": f"R{r}C{c_result+1}",  "values": [[u["result"]]]})
        if c_units_r >= 0: batch.append({"range": f"R{r}C{c_units_r+1}", "values": [[u["units_result"]]]})
        if batch:
            sheets_call(ws_tt.batch_update, batch, value_input_option="USER_ENTERED")
    return graded


def grade_props(ws_props, scores: dict, player_stats: dict, yesterday: str) -> int:
    """
    Grade yesterday's Player Props Shadow rows using MLB Stats API boxscore data.
    Prop types: SP Strikeouts, Total Bases, Home Run, H+R+RBI.
    Over/Under grading: actual > line = Over wins, actual < line = Under wins,
    actual == line = Push.
    """
    all_vals = ws_props.get_all_values()
    if len(all_vals) < 2:
        return 0

    header = all_vals[0]
    graded = 0

    def ci(name):
        try:    return header.index(name)
        except: return -1

    c_date    = ci("Date")
    c_game    = ci("Game")
    c_prop    = ci("Prop Type")
    c_player  = ci("Player")
    c_dir     = ci("Direction")
    c_line    = ci("Book Line")
    c_juice   = ci("Book Juice")
    c_units   = ci("Units")
    c_result  = ci("Result")
    c_actual  = ci("Actual Stat")
    c_units_r = ci("Units Result")

    # Prop type → player_stats key
    PROP_KEY = {
        "SP Strikeouts": "strikeouts",
        "Total Bases":   "total_bases",
        "Home Run":      "home_runs",
        "H+R+RBI":       "h_r_rbi",
    }

    batch_all = []

    for i, row in enumerate(all_vals[1:], start=2):
        while len(row) < len(header):
            row.append("")

        if c_date >= 0 and row[c_date] != yesterday:
            continue
        if c_result >= 0 and row[c_result] not in ("", "Pending"):
            continue

        prop_type = row[c_prop].strip() if c_prop >= 0 else ""
        if prop_type not in PROP_KEY:
            continue

        player    = row[c_player].strip().lower() if c_player >= 0 else ""
        direction = row[c_dir].strip() if c_dir >= 0 else ""
        stat_key  = PROP_KEY[prop_type]

        # Look up player stat — try exact match then partial
        pdata = player_stats.get(player)
        if not pdata:
            for pname, pstats in player_stats.items():
                if player and (player in pname or pname in player):
                    pdata = pstats
                    break

        actual = pdata.get(stat_key) if pdata else None

        try:
            line = float(row[c_line]) if c_line >= 0 and row[c_line] else None
        except (ValueError, TypeError):
            line = None

        try:
            juice = float(row[c_juice]) if c_juice >= 0 and row[c_juice] else -110
        except (ValueError, TypeError):
            juice = -110

        try:
            units = float(row[c_units]) if c_units >= 0 and row[c_units] else 0.5
        except (ValueError, TypeError):
            units = 0.5

        if actual is None or line is None:
            # Check if game is completed — if so and player not found, they DNP (void the bet)
            game_key = row[c_game].strip().lower() if c_game >= 0 else ""
            game_done = bool(scores.get(game_key))
            if actual is None and game_done:
                result       = "No Action"
                units_result = 0.0
            else:
                result       = "Pending"
                units_result = ""
        elif actual == line:
            result       = "Push"
            units_result = 0.0
        elif (direction == "Over" and actual > line) or (direction == "Under" and actual < line):
            result = "Win"
            if juice < 0:
                units_result = round(units * (100 / abs(juice)), 3)
            else:
                units_result = round(units * (juice / 100), 3)
        else:
            result       = "Loss"
            units_result = -units

        if c_actual >= 0 and actual is not None:
            batch_all.append({"range": f"R{i}C{c_actual+1}", "values": [[actual]]})
        if c_result >= 0:
            batch_all.append({"range": f"R{i}C{c_result+1}", "values": [[result]]})
        if c_units_r >= 0 and units_result != "":
            batch_all.append({"range": f"R{i}C{c_units_r+1}", "values": [[units_result]]})
        graded += 1

    if batch_all:
        sheets_call(ws_props.batch_update, batch_all, value_input_option="USER_ENTERED")

    return graded


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    print("=" * 60)
    print("grade_bets.py — Fantasy Six Pack Bet Grader")
    print(f"Grading date: {yesterday}")
    print("=" * 60)

    print("\nFetching MLB scores from Stats API ...")
    scores = fetch_scores(yesterday)
    print(f"  {len(scores)} completed games found")

    print("\nConnecting to Google Sheets ...")
    gc = auth()

    print("\nGrading Bet History ...")
    ws_hist = get_ws(gc, "Bet History")

    # Migration: ensure all data rows in Bet History use the current column format
    # (26 columns, with "DK Juice" at position 12 between Book Juice and Our Projection).
    # Old rows (written before DK Juice was added) have "Our Projection" at position 12.
    # Detection: position 12 holds a decimal float > 2.0 → old row, needs "DK Juice" inserted.
    # This runs every time but becomes a no-op once all rows are migrated.
    _all_hist = ws_hist.get_all_values()
    _hdr_check = _all_hist[0] if _all_hist else []
    if "Book Juice" in _hdr_check:
        bj_idx    = _hdr_check.index("Book Juice")
        insert_at = bj_idx + 1
        # Ensure header has DK Juice
        if "DK Juice" not in _hdr_check:
            _hdr_check.insert(insert_at, "DK Juice")
        migrated  = [_hdr_check]
        count_old = 0
        for _row in _all_hist[1:]:
            while len(_row) <= insert_at:
                _row.append("")
            val = str(_row[insert_at]).strip()
            try:
                is_old = "." in val and float(val) > 2.0
            except (ValueError, TypeError):
                is_old = False
            if is_old:
                _row.insert(insert_at, "")
                count_old += 1
            migrated.append(_row)
        if count_old > 0:
            sheets_call(ws_hist.clear)
            time.sleep(2)
            sheets_call(ws_hist.update, migrated, value_input_option="USER_ENTERED")
            print(f"  [migrated {count_old} old rows to current column format]")

    graded_hist, hist_updates, hist_header, hist_rows = grade_history(ws_hist, scores, yesterday)
    print(f"  {graded_hist} bets graded")
    if graded_hist > 0:
        # Re-read so results summary has the freshly-written scores/results
        _fresh = ws_hist.get_all_values()
        hist_header = _fresh[0]
        hist_rows   = _fresh[1:]

    print("\nGrading ML RL Shadow ...")
    ws_shadow = get_ws(gc, "ML RL")
    graded_shadow, avg_err = grade_shadow(ws_shadow, scores, yesterday)
    print(f"  {graded_shadow} shadow rows graded")
    if graded_shadow > 0:
        print(f"  Average prediction error (ML/RL): {avg_err:.4f}")
        print(f"  (0 = perfect, 1 = worst — lower is better calibration)")

    print("\nGrading Game Total Shadow ...")
    ws_gt_shadow = get_ws(gc, "Game Totals")
    graded_gt = grade_gt_shadow(ws_gt_shadow, scores, yesterday)
    print(f"  {graded_gt} game total shadow rows graded")

    print("\nGrading Team Totals ...")
    try:
        ws_tt = get_ws(gc, "Team Totals")
        graded_tt = grade_team_totals(ws_tt, scores, yesterday)
        print(f"  {graded_tt} team total rows graded")
    except Exception as e:
        print(f"  [Team Totals tab not found — will be created by analyze_edges.py]")

    print("\nGrading Player Props Shadow ...")
    try:
        ws_props = get_ws(gc, "Player Props Shadow")
        print("  Fetching per-player box score stats ...")
        player_stats = fetch_player_stats(yesterday)
        print(f"  {len(player_stats)} player stat lines loaded")
        graded_props = grade_props(ws_props, scores, player_stats, yesterday)
        print(f"  {graded_props} prop rows graded")
    except Exception as e:
        print(f"  [Player Props Shadow tab not found or error: {e}]")

    print("\nRebuilding Performance tab ...")
    rebuild_performance(gc)

    print("\nVenue calibration report ...")
    venue_calibration_report(ws_hist)

    print("\nRunning park factor tracker ...")
    from build_park_factor_data import daily_check
    daily_check(gc=gc, verbose=True)

    print("\nChecking edge calibration thresholds (Moneyline, Run Line, Game Total tracked separately) ...")
    check_edge_calibration(gc, ws_shadow, "Moneyline", "Moneyline Edge Calibration")
    check_edge_calibration(gc, ws_shadow, "Run Line", "Run Line Edge Calibration")
    check_edge_calibration(gc, ws_gt_shadow, "Game Total", "Game Total Edge Calibration",
                            edge_col="Edge % of Line", filter_by_bet_type=False)

    print("\nChecking Team Total star-tier calibration ...")
    try:
        ws_tt = get_ws(gc, "Team Totals")
        check_team_total_star_calibration(gc, ws_tt)
    except Exception as e:
        print(f"  [Team Totals tab not found — skipping star calibration check]")

    # ── Previous day results summary (read graded rows from sheet) ───────────
    # Use the canonical Python constant (not the sheet header) because the sheet
    # header row may be stale (e.g. missing "DK Juice") while data rows were
    # written with the current HISTORY_HEADER column order.
    HISTORY_HEADER = [
        "Date", "Game", "Time (ET)", "Away SP", "Home SP",
        "Bet Type", "Direction", "Stars", "Units Bet",
        "Book", "Book Line", "Book Juice", "DK Juice", "Our Projection",
        "Edge (runs)", "Away Score", "Home Score", "Actual Total",
        "Result", "Units Result", "Park Factor", "Venue",
        "Confidence", "Confidence %", "Bet On", "Edge %",
    ]
    hi = {h: i for i, h in enumerate(HISTORY_HEADER)}
    def hv(row, col_name):
        i = hi.get(col_name, -1)
        return row[i] if 0 <= i < len(row) else ""

    yesterday_rows = [r for r in hist_rows if hv(r, "Date") == yesterday
                      and hv(r, "Result") not in ("", "Pending")]

    if yesterday_rows:
        print("\n" + "=" * 70)
        print(f"  YESTERDAY'S RESULTS  ({yesterday})")
        print("=" * 70)
        print(f"  {'Bet':<42} {'Score':<10} {'Result':<12} Units")
        print("  " + "-" * 70)

        total_units = 0.0
        wins = losses = pushes = 0
        for row in yesterday_rows:
            btype   = hv(row, "Bet Type")
            direc   = hv(row, "Direction")
            beton   = hv(row, "Bet On")
            line    = hv(row, "Book Line")
            game    = hv(row, "Game")
            away_s  = hv(row, "Away Score")
            home_s  = hv(row, "Home Score")
            result  = hv(row, "Result")
            u_res   = hv(row, "Units Result")
            away_g, home_g = (game.split(" @ ") + ["",""])[:2]
            score_str = f"{away_s}-{home_s}" if away_s and home_s else "?"

            if btype == "Game Total":
                bet_label = f"{away_g} @ {home_g} {'o' if direc == 'Over' else 'u'}{line}"
            elif btype == "Moneyline":
                bet_label = f"{beton} ML"
            else:
                bet_label = f"{beton}"

            try: u_float = float(u_res)
            except: u_float = 0.0

            res_sym = "Win" if result == "Win" else "Loss" if result == "Loss" else "Push"
            u_str   = f"+{u_float}u" if u_float > 0 else f"{u_float}u"
            print(f"  {bet_label:<42} {score_str:<10} {res_sym:<12} {u_str}")

            total_units += u_float
            if result == "Win":    wins   += 1
            elif result == "Loss": losses += 1
            else:                  pushes += 1

        net_str = f"+{round(total_units,2)}u" if total_units >= 0 else f"{round(total_units,2)}u"
        print(f"\n  Record: {wins}W {losses}L {pushes}P  |  Net: {net_str}")
        print("=" * 70)

    # ── Player Props summary (yesterday's graded props by type and star level) ──
    try:
        ws_props_sum = get_ws(gc, "Player Props Shadow")
        all_prop_vals = ws_props_sum.get_all_values()
        if all_prop_vals and all_prop_vals[0]:
            ph = {h: i for i, h in enumerate(all_prop_vals[0])}
            def pv(row, col):
                i = ph.get(col, -1)
                return row[i] if 0 <= i < len(row) else ""
            yest_props = [r for r in all_prop_vals[1:]
                          if pv(r, "Date") == yesterday and pv(r, "Result") not in ("", "Pending")]
            if yest_props:
                print("\n" + "=" * 70)
                print(f"  PLAYER PROPS SUMMARY  ({yesterday})")
                print("=" * 70)
                from collections import defaultdict
                # Summarize by prop type x star level
                groups = defaultdict(lambda: {"W": 0, "L": 0, "P": 0, "units": 0.0})
                for row in yest_props:
                    ptype  = pv(row, "Prop Type")
                    stars  = pv(row, "Stars")
                    result = pv(row, "Result")
                    try: u = float(pv(row, "Units Result"))
                    except: u = 0.0
                    key = (ptype, stars)
                    groups[key]["W" if result == "Win" else "L" if result == "Loss" else "P"] += 1
                    groups[key]["units"] += u

                print(f"  {'Prop Type':<20} {'Stars':<8} {'W-L-P':<10} {'Net Units'}")
                print("  " + "-" * 55)
                for (ptype, stars), d in sorted(groups.items()):
                    record = f"{d['W']}-{d['L']}-{d['P']}"
                    net = d["units"]
                    net_str = f"+{net:.2f}u" if net >= 0 else f"{net:.2f}u"
                    print(f"  {ptype:<20} {stars:<8} {record:<10} {net_str}")

                total_w = sum(d["W"] for d in groups.values())
                total_l = sum(d["L"] for d in groups.values())
                total_p = sum(d["P"] for d in groups.values())
                total_u = sum(d["units"] for d in groups.values())
                net_str = f"+{total_u:.2f}u" if total_u >= 0 else f"{total_u:.2f}u"
                print(f"\n  TOTAL: {total_w}W {total_l}L {total_p}P  |  Net: {net_str}")
                print("=" * 70)
            else:
                print("\n[Player Props: no graded props from yesterday]")
    except Exception as e:
        print(f"\n[Player Props summary skipped: {e}]")

    print("\nDone.")


# ── Proactive calibration checks ───────────────────────────────────────────────
# Tiered schedule: check often early (when each new batch is most likely to reveal
# something actionable), then slow down once volume is high enough that any single
# batch is less likely to shift the picture. Run Line gets tighter thresholds than
# Moneyline since it generates roughly half the volume (~28 vs ~61 graded as of
# 2026-06-21) — checking at the same bet *count* keeps both checked on a similar
# *cadence* in calendar time, which is the goal (catch issues fast, not wait on volume).
CALIBRATION_CONFIG = {
    "Moneyline":  {"first_threshold": 100, "increment_early": 100, "increment_late": 200, "graduate_at": 500},
    "Run Line":   {"first_threshold": 50,  "increment_early": 50,  "increment_late": 100, "graduate_at": 500},
    "Game Total": {"first_threshold": 100, "increment_early": 100, "increment_late": 200, "graduate_at": 500},
}
# A single check is just one noisy snapshot — require the same directional signal to
# show up across this many CONSECUTIVE checks (each on a fresh, larger batch of data)
# before calling it actionable. The point of checking often is to collect data points
# to compare against each other, not to react to the first thing that looks interesting.
CALIBRATION_REQUIRED_STREAK = 2


def get_or_create_ws(gc, tab, rows=20, cols=8):
    sh = gc.open_by_key(ODDS_SHEET_ID)
    try:
        return sh.worksheet(tab)
    except gspread.exceptions.WorksheetNotFound:
        return sheets_call(sh.add_worksheet, title=tab, rows=rows, cols=cols)


def check_edge_calibration(gc, ws_shadow, bet_type: str, metric_name: str,
                            edge_col: str = "Edge vs Book%", filter_by_bet_type: bool = True):
    """
    Proactive, threshold-triggered check (mirrors the park-factor-tracker pattern):
    once enough new graded bets of this specific type accumulate, automatically
    re-run the edge_pct-vs-performance backtest and flag if a real signal has
    emerged — rather than waiting to be asked. Checkpoint state persists in the
    'Calibration Tracker' tab so this is idempotent across daily runs.

    bet_type: "Moneyline", "Run Line", or "Game Total" — each tracked completely
    separately (different bets, different edge distributions, different current
    performance), never pooled together.
    edge_col: column name holding the edge percentage — "Edge vs Book%" for
    Moneyline & Run Line, "Edge % of Line" for Game Totals.
    filter_by_bet_type: ML/RL Shadow mixes Moneyline and Run Line rows in one tab
    and needs filtering by the "Bet Type" column; Game Total Shadow is already a
    single bet type per tab, so no filter is needed.
    """
    cfg = CALIBRATION_CONFIG[bet_type]

    ws_calib = get_or_create_ws(gc, "Calibration Tracker")
    existing = ws_calib.get_all_values()
    header = ["Metric", "Last Checked Count", "Last Check Date", "Next Threshold", "Status", "Signal Streak", "Notes"]
    if not existing or existing[0] != header:
        sheets_call(ws_calib.update, [header], value_input_option="USER_ENTERED")
        existing = [header]

    row_idx = None
    next_threshold = cfg["first_threshold"]
    signal_streak = 0
    for i, r in enumerate(existing[1:], start=2):
        if r and r[0] == metric_name:
            row_idx = i
            try:
                next_threshold = int(r[3])
            except (ValueError, IndexError, TypeError):
                next_threshold = cfg["first_threshold"]
            try:
                signal_streak = int(r[5])
            except (ValueError, IndexError, TypeError):
                signal_streak = 0
            break

    sh_rows = ws_shadow.get_all_values()
    if len(sh_rows) < 2:
        return
    sh_hdr = sh_rows[0]
    sci = {h: i for i, h in enumerate(sh_hdr)}

    result_col = "Bet Result" if "Bet Result" in sci else "Result"

    graded = []
    for r in sh_rows[1:]:
        if not r:
            continue
        if filter_by_bet_type and "Bet Type" in sci and r[sci["Bet Type"]] != bet_type:
            continue
        result = r[sci[result_col]] if result_col in sci else ""
        if result not in ("Win", "Loss", "Push"):
            continue
        if sci.get("Pitcher Flag") is not None and r[sci["Pitcher Flag"]].strip() == "Missing SP":
            continue
        try:
            edge = float(r[sci[edge_col]].replace("%", ""))
            u = float(r[sci["Units Result"]]) if r[sci["Units Result"]] else 0.0
        except (ValueError, TypeError, KeyError):
            continue
        graded.append((edge, result, u))

    current_count = len(graded)
    today = datetime.now().strftime("%Y-%m-%d")

    if current_count < next_threshold:
        status = f"Accumulating ({current_count}/{next_threshold})"
        notes = ""
        print(f"  {bet_type}: {current_count}/{next_threshold} graded bets — not yet at recalibration threshold")
    else:
        thresholds = [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 18, 20]
        results = []
        for x in thresholds:
            sub = [(e, r, u) for e, r, u in graded if e >= x]
            if len(sub) < 10:
                continue
            w = sum(1 for _, r, _ in sub if r == "Win")
            l = sum(1 for _, r, _ in sub if r == "Loss")
            units = sum(u for _, _, u in sub)
            roi = units / len(sub) if sub else 0.0
            results.append((x, len(sub), roi))

        increasing_streak = 0
        max_streak = 0
        for i in range(1, len(results)):
            if results[i][2] >= results[i - 1][2]:
                increasing_streak += 1
                max_streak = max(max_streak, increasing_streak)
            else:
                increasing_streak = 0
        signal_this_check = max_streak >= 4

        print(f"\n  *** {bet_type} Calibration Check triggered at {current_count} graded bets ***")
        for x, n, roi in results:
            print(f"    edge>={x}%  n={n}  ROI/bet={roi:+.3f}")

        # A single check is just one data point — only act once the SAME directional
        # signal has shown up across CALIBRATION_REQUIRED_STREAK consecutive checks.
        if signal_this_check:
            signal_streak += 1
        else:
            signal_streak = 0

        if signal_streak >= CALIBRATION_REQUIRED_STREAK:
            print(f"    >>> ACTIONABLE: signal confirmed across {signal_streak} consecutive checks — recommend reviewing {bet_type} star thresholds with Claude.")
            status = f"ACTIONABLE at {current_count} — signal confirmed {signal_streak}x in a row"
        elif signal_this_check:
            print(f"    Signal detected this check (1 of {CALIBRATION_REQUIRED_STREAK} needed) — logging and re-checking at next threshold before recommending any action.")
            status = f"Monitoring at {current_count} — signal seen, streak {signal_streak}/{CALIBRATION_REQUIRED_STREAK}"
        else:
            print("    No clear signal this check — thresholds left as-is, streak reset.")
            status = f"Checked at {current_count} — no signal, streak reset"

        notes = "; ".join(f"{x}%+:{roi:+.3f}(n={n})" for x, n, roi in results)
        increment = cfg["increment_late"] if current_count >= cfg["graduate_at"] else cfg["increment_early"]
        next_threshold = current_count + increment

    new_row = [metric_name, current_count, today, next_threshold, status, signal_streak, notes]
    if row_idx:
        sheets_call(ws_calib.update, [new_row], range_name=f"A{row_idx}:G{row_idx}", value_input_option="USER_ENTERED")
    else:
        sheets_call(ws_calib.append_row, new_row, value_input_option="USER_ENTERED")


# ── Team Total star-tier calibration ─────────────────────────────────────────
def check_team_total_star_calibration(gc, ws_tt):
    """
    Tracks win rate by star tier for Team Totals and flags when 5-star bets are
    not outperforming 4-star bets — which would mean the 5-star confidence threshold
    is too loose and needs to be raised.

    Checks at 60 graded bets, then every 30 after that. Requires 2 consecutive
    checks showing the same signal before flagging as actionable (same pattern as
    edge calibration — one data point isn't enough).
    """
    FIRST_THRESHOLD   = 60
    INCREMENT         = 30
    REQUIRED_STREAK   = 2
    METRIC_NAME       = "Team Total Star Tier"
    MIN_SAMPLE_PER_TIER = 15  # need at least this many per tier to draw conclusions

    ws_calib = get_or_create_ws(gc, "Calibration Tracker")
    existing = ws_calib.get_all_values()
    header = ["Metric", "Last Checked Count", "Last Check Date", "Next Threshold", "Status", "Signal Streak", "Notes"]
    if not existing or existing[0] != header:
        sheets_call(ws_calib.update, [header], value_input_option="USER_ENTERED")
        existing = [header]

    row_idx        = None
    next_threshold = FIRST_THRESHOLD
    signal_streak  = 0
    for i, r in enumerate(existing[1:], start=2):
        if r and r[0] == METRIC_NAME:
            row_idx = i
            try:
                next_threshold = int(r[3])
            except (ValueError, IndexError, TypeError):
                next_threshold = FIRST_THRESHOLD
            try:
                signal_streak = int(r[5])
            except (ValueError, IndexError, TypeError):
                signal_streak = 0
            break

    rows = ws_tt.get_all_values()
    if len(rows) < 2:
        return
    hdr = rows[0]
    sci = {h: i for i, h in enumerate(hdr)}

    result_col = "Result"
    stars_col  = "Stars"
    units_col  = "Units Result"
    if result_col not in sci or stars_col not in sci:
        return

    graded = []
    for r in rows[1:]:
        if not r:
            continue
        result = r[sci[result_col]] if sci.get(result_col) is not None else ""
        if result not in ("Win", "Loss", "Push"):
            continue
        stars_raw = r[sci[stars_col]] if sci.get(stars_col) is not None else ""
        star_count = stars_raw.count("⭐") if stars_raw else 0
        try:
            u = float(r[sci[units_col]]) if units_col in sci and r[sci[units_col]] else 0.0
        except (ValueError, TypeError):
            u = 0.0
        graded.append((star_count, result, u))

    current_count = len(graded)
    today = datetime.now().strftime("%Y-%m-%d")

    if current_count < next_threshold:
        print(f"  Team Totals (star tier): {current_count}/{next_threshold} graded bets — not yet at recalibration threshold")
        return

    # Build per-tier stats
    tier_stats = {}
    for stars in [5, 4, 3]:
        subset = [(r, u) for s, r, u in graded if s == stars]
        if not subset:
            continue
        w = sum(1 for r, _ in subset if r == "Win")
        l = sum(1 for r, _ in subset if r == "Loss")
        units = sum(u for _, u in subset)
        win_pct = w / len(subset) if subset else 0.0
        tier_stats[stars] = {"n": len(subset), "w": w, "l": l, "win_pct": win_pct, "units": units}

    print(f"\n  *** Team Total Star Tier Calibration Check triggered at {current_count} graded bets ***")
    for stars in sorted(tier_stats.keys(), reverse=True):
        s = tier_stats[stars]
        print(f"    {'⭐'*stars}: {s['n']} bets  {s['w']}W {s['l']}L  {s['win_pct']*100:.1f}%  {s['units']:+.2f}u")

    # Signal: 5-star win rate is NOT higher than 4-star AND both have enough sample
    five  = tier_stats.get(5, {})
    four  = tier_stats.get(4, {})
    signal_this_check = (
        five.get("n", 0) >= MIN_SAMPLE_PER_TIER
        and four.get("n", 0) >= MIN_SAMPLE_PER_TIER
        and five.get("win_pct", 1.0) <= four.get("win_pct", 0.0)
    )

    if signal_this_check:
        signal_streak += 1
    else:
        signal_streak = 0

    if signal_streak >= REQUIRED_STREAK:
        print(f"    >>> ACTIONABLE: 5-star underperforming 4-star confirmed across {signal_streak} consecutive checks — recommend raising the 5-star confidence threshold.")
        status = f"ACTIONABLE at {current_count} — 5-star underperformance confirmed {signal_streak}x"
    elif signal_this_check:
        print(f"    Signal: 5-star win rate not exceeding 4-star (1 of {REQUIRED_STREAK} needed) — logging, will recheck at next threshold.")
        status = f"Monitoring at {current_count} — signal seen, streak {signal_streak}/{REQUIRED_STREAK}"
    else:
        print("    No issue detected — 5-star performing as expected vs 4-star.")
        status = f"Checked at {current_count} — no signal, streak reset"

    notes = " | ".join(
        f"{s}★: {tier_stats[s]['w']}W-{tier_stats[s]['l']}L ({tier_stats[s]['win_pct']*100:.1f}%)"
        for s in sorted(tier_stats.keys(), reverse=True)
    )
    next_threshold = current_count + INCREMENT

    new_row = [METRIC_NAME, current_count, today, next_threshold, status, signal_streak, notes]
    if row_idx:
        sheets_call(ws_calib.update, [new_row], range_name=f"A{row_idx}:G{row_idx}", value_input_option="USER_ENTERED")
    else:
        sheets_call(ws_calib.append_row, new_row, value_input_option="USER_ENTERED")


# ── Venue calibration report ──────────────────────────────────────────────────
def venue_calibration_report(ws_hist):
    """
    Print a daily summary of projection error by venue.
    Flags venues approaching calibration thresholds.
    """
    rows   = ws_hist.get_all_values()
    if len(rows) < 2:
        return

    header = rows[0]

    def c(name):
        try:
            return header.index(name)
        except ValueError:
            return -1

    c_venue  = c("Venue")
    c_proj   = c("Our Projection")
    c_actual = c("Actual Total")
    c_result = c("Result")

    if c_venue < 0:
        print("  Venue column not found — run backfill_venues.py first")
        return

    venue_data = {}

    for row in rows[1:]:
        while len(row) < len(header):
            row.append("")

        result = row[c_result]
        if result not in ("Win", "Loss", "Push"):
            continue

        venue = row[c_venue].strip().title() or "Unknown"
        try:
            proj   = float(row[c_proj])
            actual = float(row[c_actual])
        except (ValueError, TypeError):
            continue

        if venue not in venue_data:
            venue_data[venue] = []
        venue_data[venue].append((proj, actual))

    if not venue_data:
        print("  No venue data yet")
        return

    print(f"\n  {'Venue':<40} {'Games':>5} {'Avg Proj':>8} {'Avg Act':>7} {'Avg Err':>8} {'Direction':>10} {'Status':>16}")
    print("  " + "-" * 100)

    for venue, games in sorted(venue_data.items(), key=lambda x: -len(x[1])):
        n         = len(games)
        avg_proj  = sum(p for p, _ in games) / n
        avg_act   = sum(a for _, a in games) / n
        avg_err   = avg_act - avg_proj   # positive = we're under-projecting, negative = over-projecting
        direction = f"{'UNDER-PROJ':>10}" if avg_err > 0.3 else f"{'OVER-PROJ':>10}" if avg_err < -0.3 else f"{'~Neutral':>10}"

        if n >= 25:
            status = ">>> CALIBRATE NOW"
        elif n >= 10:
            status = "  Early signal"
        else:
            status = f"  {25 - n} more needed"

        print(f"  {venue:<40} {n:>5} {avg_proj:>8.2f} {avg_act:>7.2f} {avg_err:>+8.2f} {direction} {status:>16}")

    print()
    print("  Avg Err: positive = we're projecting too LOW (consider raising park factor)")
    print("           negative = we're projecting too HIGH (consider lowering park factor)")


if __name__ == "__main__":
    main()
