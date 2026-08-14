"""
nfl_props_model.py — Player prop projection engine.

Turns (player, opponent, game context) into a projected stat line for each of
the seven prop categories the model covers:
    QB pass yards, QB pass TDs, RB rush yards, RB reception yards,
    WR/TE reception yards, WR/TE receptions, anytime TD

IMPORTANT: this module produces PROJECTIONS ONLY. It never touches the Odds
API and does not need book lines to run, so the whole engine can be built and
validated with zero credits while FETCH_PLAYER_PROPS is still gated off. Edge
calculation (projection vs. book line) is a separate, later step.

Data sources, all free:
  - organic consensus sheet (Joe Bond's) -> per-player season baseline
  - nfl_props_data.load_defense_vs_position() -> opponent matchup
  - nfl_props_data.load_red_zone_splits()     -> TD context
  - nfl_props_data.load_player_shares()       -> role/volume context
  - nfl_analyze_edges.project_game_score()    -> game script
  - nfl_props_data.load_wr_cb_matchups()      -> weekly ESPN shadow-coverage PDF
"""

import math

import nfl_props_data as props_data
import nfl_analyze_edges as edges


# ── Model constants (Year-1 "best current guess", each reasoned) ─────────────

# The organic sheet carries SEASON totals, so a per-game baseline divides by
# expected games played — NOT by 17.
#
# Consensus season projections already discount for expected missed games, so
# dividing by the 17-game schedule length systematically under-projects what a
# player does in a game he actually plays. Measured 2026-08-12 against real
# 2025 per-game production (nflverse play-by-play), for players clearing a
# meaningful usage floor:
#     QB pass yds/gm   median (season/17) / actual = 0.946   (n=42)
#     rush yds/gm      median                      = 0.907   (n=43)
#     rec yds/gm       median                      = 0.911   (n=73)
# Three independent categories landing together at ~0.91 points to one common
# cause (the games-played discount) rather than three separate production
# shifts. Median-of-medians 0.911 x 17 = 15.5, which also matches the real
# world: NFL starters average roughly 15-16 games.
#
# Prop bets ask "given he plays, what does he do?", so conditioning on games
# played is the correct denominator.
#
# Re-derive this from 2026 actuals once a real season exists — rerun the same
# comparison (organic season total / X vs. nflverse per-game actual) and reset
# X to whatever makes the median 1.00.
EXPECTED_GAMES_PLAYED = 15.5

# ASSUMPTION STILL OPEN: the user confirmed the organic sheet refreshes all
# season. If those refreshes become REST-OF-SEASON totals once games are played
# (the usual in-season fantasy convention), this divisor must become games
# REMAINING — otherwise every projection silently shrinks week over week.
# Cannot be settled until the season starts and we see a refreshed copy.
# CHECK IN WEEK 2 and fix immediately if so.

# Defense-vs-position factors come from a full season of games, so they carry
# real signal, but they also absorb strength-of-schedule and small-sample noise
# (a defense that happened to face three elite WR rooms looks worse than it is).
# Applying them raw over-adjusts. 0.5 regresses each factor halfway to neutral:
# a defense allowing 20% more WR yards than average moves a projection +10%,
# not +20%. Conservative on purpose for year one; revisit once graded prop
# results exist.
MATCHUP_DAMPING = 0.50

# Game script. The organic baseline ALREADY embeds this player's team being
# good or bad, so comparing this game's projected team score to the LEAGUE
# average would double-count team strength. Instead we compare it to THIS
# TEAM'S own baseline scoring rate, so the factor sits at ~1.0 in a typical
# game and only moves for an unusually high- or low-scoring matchup.
# Damped less than the matchup factor because implied team total is a
# genuinely strong driver of prop volume — but still damped, since a team
# scoring more doesn't lift every player proportionally.
SCRIPT_DAMPING = 0.70

# Anytime TD: converting a projected team score into an expected number of
# OFFENSIVE touchdowns. A league-average 23-point team scores roughly 2.4 TDs
# (16.8 pts with XPs) plus ~1.8 FGs (5.4 pts) = ~22.2. So points-per-offensive-
# TD ~= 23 / 2.4 ~= 9.6. Using this rather than a flat /7 correctly accounts
# for the share of scoring that comes from field goals.
POINTS_PER_OFFENSIVE_TD = 9.6

# ── WR-CB matchup (weekly ESPN PDF) ──────────────────────────────────────────
#
# *** PROVISIONAL — DERIVED FROM ONE 6-ROW SUPER BOWL SHEET (NE@SEA only). ***
# Re-verify against the FIRST full-slate regular-season sheet before trusting
# any of it. See the RE-VERIFY checklist at the bottom of this block.
#
# FORMULA SOLVED 2026-08-12 from the sheet's own key + its six real matchup
# rows, confirmed 6/6 within rounding:
#
#     Matchup = (receiver's rate - league avg) + (defender's allowed - league avg)
#
# Units are the SAME as the underlying stat — percentage POINTS for T/R, and
# fantasy-points-per-route for F/R. (An earlier guess that it was a percent
# CHANGE was wrong.) Solving the six rows pins the two league averages:
#     receiver F/R  ~ 0.37     allowed F/R  ~ 0.28
#     receiver T/R  ~ 20.0%    allowed T/R  ~ 18.5%
#
# CRITICAL: that headline number DOUBLE-COUNTS the receiver's own quality,
# which our organic baseline already encodes. Smith-Njigba's eye-catching
# "+19%" vs Christian Gonzalez is almost entirely JSN being good — Gonzalez
# sits exactly at league average in F/R allowed, so the true defender-specific
# signal there is ZERO. Applying ESPN's number directly would double-count.
# We therefore use ONLY the defender half.
#
# We also weight by how often the two actually line up, from the route
# alignment splits (offense's LEFT faces defense's RIGHT):
#     exposure = LWR*RCB + Slot*SlotCB + RWR*LCB
# so a slot corner only moves a slot receiver's projection, not a boundary
# receiver's. Cooper Kupp vs Marcus Jones (89% slot, 0.37 allowed) comes out
# +14%; Mack Hollins vs Tariq Woolen (0.22 allowed) comes out -8%.
#
# F/R rather than T/R is the driver on purpose: T/R allowed is confounded by
# target-magnet effects (a shadow corner who follows WR1s shows a high allowed
# target rate simply because WR1s draw targets), while F/R is a cleaner
# efficiency measure.
LEAGUE_AVG_ALLOWED_FR = 0.28
WR_CB_MAX_ADJ = 0.15   # generous vs. the old sign-only hack; observed range was -8%..+14%

# LIMITATION: the sheet's key says 'S' marks projected SHADOW coverage, but in
# the parsed text 'S' is the alignment code for slot (the L/S/R pairing across
# the table confirms it), so shadow is presumably marked by colour/bold, which
# text extraction loses. In a true shadow game the defender follows the
# receiver, so historical alignment understates exposure and this adjustment is
# too conservative. ASK THE USER whether shadow games are flagged in a way we
# can detect; if so, push exposure toward 1.0 for those pairings.
#
# CONFOUND — allowed F/R is NOT clean defender quality. Christian Gonzalez was
# one of the best CBs in the league in 2025 yet grades exactly league-average
# here (0.28 allowed F/R). The reason is opponent quality: a corner who travels
# with WR1s is measured against the best receivers in the league, while a lesser
# corner drawing WR3s looks better than he is. So this adjustment systematically
# UNDER-penalises elite corners and OVER-penalises weak ones. Not correctable
# from a single game's data — test it on the first full-slate sheet by checking
# whether a defender's allowed F/R tracks the quality of receivers he faced.
# Until then the exposure weighting keeps the magnitude modest (observed range
# -8% to +14%), which is the right posture for a signal we know is biased.
#
# ── RE-VERIFY ON THE FIRST FULL-SLATE REGULAR-SEASON SHEET ───────────────────
#   1. Does the formula still hold with ~80 rows instead of 6? (6/6 on a single
#      game is suggestive, not conclusive.)
#   2. Re-solve the four league-average constants by regression across the full
#      slate rather than from six rows — 0.37 / 0.28 / 20.0% / 18.5% are the
#      numbers most likely to move.
#   3. Does the PARSER survive a multi-game, multi-page layout? It was built
#      against a single-game 6-row page; column x-positions and pagination may
#      differ. A non-empty sheet parsing to 0 rows means the layout changed.
#   4. Is shadow coverage detectable in the text? (See limitation above.)
#   5. Check the opponent-quality confound above.

# Organic sheet uses the "LAR" abbreviation for the Rams; nflverse (and
# therefore every other table in this model) uses "LA".
ORGANIC_TEAM_FIXES = {"LAR": "LA"}

# Which organic-sheet column and which defense-vs-position factor drive each
# prop. (organic_col, dvp_position_group, dvp_stat)
PROP_SPECS = {
    "pass_yds":  ("Pass Yds", "QB", "pass_yds"),
    "pass_tds":  ("Pass TD",  "QB", "pass_td"),
    "rush_yds":  ("Rush Yds", "RB", "rush_yds"),
    "rec_yds":   ("Rec Yds",  None, "rec_yds"),   # position group = player's own
    "receptions": ("Rec",     None, "rec"),
}


def _f(v, default=0.0):
    """Organic sheet values arrive as display strings with thousands commas."""
    if v is None:
        return default
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, AttributeError):
        return default


def _damped(factor: float, damping: float) -> float:
    """Regress a multiplicative factor toward neutral (1.0)."""
    return 1.0 + damping * (factor - 1.0)


# ── Baseline loading ──────────────────────────────────────────────────────────
def load_organic_baselines(gc, name_map: dict) -> dict:
    """
    Read the organic consensus sheet into per-player SEASON totals keyed by
    gsis player_id, with the raw name kept for reporting.

    Players who can't be resolved to an id are still returned (keyed by their
    normalized name) rather than dropped — a book may well offer a prop on
    someone nflverse hasn't rostered yet, and silently losing them is exactly
    the failure mode that broke MLB props. `id_resolved` marks which is which.
    """
    sh = gc.open_by_key(edges.ORGANIC_SHEET_ID)
    out = {}
    for pos, tab in (("QB", "LIVE PROJECTIONS QB"), ("RB", "LIVE PROJECTIONS RB"),
                     ("WR", "LIVE PROJECTIONS WR"), ("TE", "LIVE PROJECTIONS TE")):
        try:
            rows = edges.sheet_to_dicts(sh.worksheet(tab))
        except Exception as e:
            print(f"  [warn] organic tab '{tab}' unreadable: {e}")
            continue
        for row in rows:
            name = (row.get(pos) or "").strip()
            if not name:
                continue
            team = (row.get("Team") or "").strip().upper()
            team = ORGANIC_TEAM_FIXES.get(team, team)
            pid = props_data.resolve_player_id(name, name_map)
            key = pid or f"name:{props_data.normalize_name(name)}"
            out[key] = {
                "player_id": pid,
                "id_resolved": pid is not None,
                "name": name,
                "team": team,
                "position": pos,
                "pass_yds": _f(row.get("Pass Yds")),
                "pass_tds": _f(row.get("Pass TD")),
                "rush_yds": _f(row.get("Rush Yds")),
                "rush_tds": _f(row.get("Rush TD")),
                "rec_yds":  _f(row.get("Rec Yds")),
                "receptions": _f(row.get("Rec")),
                "rec_tds":  _f(row.get("Rec TD")),
                "targets":  _f(row.get("Targets")),
            }
    return out


# ── Matchup factors ───────────────────────────────────────────────────────────
def matchup_factor(dvp: dict, opponent: str, pos_group: str, stat: str) -> float:
    """Damped defense-vs-position factor. 1.0 when the opponent is unknown."""
    opp = dvp.get(opponent)
    if not opp:
        return 1.0
    raw = opp.get(pos_group, {}).get(f"{stat}_factor", 1.0)
    return _damped(raw, MATCHUP_DAMPING)


def script_factor(proj_team_score: float, team_baseline_ppg: float) -> float:
    """
    How much better/worse this specific game projects for the team than their
    own typical game. See SCRIPT_DAMPING above for why this is measured against
    the team's own baseline rather than the league average.
    """
    if not team_baseline_ppg:
        return 1.0
    return _damped(proj_team_score / team_baseline_ppg, SCRIPT_DAMPING)


def wr_cb_factor(player_name: str, opponent: str, wr_cb_rows: list) -> tuple[float, str]:
    """
    Shadow-coverage adjustment from the weekly ESPN PDF.
    Returns (multiplier, human-readable note). Neutral if the player isn't in
    this week's sheet (it only covers a handful of tracked matchups).

    MUST match on opponent as well as player. The PDF describes ONE specific
    game — a bug caught in testing had Smith-Njigba carrying his "vs Christian
    Gonzalez" (NE) adjustment into games against ARI/WAS/LAC/SF, because the
    lookup keyed on receiver name alone.
    """
    if not wr_cb_rows or not opponent:
        return 1.0, ""
    key = props_data.normalize_name(player_name)
    for row in wr_cb_rows:
        if props_data.normalize_name(row.get("receiver", "")) != key:
            continue
        if (row.get("def_team") or "").strip().upper() != opponent.strip().upper():
            continue
        def _pct(v):
            try:
                return float(str(v).replace("%", "").strip()) / 100.0
            except (ValueError, AttributeError):
                return 0.0

        try:
            def_fr = float(row.get("cov_fr"))
        except (TypeError, ValueError):
            return 1.0, ""

        # How often these two actually face each other. Offense's left side
        # lines up against the defense's right, hence LWR<->RCB / RWR<->LCB.
        exposure = (_pct(row.get("lwr_pct")) * _pct(row.get("rcb_pct"))
                    + _pct(row.get("slot_pct")) * _pct(row.get("def_slot_pct"))
                    + _pct(row.get("rwr_pct")) * _pct(row.get("lcb_pct")))
        if exposure <= 0:
            return 1.0, ""

        # Defender half of ESPN's matchup formula only — the receiver half is
        # already in our baseline (see the block comment above).
        defender_delta = (def_fr - LEAGUE_AVG_ALLOWED_FR) / LEAGUE_AVG_ALLOWED_FR
        adj = max(-WR_CB_MAX_ADJ, min(WR_CB_MAX_ADJ, exposure * defender_delta))
        defender = row.get("defender", "?")
        return 1.0 + adj, (f"vs {defender} ({exposure*100:.0f}% of routes, "
                           f"{def_fr:.2f} F/R allowed) {adj*100:+.1f}%")
    return 1.0, ""


# ── Red zone context ──────────────────────────────────────────────────────────
def red_zone_note(pid: str, rz: dict) -> str:
    """
    Human-readable red-zone context for the sheet. Not a projection input in
    v1 — the organic sheet's consensus TD projections already price in role,
    and 2025 red-zone usage can actively mislead for a player who changed
    teams over the offseason. This becomes a real adjustment once 2026 usage
    accumulates; for now it's shown so the reasoning is visible.
    """
    p = rz.get(pid)
    if not p:
        return ""
    r5, r10 = p["rz5"], p["rz10"]
    touches5 = r5["rush_att"] + r5["tgt"]
    tds5 = r5["rush_td"] + r5["rec_td"]
    return f"RZ5: {touches5:.0f} touch/{tds5:.0f} TD | RZ10: {r10['rush_att'] + r10['tgt']:.0f} touch"


# ── Core projection ───────────────────────────────────────────────────────────
def project_player_props(baseline: dict, opponent: str, proj_team_score: float,
                         team_baseline_ppg: float, dvp: dict, rz: dict,
                         wr_cb_rows: list) -> dict:
    """
    Project every applicable prop category for one player in one game.

    projection = (season total / 17) x script_factor x matchup_factor
    and, for pass-catchers, x wr_cb_factor.

    Anytime TD is modelled separately via Poisson — see below.
    """
    pos = baseline["position"]
    script = script_factor(proj_team_score, team_baseline_ppg)
    wr_cb_mult, wr_cb_note = (wr_cb_factor(baseline["name"], opponent, wr_cb_rows)
                              if pos in ("WR", "TE") else (1.0, ""))

    out = {
        "name": baseline["name"], "team": baseline["team"], "position": pos,
        "player_id": baseline["player_id"], "id_resolved": baseline["id_resolved"],
        "opponent": opponent,
        "script_factor": round(script, 3),
        "wr_cb_note": wr_cb_note,
        "rz_note": red_zone_note(baseline["player_id"], rz) if baseline["player_id"] else "",
        "props": {},
    }

    for prop, (_, spec_pos, stat) in PROP_SPECS.items():
        season_total = baseline.get(prop, 0.0)
        if season_total <= 0:
            continue
        # Position-specific props only apply to that position; receiving props
        # use the player's own position group so a RB's receptions are judged
        # against how the defense handles RBs, not WRs.
        if prop in ("pass_yds", "pass_tds") and pos != "QB":
            continue
        if prop == "rush_yds" and pos not in ("RB", "QB"):
            continue
        if prop in ("rec_yds", "receptions") and pos not in ("RB", "WR", "TE"):
            continue
        group = spec_pos or pos
        if prop == "rush_yds" and pos == "QB":
            group = "RB"  # no separate QB-rush split; RB rushing is the closest proxy

        mf = matchup_factor(dvp, opponent, group, stat)
        base = season_total / EXPECTED_GAMES_PLAYED
        value = base * script * mf
        if prop in ("rec_yds", "receptions"):
            value *= wr_cb_mult

        out["props"][prop] = {
            "projection": round(value, 1),
            "baseline_per_game": round(base, 1),
            "matchup_factor": round(mf, 3),
        }

    # ── Anytime TD (Poisson) ─────────────────────────────────────────────────
    # Expected TDs for this player this game, then P(at least one). Poisson is
    # the standard model for scoring counts: P(>=1) = 1 - e^-lambda.
    # Base lambda comes from the consensus TD projections (rush + receiving),
    # which already encode role and goal-line usage, then gets the same script
    # and matchup treatment as everything else.
    season_tds = baseline.get("rush_tds", 0.0) + baseline.get("rec_tds", 0.0)
    if season_tds > 0:
        td_group = "RB" if pos in ("RB", "QB") else pos
        rush_mf = matchup_factor(dvp, opponent, "RB" if pos != "WR" else "WR", "rush_td")
        rec_mf = matchup_factor(dvp, opponent, td_group, "rec_td")
        # Weight the two matchup factors by how this player actually scores
        rush_share = baseline.get("rush_tds", 0.0) / season_tds
        td_mf = rush_mf * rush_share + rec_mf * (1 - rush_share)

        lam = (season_tds / EXPECTED_GAMES_PLAYED) * script * td_mf
        if pos in ("WR", "TE"):
            lam *= wr_cb_mult
        out["props"]["anytime_td"] = {
            "projection": round(1 - math.exp(-lam), 4),  # probability, not a count
            "baseline_per_game": round(season_tds / EXPECTED_GAMES_PLAYED, 3),
            "matchup_factor": round(td_mf, 3),
            "expected_tds": round(lam, 3),
        }

    return out


# ── Slate driver ──────────────────────────────────────────────────────────────
def project_slate(gc, games_by_id: dict, team_stats: dict, rest_lookup: dict,
                  weather_by_game: dict, season: int = 2025) -> tuple[list, dict]:
    """
    Project every skill player on both sides of every game on the slate.

    Returns (projections, diagnostics). Diagnostics carries the counts that
    matter for trusting the run — how many players failed to resolve to an id,
    and whether the WR-CB PDF actually parsed.
    """
    print("  Loading matchup data (defense-vs-position, red zone, shares) ...")
    dvp = props_data.load_defense_vs_position(season)
    rz = props_data.load_red_zone_splits(season)
    name_map = props_data.build_name_to_id()
    ambiguous = name_map.pop("_ambiguous", {})

    wr_cb_rows = props_data.load_wr_cb_matchups()
    print(f"  WR-CB matchup rows: {len(wr_cb_rows)}"
          + ("" if wr_cb_rows else "  (no weekly PDF present — using position-group matchups only)"))

    baselines = load_organic_baselines(gc, name_map)
    unresolved = [b["name"] for b in baselines.values() if not b["id_resolved"]]
    print(f"  Organic baselines: {len(baselines)} players "
          f"({len(unresolved)} unmatched to a player id)")

    # team -> list of that team's baselines, for fast per-game lookup
    by_team = {}
    for b in baselines.values():
        by_team.setdefault(b["team"], []).append(b)

    projections = []
    for game_id, g in games_by_id.items():
        home = edges.TEAM_NAME_TO_ABBR.get(g["home_team"])
        away = edges.TEAM_NAME_TO_ABBR.get(g["away_team"])
        if not home or not away:
            continue
        proj = edges.project_game_score(home, away, team_stats, rest_lookup,
                                        weather_by_game, game_id)
        if not proj:
            continue

        for team, opp, team_score in ((home, away, proj["proj_home"]),
                                      (away, home, proj["proj_away"])):
            baseline_ppg = team_stats.get(team, {}).get("off_rating")
            for b in by_team.get(team, []):
                p = project_player_props(b, opp, team_score, baseline_ppg,
                                         dvp, rz, wr_cb_rows)
                p["game_id"] = game_id
                p["game"] = f"{g['away_team']} @ {g['home_team']}"
                p["commence_time"] = g.get("commence_time", "")
                p["proj_team_score"] = team_score
                projections.append(p)

    diagnostics = {
        "players_projected": len(projections),
        "unresolved_names": unresolved,
        "ambiguous_names": ambiguous,
        "wr_cb_rows": len(wr_cb_rows),
    }
    return projections, diagnostics
