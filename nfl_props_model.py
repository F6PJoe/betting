"""
nfl_props_model.py — Player prop projection engine.

Turns (player, opponent, game context) into a projected stat line for each of
the seven prop categories the model covers:
    QB pass yards, QB pass TDs, RB rush yards, RB reception yards,
    WR/TE reception yards, WR/TE receptions, anytime TD

IMPORTANT: this module never touches the Odds API — it only ever reads rows
that were already fetched, so nothing here can spend a credit. project_slate()
needs no book lines at all and can be built and validated for free.
calibrate_to_market() and analyze_player_props() do read posted lines: the
first to re-level our projections against the market, the second to price the
edge. Both take those lines as an argument; neither goes and gets them.

Data sources, all free:
  - organic consensus sheet (Joe Bond's) -> per-player season baseline
  - nfl_props_data.load_defense_vs_position() -> opponent matchup
  - nfl_props_data.load_red_zone_splits()     -> TD context
  - nfl_props_data.load_player_shares()       -> role/volume context
  - nfl_analyze_edges.project_game_score()    -> game script
  - nfl_props_data.load_wr_cb_matchups()      -> weekly ESPN shadow-coverage PDF
"""

import math
import statistics

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
# RETIRED 2026-09-25 — no longer used anywhere in the projection path. The
# weekly sheet supplies PER-GAME numbers, so there is nothing to divide. Kept
# only so the reasoning below stays readable and nobody reintroduces a divisor
# out of habit. If a future source reverts to season totals, that source must
# carry its own divisor; do not resurrect this one.
EXPECTED_GAMES_PLAYED = None

# ASSUMPTION STILL OPEN: the user confirmed the organic sheet refreshes all
# season. If those refreshes become REST-OF-SEASON totals once games are played
# (the usual in-season fantasy convention), this divisor must become games
# REMAINING — otherwise every projection silently shrinks week over week.
# Cannot be settled until the season starts and we see a refreshed copy.
# CHECK IN WEEK 2 and fix immediately if so.

# ── Per-stat consensus calibration ───────────────────────────────────────────
# The organic sheet's season totals sit at a different LEVEL from the market,
# differently per stat, so a per-game baseline has to be re-levelled before any
# per-player disagreement means anything.
#
# WHY THIS IS SEPARATE FROM EXPECTED_GAMES_PLAYED: back-solving a divisor from
# each ratio gives a DIFFERENT answer per stat (15.9 / 16.5 / 17.6 / 18.0 /
# 18.7 when first measured). If the whole effect were games-played, every stat
# would imply the SAME divisor — a player plays the same games whether you are
# counting his yards or his catches. It doesn't, so there are two distinct
# effects stacked on top of each other:
#   1. games played  -> EXPECTED_GAMES_PLAYED (15.5), roughly right.
#   2. the consensus sheet being differentially levelled by stat.
# Only (2) belongs here.
#
# WHY CALIBRATING TO THE MARKET IS NOT CIRCULAR: this corrects the LEVEL only.
# Per-player deviations survive untouched, so the model can still disagree with
# the book about individual players — which is where any real edge lives. A
# model that is uniformly 20% off is not finding edges, it is just biased, and
# that bias has to come out before a per-player disagreement means anything.
#
# *** THESE ARE FALLBACKS ONLY. *** The live numbers are measured at RUNTIME by
# calibrate_to_market() below; these apply only when a stat has too few posted
# markets to measure (early-week runs), or when the measurement fails its
# sanity band.
#
# WHY RUNTIME AND NOT A CONSTANT: the organic sheet refreshes on its own — it
# carries its own 'Update Date' tab. On 2026-09-08 at 4:01 AM it refreshed and
# every total dropped 12-15% (Pass Yds 0.881x, Pass Att 0.858x, Pass TD 0.862x,
# Rush Yds 0.846x vs. the sheet's own pre-refresh copy). The constants below
# had been measured five days earlier, so within one refresh the median
# projection/line went 1.000 -> 0.889 and the prop board went 94% Unders — 134
# Unders against 8 Overs, none of them real. A hardcoded level correction
# against an input that re-levels itself weekly is stale the moment it lands;
# measuring it each run is the only version that cannot silently drift.
#
# RESET TO NEUTRAL 2026-09-25 with the switch to per-game weekly projections.
# The previous values (0.906-1.074) were measured against SEASON totals divided
# by EXPECTED_GAMES_PLAYED — a completely different basis, so carrying them over
# would apply a correction derived for arithmetic the model no longer does.
# Neutral is the honest starting point; calibrate_to_market() measures the real
# level against posted lines on the first run and every run after.
PROP_CALIBRATION = {
    "pass_yds":   1.0,
    "pass_tds":   1.0,
    "rush_yds":   1.0,
    "rec_yds":    1.0,
    "receptions": 1.0,
}

# Anytime TD is deliberately NOT calibrated — see the block in
# calibrate_to_market(). It was measured unbiased at +0.39pp mean edge over
# ~253 markets on 08-30, and still reads -0.02pp on the same metric after the
# 09-08 sheet refresh. This records that baseline so a genuine future drift has
# something to be compared against, rather than being re-derived from memory.
# Measured over role-gated players at the best available book, which is the
# population and the pricing analyze_player_props actually bets.
TD_BASELINE_PP = 0.39

# A median needs a real sample behind it. Below this many matched markets for a
# stat we keep the fallback rather than re-levelling the whole slate off a
# handful of players — early-week runs post only a few markets per stat.
#
# WHY 15 AND NOT HIGHER: the passing stats have a hard ceiling of one starting
# QB per team, so a full slate posts about 32 pass_yds markets and fewer after
# the role gate. A threshold in the 25-30 range would read as "safely
# conservative" and in practice permanently disable calibration for the two
# stats that cannot ever produce a big sample.
MIN_CALIBRATION_SAMPLE = 15

# A correction outside this band is not a calibration signal, it is a broken
# input (sheet half-written, a column renamed, wrong season). Re-levelling the
# model to match it would hide exactly the failure we most need to see, so we
# keep the fallback and shout instead. The 09-08 refresh needed 0.89 — well
# inside — so this only trips on something genuinely wrong.
CALIBRATION_SANITY_BAND = (0.75, 1.35)

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

# Hard bound on the script factor, applied AFTER damping.
#
# WHY (2026-09-03): the factor is a RATIO of this game's projected team score
# to that team's own baseline, so it inflates whenever the two are far apart.
# Opponent-adjusting the ratings — correctly — made this worse for NYJ@TEN,
# because it lowered TEN's baseline (18.35 -> 16.55, their scoring had been
# flattered by weak defences) while their projected score barely moved. The
# raw ratio went 1.30 -> 1.41, lifting every Titans prop ~29% and putting
# three Cam Ward lines in the top ten of the board.
#
# Damping alone cannot bound a ratio; it only shrinks it proportionally. The
# honest statement is a claim about football, not arithmetic: no single
# matchup makes a player's expected output swing more than ~20%. Some of the
# team-level scoring swing is distribution between players rather than a lift
# for all of them, which SCRIPT_DAMPING already partly reflects — this caps
# what is left.
SCRIPT_FACTOR_CAP = 0.20   # clamp to [0.80, 1.20]

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
LEAGUE_AVG_ALLOWED_FR = 0.28   # fallback; the sheet's own weekly mean is preferred
WR_CB_MAX_ADJ = 0.15   # generous vs. the old sign-only hack; observed range was -8%..+14%

# ── Small-sample shrinkage on the defender's allowed rate ────────────────────
# MEASURED 2026-09-21, and the reason this input is not simply switched on.
#
# The sheet changes what it means mid-season. Pre-season it carries each
# defender's FULL PRIOR SEASON (~386 coverage routes, allowed F/R sd 0.068);
# once games are played it switches to SEASON-TO-DATE, so in September each
# number rests on one game (~28 routes, sd 0.221). Same column, wildly
# different reliability, and nothing in the sheet says so.
#
# Those two sheets measure the SAME 77 defenders, which is a direct reliability
# test: correlation between a defender's one-game number and his own full-season
# number is -0.12. A one-game allowed F/R carries no usable signal at all.
#
# Splitting the variance (observed = true + noise/n) over those two samples:
#   noise variance   1.335        true between-defender variance  0.0012
#   -> k = noise/true = ~1,145 routes, rounded to 1,100 below.
# Weight on the observed number is routes/(routes + k):
#   28 routes (one game)      3%      386 routes (a full season)   26%
# So in September the factor sits near neutral and earns influence only as real
# coverage volume accumulates, which is exactly the intent.
#
# THAT 26% CEILING IS NOT A BUG. It says most of the spread between defenders
# in allowed F/R is noise even across a full season (true sd ~0.034 against an
# observed 0.068) — consistent with the known confound that this stat barely
# separates good corners from bad, since a corner who travels with WR1s is
# measured against the league's best receivers. Do NOT raise this to make the
# adjustment "do more"; re-derive it from a fresh reliability test instead.
WR_CB_SHRINK_ROUTES = 1100

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
# The weekly sheet abbreviates three teams differently from nflverse. Same
# class of bug as the WR-CB sheet's ARZ/BLT/CLV/HST/LAR: team codes are matched
# by exact string, so an unmapped code silently drops that team's players.
# Derived by diffing the two code sets, not guessed.
ORGANIC_TEAM_FIXES = {"LAR": "LA", "WSH": "WAS", "JAC": "JAX", "KCC": "KC"}

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
def load_organic_baselines(gc, name_map: dict, opponents: dict | None = None) -> dict:
    """
    Read the weekly projections sheet into PER-GAME baselines keyed by gsis
    player_id, with the raw name kept for reporting.

    SOURCE CHANGED 2026-09-25 (owner): from "Draft Fantasy Football Projections"
    (season totals, last refreshed 09-09) to "Weekly Fantasy Football
    Projections" (per-game, refreshed weekly). This is strictly better:
      * PER-GAME numbers remove EXPECTED_GAMES_PLAYED from the model entirely.
        That divisor was known to be imprecise, differed by stat, and carried a
        standing risk that the source would switch to rest-of-season totals and
        silently shrink every projection week over week. Gone.
      * Refreshed weekly, so it finally reflects injuries and role changes. The
        old sheet had been frozen since five minutes into the Week 1 opener.
      * Carries an Opp column, so a stale sheet can be DETECTED rather than
        quietly projecting last week's matchups.
    Validated on switch: 99.3% name join (453/456), team agrees with nflverse
    rosters on all 453, all 16 of that week's matchups present, and 94.4% of
    book-priced players covered (the misses are fullbacks and deep backups the
    role gate discards anyway).

    Players who can't be resolved to an id are still returned (keyed by their
    normalized name) rather than dropped — a book may well offer a prop on
    someone nflverse hasn't rostered yet, and silently losing them is exactly
    the failure mode that broke MLB props. `id_resolved` marks which is which.

    `opponents` is {team_abbr: opponent_abbr} for the slate being priced. When
    given, a row whose Opp disagrees is SKIPPED: that means the sheet has not
    been refreshed for this week, and last week's matchup is not a projection
    for this one. Silence is the danger here — see the WR-CB sheet, which sat
    stale for two weeks contributing nothing while reporting itself fine.
    """
    sh = gc.open_by_key(edges.WEEKLY_PROJ_SHEET_ID)
    out = {}
    stale = 0
    for pos, tab in (("QB", "LIVE PROJECTIONS QB"), ("RB", "LIVE PROJECTIONS RB"),
                     ("WR", "LIVE PROJECTIONS WR"), ("TE", "LIVE PROJECTIONS TE")):
        try:
            rows = edges.sheet_to_dicts(sh.worksheet(tab))
        except Exception as e:
            print(f"  [warn] projections tab '{tab}' unreadable: {e}")
            continue
        for row in rows:
            name = (row.get(pos) or "").strip()
            if not name:
                continue
            team = (row.get("Team") or "").strip().upper()
            team = ORGANIC_TEAM_FIXES.get(team, team)
            opp = ORGANIC_TEAM_FIXES.get((row.get("Opp") or "").strip().upper(), "")
            if opponents and team in opponents and opp and opponents[team] != opp:
                stale += 1
                continue
            pid = props_data.resolve_player_id(name, name_map)
            key = pid or f"name:{props_data.normalize_name(name)}"
            out[key] = {
                "opponent_sheet": opp,
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
    if stale:
        print(f"  [check] {stale} projection row(s) skipped — their Opp disagrees "
              f"with this week's schedule, i.e. the sheet is not refreshed")
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
    f = _damped(proj_team_score / team_baseline_ppg, SCRIPT_DAMPING)
    return max(1 - SCRIPT_FACTOR_CAP, min(1 + SCRIPT_FACTOR_CAP, f))


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

        # Shrink toward the league average by how many coverage routes actually
        # back this number — see WR_CB_SHRINK_ROUTES. The baseline is the
        # SHEET'S OWN mean where available rather than a hardcoded constant,
        # because the sheet re-derives its league averages every week (measured:
        # 0.650 pre-season vs 0.670 after Week 1).
        league_fr = row.get("_league_fr") or LEAGUE_AVG_ALLOWED_FR
        try:
            routes = float(row.get("cov_routes") or 0)
        except (TypeError, ValueError):
            routes = 0.0
        weight = routes / (routes + WR_CB_SHRINK_ROUTES)
        def_fr = weight * def_fr + (1 - weight) * league_fr

        # How often these two actually face each other. Offense's left side
        # lines up against the defense's right, hence LWR<->RCB / RWR<->LCB.
        exposure = (_pct(row.get("lwr_pct")) * _pct(row.get("rcb_pct"))
                    + _pct(row.get("slot_pct")) * _pct(row.get("def_slot_pct"))
                    + _pct(row.get("rwr_pct")) * _pct(row.get("lcb_pct")))
        if exposure <= 0:
            return 1.0, ""

        # Defender half of ESPN's matchup formula only — the receiver half is
        # already in our baseline (see the block comment above).
        defender_delta = (def_fr - league_fr) / league_fr
        adj = max(-WR_CB_MAX_ADJ, min(WR_CB_MAX_ADJ, exposure * defender_delta))
        defender = row.get("defender", "?")
        return 1.0 + adj, (f"vs {defender} ({exposure*100:.0f}% of routes, "
                           f"{def_fr:.2f} F/R allowed, {weight*100:.0f}% weight "
                           f"on {routes:.0f} routes) {adj*100:+.1f}%")
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

    projection = per-game baseline x script_factor x matchup_factor
    and, for pass-catchers, x wr_cb_factor.

    The baseline arrives PER GAME from the weekly sheet (see
    load_organic_baselines), so there is no games-played divisor any more.

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
        per_game = baseline.get(prop, 0.0)
        if per_game <= 0:
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
        # Already per game — no games-played divisor. PROP_CALIBRATION is a
        # level correction only (relative differences between players are
        # preserved) and is now just a fallback: calibrate_to_market() measures
        # the real one against the posted lines every run.
        base = per_game / PROP_CALIBRATION.get(prop, 1.0)
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
    game_tds = baseline.get("rush_tds", 0.0) + baseline.get("rec_tds", 0.0)
    if game_tds > 0:
        td_group = "RB" if pos in ("RB", "QB") else pos
        rush_mf = matchup_factor(dvp, opponent, "RB" if pos != "WR" else "WR", "rush_td")
        rec_mf = matchup_factor(dvp, opponent, td_group, "rec_td")
        # Weight the two matchup factors by how this player actually scores
        rush_share = baseline.get("rush_tds", 0.0) / game_tds
        td_mf = rush_mf * rush_share + rec_mf * (1 - rush_share)

        # game_tds is already this game's expected TDs, so it IS lambda.
        lam = game_tds * script * td_mf
        if pos in ("WR", "TE"):
            lam *= wr_cb_mult
        out["props"]["anytime_td"] = {
            "projection": round(1 - math.exp(-lam), 4),  # probability, not a count
            "baseline_per_game": round(game_tds, 3),
            "matchup_factor": round(td_mf, 3),
            "expected_tds": round(lam, 3),
        }

    return out


# ── Prop edge analysis ────────────────────────────────────────────────────────
# Game-to-game standard deviation as a linear function of a player's mean:
#     sd = slope * mean + intercept
# FITTED FROM REAL DATA 2026-08-30 — nflverse player game logs, 2019-2024 REG,
# players with 8+ games in a season. Fits are strong (r = 0.76 to 0.90 across
# 5,400+ player-seasons), so this is measured, not assumed.
#
# This is what lets a projection become a PROBABILITY instead of a raw point
# gap: "12 yards above the line" means something completely different for a
# 250-yard passing prop (sd ~79) than a 4-reception prop (sd ~2.1). Converting
# both to P(over) puts every prop type on one comparable scale.
SD_MODEL = {
    "pass_yds":   (0.2516, 16.004),
    "pass_tds":   (0.4149, 0.436),
    "rush_yds":   (0.4940, 5.293),
    "rec_yds":    (0.4682, 8.210),
    "receptions": (0.3559, 0.693),
}

# Anytime-TD overround, MEASURED 2026-08-30 rather than assumed:
#   book implied-probability sum per game (median, 13 game/book combos) = 4.571
#   actual distinct offensive TD scorers per game (2025, 283 games)     = 4.131
#   => overround = 1.107
# Anytime TD is NOT a mutually exclusive market — several players score in a
# game — so the usual "divide by the sum" de-vig does not apply. Books also
# only post the "Yes" side, so there is no two-way price to de-vig against.
# A flat proportional divisor is the defensible middle ground.
# CAVEAT: assumes vig is spread proportionally, which understates the juice on
# longshots (favourite-longshot bias). Re-measure once a few weeks of closing
# ATD prices exist.
ANYTIME_TD_OVERROUND = 1.107

# Prop unit scale, in PERCENTAGE POINTS of win probability — same currency as
# the moneyline scale so the star ratings mean the same thing across bet types.
# Slightly tighter at the top than ML because prop markets are softer and throw
# off bigger nominal edges; a 20-point "edge" on a prop is far more likely to be
# a modelling error than a real one. Year-1 estimate, recalibrate with results.
# RAISED 2026-09-03 from a 4.0pp floor after the owner asked whether ~500
# tracked bets could possibly be real. It could not:
#   * at 4pp the model bet 26 props PER GAME — 416/week, ~7,500 a season
#   * the MEDIAN market disagrees with the book by 5.0pp, so a 4pp floor was
#     betting BELOW the model's own noise floor: most "edges" were the model
#     being imprecise, not the book being wrong
# 15pp is 3x that noise floor and yields ~3 bets/game (50/week, ~900/season),
# proportionate to the ~38 game-level bets/week.
#
# This is a VOLUME-based choice, not a validated one — no prop has been graded
# yet. The Projection Log records ALL priced markets with their edges every
# run, so once Week 1-4 results exist the threshold can be re-cut on evidence
# (which edge buckets actually produced positive CLV and ROI) rather than on
# judgement. Expect to revisit it.
PROP_SCALE = [
    (15.0, 0.3), (17.5, 0.4), (20.0, 0.5), (22.0, 0.6),
    (24.0, 0.7), (26.0, 0.8), (28.0, 0.9), (30.0, 1.0),
]

# ANYTIME TD NEEDS ITS OWN SCALE. A single percentage-POINT threshold is not
# neutral across base rates, and anytime TD sits on a completely different one:
#     yardage/reception props  median model P ~57%,  median |edge| 5-9pp
#     anytime TD               median model P ~20.5%, median |edge| 2.3pp
# A 15pp bar is ~6.5x anytime TD's own noise floor and excluded it ENTIRELY
# (0 of 256 markets qualified), which would have silently dropped the one
# market the owner called non-negotiable.
#
# 7pp is ~3x its 2.3pp noise floor — the same stringency the 15pp bar applies
# to the ~57% markets — and yields ~26 bets/week (~1.6/game).
ANYTIME_TD_SCALE = [
    (7.0, 0.3), (8.5, 0.4), (10.0, 0.5), (11.5, 0.6),
    (13.0, 0.7), (14.5, 0.8), (16.0, 0.9), (18.0, 1.0),
]


def scale_for(prop: str):
    """Anytime TD is graded on its own scale — see ANYTIME_TD_SCALE."""
    return ANYTIME_TD_SCALE if prop == "anytime_td" else PROP_SCALE

# ── Prop TRACKING gate (separate from the fetch gate) ────────────────────────
# Prop odds are still FETCHED and still analysed — we need them on the board to
# re-derive the divisors — but qualifying props are NOT written to Bet History
# while this is False.
#
# WHY: EXPECTED_GAMES_PLAYED is known to be mis-levelled (see the block above).
# Bet History FREEZES the first qualifying entry, so any prop written now would
# lock a biased line in as the Primary — the row that grades and that the W/L
# record counts. That would permanently poison Week 1's prop calibration, and
# with only ~18 weeks in a season that is an expensive week to waste.
#
# FLIP TO TRUE once the per-stat divisors are re-derived against all 16 games'
# full prop menus (owner agreed 2026-08-30 to do that a few days out, before
# Week 1 — not to wait for in-season data, which does NOT fix a level bias).
# ENABLED 2026-09-03. Unblocked by three things landing together:
#   1. PROP_CALIBRATION removed the level bias (all five stats now 1.000)
#   2. prop lines are now written to the Line Log -> closing capture works
#   3. grade_prop() handles all six prop types, incl. Void for inactives
PROPS_TRACKING_ENABLED = True

# Odds API market key -> our internal prop key
MARKET_TO_PROP = {
    "player_pass_yds": "pass_yds",
    "player_pass_tds": "pass_tds",
    "player_rush_yds": "rush_yds",
    "player_reception_yds": "rec_yds",
    "player_receptions": "receptions",
    "player_anytime_td": "anytime_td",
}

PROP_LABEL = {
    "pass_yds": "Pass Yds", "pass_tds": "Pass TDs", "rush_yds": "Rush Yds",
    "rec_yds": "Rec Yds", "receptions": "Receptions", "anytime_td": "Anytime TD",
}


# ── Role-mismatch gate ───────────────────────────────────────────────────────
# If our projection is wildly away from the book's line, we are not disagreeing
# about a MATCHUP — we are disagreeing about the player's ROLE, and the book is
# far better informed about that than a season total is.
#
# WHY THIS EXISTS (found 2026-09-03): the biggest "edges" in the model were all
# backups. Kirk Cousins projected 69 pass yards against a 209.5 line (50pp
# "edge"); Deshaun Watson 111 vs 178.5; George Holani 9.2 vs 20.5. The cause is
# structural: the consensus sheet gives a backup a SEASON total reflecting a
# partial role (say 1,000 yards because he is expected to play ~5 games), and
# dividing by EXPECTED_GAMES_PLAYED assumes he plays 15.5. But a book only
# posts a prop line for a player it expects to PLAY. So every backup with a
# posted line manufactured a huge fake Under edge.
#
# Raising the star threshold would have made this WORSE, not better — these
# were the LARGEST edges in the system, so a tighter filter would have kept
# almost nothing but backups.
#
# Band chosen from the model's own mechanics, not from percentile-fitting:
# MATCHUP_DAMPING (0.5) and SCRIPT_DAMPING (0.7) applied to factors that
# average ~1.00 cannot move a projection more than roughly +/-20% off baseline.
# Anything beyond ~+/-35% therefore lives in the BASELINE, i.e. the role.
# Measured effect: markets inside 0.80-1.25 average 7.0pp disagreement;
# those below 0.60 average 41.5pp.
#
# TIGHTENED 2026-09-08 from (0.70, 1.45), on the owner's call. Two reasons.
#
# First, the original numbers never matched the reasoning directly above them:
# that argument lands on +/-35%, which is 0.65-1.35, but the band was written
# as -30%/+45%. The upper bound in particular was looser than anything here
# justifies.
#
# Second, once the 09-08 level bias was removed (see calibrate_to_market) the
# shape of what remained was visible, and 53 of 93 qualifying edges — 57% —
# came from disagreeing with the book by more than 15% about a player's LEVEL
# rather than his matchup. They were the top of the board, not the tail of it:
# Jacoby Brissett Under 226.5 at ratio 0.77 for a 30.9pp "edge" was the single
# biggest play on the slate, with Jadarian Price (0.71) and three separate
# Brissett lines behind it. Same failure as the Kirk Cousins case that created
# this gate, just landing at a magnitude that slipped under a 0.70 floor.
#
# 0.80 is where the damping argument runs out: a projection more than 20% off
# its own baseline cannot have got there through matchup and script, so it came
# from the baseline. 1.35 keeps the ceiling symmetric with the +/-35% logic.
# RE-DERIVE against graded results after Week 4 — a band set from mechanics is
# a starting point, and real Win/Loss on these is the stronger anchor.
ROLE_MISMATCH_BAND = (0.80, 1.35)


def prop_sd(prop: str, mu: float) -> float | None:
    spec = SD_MODEL.get(prop)
    if not spec or mu is None:
        return None
    slope, inter = spec
    return max(0.5, slope * mu + inter)


def _is_team_entry(name: str) -> bool:
    """Book lists team defenses in the anytime-TD market; we don't model those."""
    n = str(name)
    return "D/ST" in n or n.endswith(" Defense")


def _market_lines(prop_rows: list[dict]) -> tuple[dict, dict]:
    """(prop, player) -> median posted line, and player -> [anytime-TD prices]."""
    lines, td_prices = {}, {}
    for r in prop_rows:
        prop = MARKET_TO_PROP.get(str(r.get("market_key")))
        if not prop:
            continue
        player = str(r.get("player", "")).strip()
        if not player or _is_team_entry(player):
            continue
        if prop == "anytime_td":
            if str(r.get("direction")) == "Yes":
                try:
                    td_prices.setdefault(player, []).append(float(r.get("price")))
                except (TypeError, ValueError):
                    pass
            continue
        try:
            lines.setdefault((prop, player), []).append(float(r.get("point")))
        except (TypeError, ValueError):
            pass
    return ({k: statistics.median(v) for k, v in lines.items() if v}, td_prices)


def calibrate_to_market(projections: list[dict], prop_rows: list[dict]) -> dict:
    """
    Re-level our projections against the posted market, per stat, IN PLACE.

    See the PROP_CALIBRATION block for why this is a runtime measurement rather
    than a constant: the organic sheet re-levels itself on refresh, and a
    hardcoded correction goes stale silently the moment it does.

    Median, not mean, for the yardage stats: a handful of backups whose
    consensus totals reflect a partial role would drag a mean badly. The
    median ignores them.

    CALIBRATED ON THE POPULATION WE ACTUALLY BET — the same ROLE_MISMATCH_BAND
    that analyze_player_props applies is applied here. Measuring over every
    posted market instead gets this wrong in a way that matters: the anytime-TD
    menu carries ~170 deep backups and longshots that the gate throws out, and
    calibrating across all 407 rather than the 237 we would bet turned a TD
    book that was already unbiased (-0.02pp) into +3.42pp the other way. A
    level correction has to be measured where it will be spent.

    ITERATED, because that gate makes a single pass under-correct exactly when
    correction matters most. If the sheet re-levels DOWN, the lowest players
    fall out of the bottom of the band, so the survivors read high and the
    measured correction comes up short. Measured on a simulated -20% refresh: a
    single pass recovered the median only to 0.951 and still left 80% of the
    board on Unders. Re-measuring after each pass lets the gate population
    settle, and it converges in two or three.

    Returns a per-stat report — measured factor, sample size, and what was
    actually applied — for the run log and the Pipeline Health tab.
    """
    lines, td_prices = _market_lines(prop_rows)
    name_map = props_data.build_name_to_id()
    name_map.pop("_ambiguous", None)

    by_pid = {p["player_id"]: p for p in projections if p.get("player_id")}
    td_pairs = []

    # Resolve names ONCE — it is the expensive part, and iterating must not
    # repeat it. Ungated here; the gate is applied per pass, since which
    # players clear it changes as the level moves.
    matched = {}
    for (prop, player), line in lines.items():
        pid = props_data.resolve_player_id(player, name_map)
        proj = by_pid.get(pid) if pid else None
        if not proj or prop not in proj.get("props", {}) or not line:
            continue
        matched.setdefault(prop, []).append((proj["props"][prop], float(line)))

    for player, prices in td_prices.items():
        pid = props_data.resolve_player_id(player, name_map)
        proj = by_pid.get(pid) if pid else None
        if not proj or "anytime_td" not in proj.get("props", {}):
            continue
        fairs = [edges.american_to_implied(px) / ANYTIME_TD_OVERROUND for px in prices]
        mu = float(proj["props"]["anytime_td"]["projection"])
        # Gate on the MEAN price across books but score against the BEST one —
        # exactly what analyze_player_props does, so the number reported here
        # is the same number the edges are actually priced off.
        mean_fair = statistics.fmean(fairs)
        if 0 < mean_fair < 1 and mu > 0:
            if ROLE_MISMATCH_BAND[0] <= mu / mean_fair <= ROLE_MISMATCH_BAND[1]:
                td_pairs.append((mu, min(fairs)))

    report = {}
    lo, hi = CALIBRATION_SANITY_BAND

    # ── yardage / reception stats: projection scales linearly ────────────────
    for prop in ("pass_yds", "pass_tds", "rush_yds", "rec_yds", "receptions"):
        pairs = matched.get(prop, [])
        fallback = PROP_CALIBRATION.get(prop, 1.0)

        def gated_median():
            v = [d["projection"] / line for d, line in pairs
                 if line and ROLE_MISMATCH_BAND[0] <= d["projection"] / line
                 <= ROLE_MISMATCH_BAND[1]]
            return (float(statistics.median(v)) if v else None), len(v)

        m, n = gated_median()
        if n < MIN_CALIBRATION_SAMPLE:
            report[prop] = {"n": n, "measured": None, "applied": 1.0,
                            "status": "fallback (too few markets)"}
            continue
        if not (lo <= m <= hi):
            report[prop] = {"n": n, "measured": round(m, 3), "applied": 1.0,
                            "status": f"REJECTED — outside {lo}-{hi}, kept fallback"}
            continue

        # Divide the level out: after this the median projection sits on the
        # median line, and every per-player deviation around it is preserved.
        # Repeat until the gate population stops moving — see the docstring.
        # CONVERGENCE TOLERANCE 0.002: projections are stored to one decimal,
        # so chasing below ~0.2% is chasing rounding, not level.
        total, first, passes = 1.0, m, 0
        for _ in range(5):
            passes += 1
            for p in projections:
                d = p.get("props", {}).get(prop)
                if d:
                    d["projection"] = round(d["projection"] / m, 1)
                    d["baseline_per_game"] = round(d["baseline_per_game"] / m, 1)
            total *= m
            m, n = gated_median()
            if m is None or n < MIN_CALIBRATION_SAMPLE or abs(m - 1.0) <= 0.002:
                break
        # Each pass was individually inside the sanity band, but a compounded
        # correction can still land outside it. That is not necessarily wrong —
        # it is what a large genuine re-level looks like — so it is flagged for
        # review rather than discarded, which would leave the board skewed.
        status = ("measured" if lo <= total <= hi
                  else f"measured — compounded to {total:.3f}, outside {lo}-{hi}, REVIEW")
        report[prop] = {"n": n, "measured": round(first, 3),
                        "applied": round(1 / total, 3), "status": status,
                        "passes": passes, "residual": round(m, 4) if m else None,
                        "effective_constant": round(fallback * total, 3)}

    # ── anytime TD: MEASURED AND REPORTED, DELIBERATELY NOT APPLIED ──────────
    # TDs were left uncorrected when the yardage calibration was first built,
    # on a measurement of +0.39pp mean edge over ~253 markets (08-30). That
    # baseline was taken over the players that clear the role gate, priced at
    # the BEST book — the same population and pricing analyze_player_props
    # uses. On that metric of record TDs read -0.02pp on 09-08: unchanged, and
    # still unbiased. So there is nothing here to correct.
    #
    # WHY THIS IS WORTH SPELLING OUT: the same slate reads -3.16pp measured
    # over every posted TD market against the mean price across books. That
    # number is real but it is not the same quantity — the TD menu carries
    # ~170 deep backups and longshots the role gate discards, and they sit
    # where a Poisson built off season TD totals disagrees with the book by
    # construction. Reading it as drift and "fixing" it moved a book that was
    # sitting at -0.02pp to +1.17pp and took the slate from 7 anytime-TD bets
    # to 19. Two populations, two answers, and only one of them is the
    # population we bet.
    #
    # The residual best-book edge after any level correction is line-shopping
    # value, which is real and should be kept, not calibrated away. So this
    # measures on the metric of record and reports it; if a future sheet
    # refresh genuinely moves TDs, THRESHOLD is where that becomes visible.
    TD_REPORT_THRESHOLD_PP = 2.0
    if len(td_pairs) < MIN_CALIBRATION_SAMPLE:
        report["anytime_td"] = {"n": len(td_pairs), "measured": None,
                                "applied": 1.0, "status": "not enough markets to measure"}
    else:
        bias_pp = statistics.fmean([(p - f) * 100 for p, f in td_pairs])
        drifted = abs(bias_pp - TD_BASELINE_PP) > TD_REPORT_THRESHOLD_PP
        report["anytime_td"] = {
            "n": len(td_pairs), "measured": round(bias_pp, 2), "applied": 1.0,
            "bias_pp": round(bias_pp, 2),
            "status": (f"DRIFTED from {TD_BASELINE_PP:+}pp baseline — review"
                       if drifted else "measured, unbiased — no correction applied")}

    return report


def analyze_player_props(prop_rows: list[dict], projections: list[dict]) -> tuple[list, list]:
    """
    Compare book prop lines against our projections.

    Returns (bet_candidates, diagnostics_rows).

    Method, uniform across prop types: turn our projection into P(outcome),
    turn the book's price into a vig-free probability, and take the difference
    in percentage points. That keeps a 12-yard edge on a passing prop and a
    0.4-reception edge on a receptions prop on the same comparable scale.
    """
    by_pid = {}
    for p in projections:
        if p.get("player_id"):
            by_pid[p["player_id"]] = p

    name_map = props_data.build_name_to_id()
    name_map.pop("_ambiguous", None)

    # (game_id, prop, player, line) -> {direction: {book: price}}
    grouped = {}
    meta = {}
    for r in prop_rows:
        prop = MARKET_TO_PROP.get(str(r.get("market_key")))
        if not prop:
            continue
        player = str(r.get("player", "")).strip()
        if not player or _is_team_entry(player):
            continue
        try:
            price = float(r.get("price"))
        except (TypeError, ValueError):
            continue
        line = r.get("point")
        line = None if line in ("", None) else float(line)
        key = (str(r.get("game_id")), prop, player, line)
        grouped.setdefault(key, {}).setdefault(str(r.get("direction", "")), {})[
            str(r.get("sportsbook"))] = price
        meta[key] = r

    candidates, diagnostics = [], []
    unmatched = set()
    role_mismatch = []

    for (game_id, prop, player, line), sides in grouped.items():
        pid = props_data.resolve_player_id(player, name_map)
        proj = by_pid.get(pid) if pid else None
        if not proj or prop not in proj.get("props", {}):
            unmatched.add(player)
            continue

        pdata = proj["props"][prop]
        mu = pdata["projection"]
        row = meta[(game_id, prop, player, line)]
        game_label = proj.get("game", "")

        offers = []   # (direction, our_p, fair_p, price, book)

        if prop == "anytime_td":
            # mu IS already a probability here (Poisson P(>=1 TD)).
            fair_probs = [edges.american_to_implied(px) / ANYTIME_TD_OVERROUND
                          for px in sides.get("Yes", {}).values()]
            # Same role gate, on probability rather than a line: a backup with a
            # posted anytime-TD price fails it for the same reason.
            if fair_probs and mu > 0:
                ratio = mu / (sum(fair_probs) / len(fair_probs))
                if not (ROLE_MISMATCH_BAND[0] <= ratio <= ROLE_MISMATCH_BAND[1]):
                    role_mismatch.append((player, prop, round(ratio, 2)))
                    continue
            for book, price in sides.get("Yes", {}).items():
                fair = edges.american_to_implied(price) / ANYTIME_TD_OVERROUND
                offers.append(("Yes", mu, fair, price, book))
        else:
            if line is None:
                continue
            # ROLE GATE — see ROLE_MISMATCH_BAND. Skip rather than bet: the
            # disagreement is about whether this player is even starting.
            ratio = mu / line if line else 1.0
            if not (ROLE_MISMATCH_BAND[0] <= ratio <= ROLE_MISMATCH_BAND[1]):
                role_mismatch.append((player, prop, round(ratio, 2)))
                continue
            sd = prop_sd(prop, mu)
            if sd is None:
                continue
            p_over = 1 - edges.normal_cdf(line, mean=mu, sd=sd)
            for book in set(sides.get("Over", {})) | set(sides.get("Under", {})):
                po, pu = sides.get("Over", {}).get(book), sides.get("Under", {}).get(book)
                if po is None or pu is None:
                    continue   # need both sides at the same book to remove vig
                io_, iu = edges.american_to_implied(po), edges.american_to_implied(pu)
                tot = io_ + iu
                offers.append(("Over", p_over, io_ / tot, po, book))
                offers.append(("Under", 1 - p_over, iu / tot, pu, book))

        if not offers:
            continue

        best = max(offers, key=lambda o: (o[1] - o[2]))
        direction, our_p, fair_p, price, book = best
        edge_pp = (our_p - fair_p) * 100

        diagnostics.append({
            "game_id": game_id, "game": game_label, "player": player, "prop": prop,
            "line": line, "projection": mu, "our_p": round(our_p * 100, 2),
            "fair_p": round(fair_p * 100, 2), "edge_pp": round(edge_pp, 2),
        })

        scale = scale_for(prop)
        if edge_pp < scale[0][0]:
            continue
        units = edges.unit_scale(edge_pp, scale)
        stars = edges.stars_from_units(units)

        side = f"{player} {direction}" if direction != "Yes" else player
        candidates.append({
            "game_id": game_id, "game": game_label,
            "kickoff_et": "", "kickoff_utc": proj.get("commence_time", ""),
            "bet_type": PROP_LABEL[prop], "side": side,
            "bet_on": (f"{player} {direction} {line:g}" if line is not None
                       else f"{player} Anytime TD"),
            "line": line, "price": price, "book": book,
            "stars": stars, "units": units,
            "projection": round(mu, 3) if prop != "anytime_td" else round(mu * 100, 2),
            "edge": round(edge_pp, 2), "edge_pct": round(edge_pp, 2),
            "consensus_line": line, "books_at_line": len(sides.get(direction, {})),
        })

    return candidates, {"diagnostics": diagnostics, "unmatched": sorted(unmatched),
                        "role_mismatch": role_mismatch}


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

    # Who each team actually plays on THIS slate, so a projection row carrying
    # last week's opponent can be spotted and skipped rather than silently used.
    opponents = {}
    for g in games_by_id.values():
        h = edges.TEAM_NAME_TO_ABBR.get(g["home_team"])
        a = edges.TEAM_NAME_TO_ABBR.get(g["away_team"])
        if h and a:
            opponents[h], opponents[a] = a, h

    baselines = load_organic_baselines(gc, name_map, opponents)
    unresolved = [b["name"] for b in baselines.values() if not b["id_resolved"]]
    print(f"  Weekly projections: {len(baselines)} players "
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
