"""
nfl_props_data.py — Matchup-focused data loaders for the NFL player props model.

Free replacements (built from nflverse play-by-play + rosters) for the paid
DFS tools the user already relies on for weekly research:
  - load_defense_vs_position()  ~ DVP / aFPA (fantasy points allowed by position)
  - load_red_zone_splits()      ~ 4for4 red zone stats (passing/rushing/receiving)
  - load_player_shares()        ~ "Share Data" (snap/target/rush share)

The one piece that ISN'T free-replaceable is the ESPN WR-CB shadow-coverage
matchup sheet (individual defender assignments) — that needs a weekly human
upload. load_wr_cb_matchups() reads whatever PDF is sitting at WR_CB_PDF_PATH.

Imported by nfl_analyze_edges.py; not meant to be run standalone.
"""

import os
import pandas as pd
import nfl_data_py as nfl_data

PBP_URL_TMPL = "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.parquet"

# Fixed location for the weekly WR-CB matchup upload — user always saves/
# overwrites this same filename, so the script never has to guess which
# week's file is current.
WR_CB_PDF_PATH = os.path.join(os.path.dirname(__file__), "nfl_wr_cb_matchup_current.pdf")


def _load_pbp(season: int) -> pd.DataFrame:
    return pd.read_parquet(PBP_URL_TMPL.format(season=season), engine="auto")


def _player_positions(season: int) -> dict:
    """{player_id (gsis format, e.g. '00-0034796'): position}."""
    roster = nfl_data.import_seasonal_rosters([season])
    return dict(zip(roster["player_id"], roster["position"]))


def _games_played_by_team(pbp: pd.DataFrame) -> dict:
    """{team_abbr: number of distinct games} — used to turn season totals
    allowed into per-game rates."""
    home = pbp[["game_id", "home_team"]].drop_duplicates().rename(columns={"home_team": "team"})
    away = pbp[["game_id", "away_team"]].drop_duplicates().rename(columns={"away_team": "team"})
    both = pd.concat([home, away])
    return both.groupby("team")["game_id"].nunique().to_dict()


# ── Defense vs. position (free DVP/aFPA equivalent) ──────────────────────────
def load_defense_vs_position(season: int = 2025) -> dict:
    """
    Per-defense, per-position-group allowed rates (per game), plus a
    "factor" vs league average (1.00 = average, >1.00 = allows more than
    average = good matchup for that position, <1.00 = tougher matchup).

    Positions broken out: QB (pass), RB (rush + receiving), WR (receiving),
    TE (receiving) — the four groups that matter for our six prop
    categories. Built from 2025 play-by-play; same prior-season-baseline
    philosophy as the game-level model (see nfl_analyze_edges.py) — blend
    toward 2026-to-date once real games exist.
    """
    pbp = _load_pbp(season)
    pos_map = _player_positions(season)
    games_by_team = _games_played_by_team(pbp)

    result = {}

    def _bucket(team):
        return result.setdefault(team, {
            "QB": {"pass_yds": 0, "pass_td": 0},
            "RB": {"rush_yds": 0, "rush_td": 0, "rec": 0, "rec_yds": 0, "rec_td": 0},
            "WR": {"rec": 0, "rec_yds": 0, "rec_td": 0},
            "TE": {"rec": 0, "rec_yds": 0, "rec_td": 0},
        })

    # Passing yards/TDs allowed (charged to the opposing QB's position, i.e.
    # just "QB" since that's who throws them — passer position is ~always QB)
    pass_plays = pbp[pbp["passer_player_id"].notna()].copy()
    pass_plays["passer_pos"] = pass_plays["passer_player_id"].map(pos_map)
    for defteam, grp in pass_plays[pass_plays["passer_pos"] == "QB"].groupby("defteam"):
        b = _bucket(defteam)["QB"]
        b["pass_yds"] += grp["passing_yards"].fillna(0).sum()
        b["pass_td"] += grp["pass_touchdown"].fillna(0).sum()

    # Rushing yards/TDs allowed, by rusher position (mostly RB, some QB/WR —
    # we only track RB here since that's the prop category that matters)
    rush_plays = pbp[pbp["rusher_player_id"].notna()].copy()
    rush_plays["rusher_pos"] = rush_plays["rusher_player_id"].map(pos_map)
    for defteam, grp in rush_plays[rush_plays["rusher_pos"] == "RB"].groupby("defteam"):
        b = _bucket(defteam)["RB"]
        b["rush_yds"] += grp["rushing_yards"].fillna(0).sum()
        b["rush_td"] += grp["rush_touchdown"].fillna(0).sum()

    # Receiving yards/receptions/TDs allowed, by receiver position (RB/WR/TE)
    rec_plays = pbp[pbp["receiver_player_id"].notna()].copy()
    rec_plays["receiver_pos"] = rec_plays["receiver_player_id"].map(pos_map)
    for pos in ("RB", "WR", "TE"):
        for defteam, grp in rec_plays[rec_plays["receiver_pos"] == pos].groupby("defteam"):
            b = _bucket(defteam)[pos]
            b["rec"] += grp["complete_pass"].fillna(0).sum()
            b["rec_yds"] += grp["receiving_yards"].fillna(0).sum()
            b["rec_td"] += grp["pass_touchdown"].fillna(0).sum()

    # Convert to per-game rates + league averages + factors
    league_avg = {"QB": {}, "RB": {}, "WR": {}, "TE": {}}
    for pos in ("QB", "RB", "WR", "TE"):
        stat_keys = result[next(iter(result))][pos].keys()
        for stat in stat_keys:
            per_game_vals = []
            for team, buckets in result.items():
                gp = games_by_team.get(team, 17)
                per_game_vals.append(buckets[pos][stat] / gp)
            league_avg[pos][stat] = sum(per_game_vals) / len(per_game_vals) if per_game_vals else 0

    factors = {}
    for team, buckets in result.items():
        gp = games_by_team.get(team, 17)
        factors[team] = {}
        for pos in ("QB", "RB", "WR", "TE"):
            factors[team][pos] = {}
            for stat, total in buckets[pos].items():
                per_game = total / gp
                avg = league_avg[pos][stat]
                factors[team][pos][f"{stat}_per_gm"] = round(per_game, 2)
                factors[team][pos][f"{stat}_factor"] = round(per_game / avg, 3) if avg else 1.0

    factors["_league_avg"] = league_avg
    return factors


# ── Red zone splits (free 4for4 equivalent) ───────────────────────────────────
def load_red_zone_splits(season: int = 2025) -> dict:
    """
    Per-player red-zone volume/production, split at the 20/10/5 yard lines
    (matches the 4for4 red-zone report structure). Core input for anytime-TD
    projections, since TDs cluster near the goal line and don't scale
    linearly with overall yardage.

    Returns {player_id: {"name":, "team":, "position":,
                          "rz20": {...}, "rz10": {...}, "rz5": {...}}}
    where each rzN bucket has rush_att/rush_yds/rush_td (rushers) and/or
    targets/rec/rec_yds/rec_td (receivers), whichever apply to that player.
    """
    pbp = _load_pbp(season)
    pos_map = _player_positions(season)
    roster = nfl_data.import_seasonal_rosters([season])
    name_map = dict(zip(roster["player_id"], roster["player_name"]))
    team_map = dict(zip(roster["player_id"], roster["team"]))

    players = {}

    def _get(pid):
        if pid not in players:
            players[pid] = {
                "name": name_map.get(pid, pid), "team": team_map.get(pid, ""),
                "position": pos_map.get(pid, ""),
                "rz20": {"rush_att": 0, "rush_yds": 0, "rush_td": 0, "tgt": 0, "rec": 0, "rec_yds": 0, "rec_td": 0},
                "rz10": {"rush_att": 0, "rush_yds": 0, "rush_td": 0, "tgt": 0, "rec": 0, "rec_yds": 0, "rec_td": 0},
                "rz5":  {"rush_att": 0, "rush_yds": 0, "rush_td": 0, "tgt": 0, "rec": 0, "rec_yds": 0, "rec_td": 0},
            }
        return players[pid]

    for cutoff, key in ((20, "rz20"), (10, "rz10"), (5, "rz5")):
        zone = pbp[pbp["yardline_100"] <= cutoff]

        rush = zone[zone["rusher_player_id"].notna()]
        for pid, grp in rush.groupby("rusher_player_id"):
            b = _get(pid)[key]
            b["rush_att"] += len(grp)
            b["rush_yds"] += grp["rushing_yards"].fillna(0).sum()
            b["rush_td"] += grp["rush_touchdown"].fillna(0).sum()

        rec = zone[zone["receiver_player_id"].notna()]
        for pid, grp in rec.groupby("receiver_player_id"):
            b = _get(pid)[key]
            b["tgt"] += len(grp)
            b["rec"] += grp["complete_pass"].fillna(0).sum()
            b["rec_yds"] += grp["receiving_yards"].fillna(0).sum()
            b["rec_td"] += grp["pass_touchdown"].fillna(0).sum()

    return players


# ── Player role/share (free "Share Data" equivalent) ─────────────────────────
def load_player_shares(season: int = 2025) -> dict:
    """
    Per-player share of team volume — snap share (from nflverse snap counts),
    target share and rush share (computed from play-by-play team totals).
    This is what separates "good matchup" from "good matchup that this
    player actually sees enough volume to benefit from" — critical for
    committee backfields and any team with a muddled WR pecking order.

    Returns {player_id: {"name":, "team":, "position":, "games":,
                          "snap_pct":, "target_share":, "rush_share":}}
    """
    pbp = _load_pbp(season)
    pos_map = _player_positions(season)
    roster = nfl_data.import_seasonal_rosters([season])
    name_map = dict(zip(roster["player_id"], roster["player_name"]))
    team_map = dict(zip(roster["player_id"], roster["team"]))

    # Team-level totals (denominator for share calcs)
    team_targets = pbp[pbp["receiver_player_id"].notna()].groupby("posteam").size()
    team_rushes = pbp[pbp["rusher_player_id"].notna()].groupby("posteam").size()

    players = {}

    def _get(pid, team):
        if pid not in players:
            players[pid] = {
                "name": name_map.get(pid, pid), "team": team_map.get(pid, team),
                "position": pos_map.get(pid, ""),
                "targets": 0, "carries": 0,
            }
        return players[pid]

    for pid, grp in pbp[pbp["receiver_player_id"].notna()].groupby("receiver_player_id"):
        team = grp["posteam"].iloc[0]
        _get(pid, team)["targets"] += len(grp)
        _get(pid, team)["_team"] = team

    for pid, grp in pbp[pbp["rusher_player_id"].notna()].groupby("rusher_player_id"):
        team = grp["posteam"].iloc[0]
        _get(pid, team)["carries"] += len(grp)
        _get(pid, team)["_team"] = team

    try:
        snaps = nfl_data.import_snap_counts([season])
        # snap_counts is keyed by pfr_player_id (e.g. "McCaCh01"), not the
        # gsis-format player_id (e.g. "00-0034796") used everywhere else here
        # — roster carries both, so crosswalk through it.
        gsis_to_pfr = dict(zip(roster["player_id"], roster["pfr_id"]))
        pfr_snap_pct = snaps.groupby("pfr_player_id")["offense_pct"].mean().to_dict()
        snap_lookup = {gsis: pfr_snap_pct[pfr] for gsis, pfr in gsis_to_pfr.items() if pfr in pfr_snap_pct}
    except Exception as e:
        print(f"  [warn] snap counts unavailable this run: {e}")
        snap_lookup = {}

    for pid, p in players.items():
        team = p.pop("_team", p["team"])
        p["target_share"] = round(p["targets"] / team_targets.get(team, 1), 3)
        p["rush_share"] = round(p["carries"] / team_rushes.get(team, 1), 3)
        p["snap_pct"] = snap_lookup.get(pid)  # None if crosswalk/join unavailable

    return players


# ── WR-CB shadow-coverage matchup (weekly manual upload) ──────────────────────
def load_wr_cb_matchups(pdf_path: str = WR_CB_PDF_PATH) -> list[dict]:
    """
    Parse the weekly ESPN WR-CB matchup PDF the user uploads. This is the one
    input with no free equivalent — individual defender assignment/shadow-
    coverage detection isn't in any public data source.

    Returns [] if the file isn't present yet (e.g. off-season, or user hasn't
    uploaded this week's copy) — callers should treat this as "no shadow-
    coverage data available" and fall back to load_defense_vs_position().

    Uses word-position reconstruction rather than pdfplumber's table
    detection — the PDF's colored matchup-grade boxes and merged cells
    confuse straight table extraction (verified: extract_table() mangled
    the real Week 22 sample into 3 garbage rows). Bucketing words by x0
    position against the header row's column boundaries is far more robust
    and should survive minor template tweaks.

    NOTE: built against the Week 22 sample PDF's exact column layout (OFF
    team/Receiver/Pos/Proj Tgt%/Ht/Wt/LWR/Slot/RWR/Rt/T-R/F-R/[matchup]T-R/
    F-R/DEF team/Z%/Defender/Pos/Ht/Wt/RCB/Slot/LCB/[snap]Rt/T-R/F-R/
    [coverage]Rt/T-R/F-R). If ESPN changes the template, this will need
    adjusting — flag immediately if a weekly upload parses to 0 rows.

    Returns one dict per OFFENSIVE receiver row (the DEF-side columns are
    folded into the same row as that receiver's matchup), with keys:
    off_team, receiver, receiver_pos, proj_tgt_pct, routes,
    matchup_tr_pct, matchup_fr, def_team, defender, defender_pos.
    """
    if not os.path.exists(pdf_path):
        return []

    try:
        import pdfplumber
    except ImportError:
        print("  [warn] pdfplumber not installed — cannot parse WR-CB matchup PDF")
        return []

    # Column boundaries in reading order, keyed by the label(s) that appear
    # in the header row at that x-position (see NOTE above for full layout).
    COLUMNS = [
        ("off_team", 0), ("receiver", 40), ("receiver_pos", 101),
        ("proj_tgt_pct", 112), ("ht", 145), ("wt", 156),
        ("lwr_pct", 162), ("slot_pct", 179), ("rwr_pct", 193),
        ("routes", 209), ("tr_pct", 219), ("fr", 233),
        ("matchup_tr_pct", 253), ("matchup_fr", 267),
        ("def_team", 291), ("z_pct", 307), ("defender", 320),
        ("defender_pos", 392), ("def_ht", 411), ("def_wt", 426),
        ("rcb_pct", 435), ("def_slot_pct", 451), ("lcb_pct", 466),
        ("snap_routes", 482), ("snap_tr_pct", 493), ("snap_fr", 505),
        ("cov_routes", 518), ("cov_tr_pct", 530), ("cov_fr", 542),
    ]
    bounds = [c[1] for c in COLUMNS]
    names = [c[0] for c in COLUMNS]

    def _bucket_row(words):
        d = {n: [] for n in names}
        for w in words:
            idx = 0
            for i, b in enumerate(bounds):
                if w["x0"] >= b:
                    idx = i
            d[names[idx]].append(w["text"])
        return {k: " ".join(v) for k, v in d.items()}

    rows = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            words = page.extract_words()
            if not any(w["text"] == "Receiver" for w in words):
                continue  # not a matchup-table page (e.g. the EPA summary page)
            line_groups = {}
            for w in words:
                line_groups.setdefault(round(w["top"]), []).append(w)
            for top in sorted(line_groups):
                line = sorted(line_groups[top], key=lambda w: w["x0"])
                first_word = line[0]["text"]
                # Skip header/key/legend lines — data rows start with a team code
                if first_word in ("OFF", "Key:", "Wide", "Week", "Matchup") or len(first_word) > 4:
                    continue
                if not first_word.isupper() or not first_word.isalpha():
                    continue
                d = _bucket_row(line)
                if d.get("receiver") and d.get("defender"):
                    rows.append(d)
    return rows
