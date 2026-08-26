"""
dfs_main_build.py — Lineup solver for DraftKings NFL MAIN SLATE DFS.

MainRules_vNext.1, Parts 7/8/12/15/19/23. Takes the authoritative player table
from dfs_main_ingest and emits legal, correlated lineups under a declared set
of constraints. It does NOT decide the player pool, the exposure framework, or
which QBs are worth stacking — those are judgment calls that happen upstream
and arrive here as an already-filtered player list plus a BuildConfig.

Main Slate only.

WHAT IS AND IS NOT ENCODED HERE
Encoded: roster legality, salary cap and salary-band targeting, QB stacking,
opponent bringback, DST-vs-own-stack avoidance, lineup uniqueness, and the
Part 12 FLEX late-swap assignment.

Deliberately NOT encoded: ownership penalties and per-player exposure caps.
Both need real projected ownership, and Part 14 is explicit that the answer to
an ownership problem is never to insert a bad play — so those belong in a layer
that shapes the POOL and the objective, not in hard structural constraints. The
knobs are here (`own_weight`, `max_exposure`) and wire up when the weekly
ownership column lands.

Nothing in here invents a constraint the rules do not state. Anything not in
MainRules_vNext.1 defaults to off — see `max_per_team`.

SOLVER
Sequential MILP (CBC via pulp): solve, record, add a uniqueness cut, re-solve.
Standard approach for portfolio construction and it keeps every lineup provably
optimal against the constraints active at the time it was cut.
"""

import random
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime

import pulp

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SALARY_CAP = 50_000
ROSTER_SIZE = 9

# Base (non-FLEX) requirement per position. FLEX is whichever of these runs one
# over, which is why the solver bounds each position rather than fixing it.
BASE = {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "DST": 1}
FLEX_POS = ("RB", "WR", "TE")
SKILL = ("RB", "WR", "TE")
PASS_CATCHER = ("WR", "TE")


@dataclass
class BuildConfig:
    """
    One contest's construction rules.

    min_stack / bringback are expressed as portfolio TARGETS in the rules
    (Part 7 "do not apply stacking mechanically", Part 8 "~90%, treat as a
    target/sanity check rather than a blind optimizer constraint"), so they are
    per-lineup switches here and the caller varies them across the portfolio.
    Forcing 100% bringback on every lineup would violate Part 8's own wording.
    """
    n_lineups: int
    # Part 2, and there is NO default. Every caller must hand over the explicit
    # approved starter pool as dk_ids. Caught live on 2026-08-26: fed a pool
    # filtered on DK's season-average scoring instead of the projection-source
    # starter list, the solver put Carson Wentz ($4,000, 16.3 avg, one of FOUR
    # QBs DK lists for MIN) in all five smoke lineups. "A cheap backup QB being
    # present in a DraftKings salary file does NOT make him playable." Leaving
    # this optional would mean one forgetful caller reintroduces that silently.
    approved_qbs: frozenset | None = None
    min_stack: int = 1               # stack partners rostered with the QB
    require_bringback: bool = True

    # ANY running back may be a stack partner alongside a pass catcher — Burrow
    # with Ja'Marr Chase AND Chase Brown is a perfectly good double stack. What
    # is banned is the RB being the ONLY partner: Burrow + Chase Brown with no
    # receiver, or Cam Ward + Tony Pollard.
    #
    # solo_stack_rbs is the rare exemption, by dk_id, naming backs who MAY stand
    # alone as the whole stack this week. Empty is the normal state and will
    # usually stay empty — "very rarely, if ever, ... but once in a blue moon it
    # is possible." Ask the user each week; never infer it.
    #
    # An RB as a BRINGBACK against an opposing stack needs no exemption at all;
    # that is always fine and is handled by SKILL.
    solo_stack_rbs: frozenset = frozenset()

    # MINI-CORRELATION (user rule, 2026-08-26; not in MainRules_vNext.1).
    # At least this many rostered players must sit in a game from which the
    # lineup takes TWO OR MORE players. A lone player in a game correlates with
    # nobody and counts zero.
    #
    # The user's own arithmetic, which this reproduces exactly:
    #   QB + his WR + an opposing WR            = 3 in that game
    #   a WR elsewhere + an opposing player     = 2 in that game   -> 5
    #   double-stacked QB makes the first group 4                  -> 6
    # "The more correlation the better. I wouldn't necessarily force it beyond
    # 5" — so this is a floor, never a target the objective chases.
    min_correlated: int = 0
    # DST is excluded by default: a defense sharing a game with your skill
    # players is usually ANTI-correlated with them.
    correlation_positions: tuple = ("QB", "RB", "WR", "TE")

    # TE at FLEX (user rule, 2026-08-26). NOT banned, but rare: "maybe 1-2 weeks
    # out of the year when there is some sort of a 'free square' TE or if
    # salaries are REALLY REALLY REALLY tight." A TE reaches FLEX only in a
    # two-TE roster, so this cap is the whole mechanism. Default 1 keeps TEs out
    # of FLEX; raise to 2 for a week that genuinely earns it, as a deliberate
    # decision rather than a default the optimizer drifts into.
    max_te: int = 1

    # A player may NEVER share a lineup with the DST he is playing against
    # (user, hard rule 2026-08-26): Lions DST rules out every Saints player.
    # This is strictly broader than the old QB-only version, and it pushes the
    # DST out of the stacked game entirely once a bringback is in play — you
    # hold players on both sides, so neither defense is legal.
    ban_dst_vs_players: bool = True

    # A small nudge for rostering a DST alongside a running back from the SAME
    # team — shared game script, and the user is fine with the pairing. Points,
    # in the same units as `score`. Deliberately tiny: "a slight bump is fine so
    # long as it doesn't materially start to force things to go that way."
    rb_dst_bonus: float = 0.0
    min_unique: int = 1              # players that must differ from every prior lineup
    salary_min: int = 0
    salary_max: int = SALARY_CAP
    avoid_dst_vs_stack: bool = True  # never roster a DST facing your own QB
    max_per_team: int | None = None  # NOT a vNext.1 rule; off unless asked for
    own_weight: float = 0.0          # ownership penalty; needs real pOwn to matter
    own_attr: str = "own_large"      # 'own_large' for big fields, 'own_small' for the dime
    locked: tuple = ()               # dk_ids that must appear
    banned: tuple = ()               # dk_ids that must not

    # Per-lineup multiplicative noise on the objective, sigma as a fraction
    # (0.05 = 5%). Re-drawn for EVERY lineup — drawn once it would just reorder
    # the pool and hand back the same solve. This is how the user's own solver
    # already works, and it is what makes 90 genuinely different lineups
    # possible off one projection set instead of 90 near-copies of the optimum.
    randomness: float = 0.0
    seed: int | None = None          # set for reproducible portfolios

    # Exposure ceilings and pool-size caps, per position, as fractions/counts.
    # Enforced by banning a player once he hits his ceiling, which is what keeps
    # Parts 9/10/16's "~60% even for elite plays" from being quietly exceeded.
    max_exposure: dict = field(default_factory=dict)   # {'RB': 0.60, ...}
    max_pool: dict = field(default_factory=dict)       # {'TE': 6, ...} Part 11 hard max

    # Part 15 salary diversification: per-lineup (min, max) bands. Deliberately
    # optional — Part 17 says the dime should NOT leave salary on the table just
    # to be different, so that set is built without a schedule.
    salary_schedule: tuple = ()


@dataclass
class Lineup:
    players: list
    slots: dict = field(default_factory=dict)   # 'QB'/'RB1'/.../'FLEX'/'DST' -> Player

    @property
    def salary(self):
        return sum(p.salary for p in self.players)

    @property
    def proj(self):
        return sum(p.proj for p in self.players)

    @property
    def ceiling(self):
        return sum(p.ceiling for p in self.players)

    def own(self, attr="own_large"):
        return sum(getattr(p, attr) for p in self.players)

    @property
    def qb(self):
        return next((p for p in self.players if p.pos == "QB"), None)

    @property
    def ids(self):
        return frozenset(p.dk_id for p in self.players)

    def stack_size(self):
        """
        Skill players rostered from the QB's own team.

        Counts RBs, because the user counts them: Burrow with Ja'Marr Chase and
        Chase Brown "would be fine as that would be a double stack." Use
        pass_catcher_stack() for the WR/TE-only figure.
        """
        q = self.qb
        if not q:
            return 0
        return sum(1 for p in self.players
                   if p.team == q.team and p.pos in SKILL)

    def pass_catcher_stack(self):
        """WR/TE only. Zero here with stack_size() >= 1 means an RB-only stack."""
        q = self.qb
        if not q:
            return 0
        return sum(1 for p in self.players
                   if p.team == q.team and p.pos in PASS_CATCHER)

    def has_bringback(self):
        q = self.qb
        if not q:
            return False
        return any(p.team == q.opp and p.pos in SKILL for p in self.players)

    def correlated_count(self, positions=("QB", "RB", "WR", "TE")):
        """
        Players sitting in a game this lineup takes two or more players from.

        The user's mini-correlation floor is 5. A player alone in his game
        correlates with nobody and contributes zero, which is why this counts
        game clusters rather than pairs.
        """
        by_game = {}
        for p in self.players:
            if p.pos in positions and p.game:
                by_game.setdefault(p.game, []).append(p)
        return sum(len(v) for v in by_game.values() if len(v) >= 2)

    def game_clusters(self, positions=("QB", "RB", "WR", "TE")):
        """{game: n} for games contributing 2+ players, largest first."""
        by_game = {}
        for p in self.players:
            if p.pos in positions and p.game:
                by_game[p.game] = by_game.get(p.game, 0) + 1
        return dict(sorted(((g, n) for g, n in by_game.items() if n >= 2),
                           key=lambda kv: -kv[1]))


# ── FLEX assignment (Part 12) ─────────────────────────────────────────────────

def assign_slots(players):
    """
    Map 9 players onto DK's named slots, obeying Part 12's late-swap rule:
    the latest-starting eligible player goes in FLEX; ties break to the highest
    salary.

    "Eligible" is narrower than it first looks. With 3 RB / 3 WR / 1 TE the FLEX
    can only be an RB — the other two RBs have to fill RB1/RB2 and there is
    nowhere else for a third to go. So the surplus position is forced first, and
    the late-swap choice happens only within it.
    """
    by_pos = {}
    for p in players:
        by_pos.setdefault(p.pos, []).append(p)

    surplus = [pos for pos in FLEX_POS if len(by_pos.get(pos, [])) > BASE[pos]]
    if len(surplus) != 1:
        raise ValueError(
            "cannot assign FLEX: expected exactly one position over its base "
            "requirement, got %s" % {k: len(v) for k, v in by_pos.items()})
    fpos = surplus[0]

    # Latest kickoff wins; identical kickoff breaks to salary. A missing kickoff
    # sorts to the very bottom (datetime.min) so it can never silently win the
    # FLEX — the ingest gate already FAILs on missing times, this is belt-and-braces.
    def late_key(p):
        return (p.kickoff or datetime.min, p.salary)

    pool = sorted(by_pos[fpos], key=late_key)
    flex = pool[-1]

    slots = {"QB": by_pos["QB"][0], "DST": by_pos["DST"][0], "FLEX": flex}
    for pos, n in (("RB", 2), ("WR", 3), ("TE", 1)):
        rest = [p for p in by_pos.get(pos, []) if p is not flex]
        if len(rest) != n:
            raise ValueError("slot fill mismatch at %s: %d for %d" % (pos, len(rest), n))
        for i, p in enumerate(rest, 1):
            slots[pos + str(i) if n > 1 else pos] = p
    return slots


# ── Solver ────────────────────────────────────────────────────────────────────


# ── Part 15 salary diversification ────────────────────────────────────────────

def salary_bands(n, cap=SALARY_CAP):
    """
    Build a per-lineup (min, max) schedule satisfying Part 15's large-field
    guides simultaneously:
        <= ~40% at $49,800+   >= ~25% in $49,000-$49,400   >= ~10% at <= $49,000

    Returned in a fixed order and shuffled by the caller if desired. Part 15 is
    explicit these are guides, "not excuses to destroy projection" — so the
    bands are wide enough that the solver still has room to find a good lineup
    inside each one.

    Bands are non-overlapping at the edges on purpose. A band of
    (49_400, 49_800) lets a lineup land on exactly 49,800, which then counts
    against the "no more than ~40% at $49,800+" guide it was supposed to relieve
    — the filler band alone pushed a nominally-compliant 40% up to 65%.
    """
    hi = round(n * 0.40)
    mid = round(n * 0.25)
    lo = max(1, round(n * 0.10))
    rest = max(0, n - hi - mid - lo)
    return tuple([(49_800, cap)] * hi + [(49_000, 49_400)] * mid
                 + [(0, 48_999)] * lo + [(49_401, 49_799)] * rest)[:n]


# ── Solver ────────────────────────────────────────────────────────────────────

def build_portfolio(players, cfg, score=lambda p: p.proj, existing=None, verbose=False):
    """
    Solve cfg.n_lineups lineups over `players`.

    `score` is the per-player objective contribution — pass a lambda so the
    caller decides whether it is optimizing projection, ceiling, or a blend,
    rather than this module assuming.

    `existing` seeds the uniqueness cuts with lineups already built for other
    contests. Pass the running portfolio to guarantee no lineup ever repeats
    across sets: separate solves over one pool with one objective otherwise
    converge on the same answer, which is how the $1 and $3 sets came back with
    20 identical lineups on 2026-08-26.

    Returns (lineups, unsolved_reason|None). A short return is not an exception:
    over-constrained slates happen, and Part 29 says surface the conflict rather
    than silently loosening a rule to make the solve go through.
    """
    if cfg.approved_qbs is None:
        raise ValueError(
            "BuildConfig.approved_qbs is required (Part 2). Pass the explicit set "
            "of starter dk_ids. There is deliberately no default: DK lists every "
            "backup on the slate, and an unfiltered pool will hand you a $4,000 "
            "QB2 the moment his salary looks attractive.")

    approved = frozenset(cfg.approved_qbs)
    full = [p for p in players
            if p.playable and p.dk_id not in cfg.banned
            and (p.pos != "QB" or p.dk_id in approved)]
    idx = {p.dk_id: p for p in full}

    qbs = [p for p in full if p.pos == "QB"]
    if not qbs:
        return [], "no approved QB survived the pool filter"
    for pos, need in BASE.items():
        have = sum(1 for p in full if p.pos == pos)
        if have < need + (1 if pos in FLEX_POS else 0):
            return [], "pool has only %d %s -- cannot fill the roster" % (have, pos)

    teams = {p.team for p in full}
    catchers = {t: [p for p in full if p.team == t and p.pos in PASS_CATCHER]
                for t in teams}
    opp_of = {q.team: q.opp for q in qbs}
    opp_skill = {t: [p for p in full if p.pos in SKILL and p.team == opp_of.get(t)]
                 for t in teams}

    lineups = []
    cuts = [lu.ids for lu in (existing or [])]
    rng = random.Random(cfg.seed)
    used = Counter()                    # dk_id -> lineups containing him
    seen = defaultdict(set)             # pos -> distinct dk_ids used so far
    relaxed = {}

    def eligible(slack=0.0):
        """Pool minus anyone who has hit his exposure ceiling."""
        blocked = {dk_id for dk_id, c in used.items()
                   if (cfg.max_exposure.get(idx[dk_id].pos) is not None
                       and c >= (cfg.max_exposure[idx[dk_id].pos] + slack) * cfg.n_lineups)}
        return [p for p in full if p.dk_id not in blocked]

    def solve(live, smin, smax, pool_slack=0, uniq=None):
        prob = pulp.LpProblem("dfs_main", pulp.LpMaximize)
        y = {p.dk_id: pulp.LpVariable("y_" + p.dk_id, cat="Binary") for p in live}

        def jitter(p):
            s = score(p)
            return s * (1 + rng.gauss(0, cfg.randomness)) if cfg.randomness else s

        obj = pulp.lpSum(jitter(p) * y[p.dk_id] for p in live)
        if cfg.own_weight:
            obj -= cfg.own_weight * pulp.lpSum(
                getattr(p, cfg.own_attr) * y[p.dk_id] for p in live)

        # RB paired with his own DST: a small bonus, never a requirement. b_t can
        # only be 1 when both a same-team RB and that DST are rostered, and since
        # it only ever adds to the objective the solver claims it exactly when
        # the pairing happens to occur.
        if cfg.rb_dst_bonus:
            for d in live:
                if d.pos != "DST":
                    continue
                mates = [p for p in live if p.pos == "RB" and p.team == d.team]
                if not mates:
                    continue
                b = pulp.LpVariable("rbdst_%s_%d" % (d.dk_id, n), cat="Binary")
                prob += b <= y[d.dk_id]
                prob += b <= pulp.lpSum(y[p.dk_id] for p in mates)
                obj += cfg.rb_dst_bonus * b
        prob += obj

        # -- roster legality --
        prob += pulp.lpSum(y.values()) == ROSTER_SIZE
        prob += pulp.lpSum(y[p.dk_id] for p in live if p.pos == "QB") == 1
        prob += pulp.lpSum(y[p.dk_id] for p in live if p.pos == "DST") == 1
        for pos in FLEX_POS:
            sel = pulp.lpSum(y[p.dk_id] for p in live if p.pos == pos)
            prob += sel >= BASE[pos]
            prob += sel <= BASE[pos] + 1     # at most one position takes the FLEX
        # TE at FLEX is a two-TE lineup by definition; cap TE to forbid it.
        prob += pulp.lpSum(y[p.dk_id] for p in live if p.pos == "TE") <= cfg.max_te

        # -- mini-correlation --
        # A player counts as correlated only if his GAME contributes >= 2 players.
        # z_g flags "this game is in play", k_g is how many of its players count.
        # k_g is bounded by the actual count, so pushing the sum to the floor
        # makes k_g equal that count; no incentive to overstate.
        if cfg.min_correlated:
            games = {}
            for p in live:
                if p.pos in cfg.correlation_positions and p.game:
                    games.setdefault(p.game, []).append(p)
            terms = []
            for g, members in games.items():
                if len(members) < 2:
                    continue
                count = pulp.lpSum(y[p.dk_id] for p in members)
                z = pulp.LpVariable("z_%s_%d" % (re.sub(r"\W", "", g), n), cat="Binary")
                k = pulp.LpVariable("k_%s_%d" % (re.sub(r"\W", "", g), n),
                                    lowBound=0, upBound=ROSTER_SIZE)
                prob += count >= 2 * z          # a game only counts with 2+ players
                prob += k <= count
                prob += k <= ROSTER_SIZE * z
                terms.append(k)
            if terms:
                prob += pulp.lpSum(terms) >= cfg.min_correlated

        # -- salary --
        spend = pulp.lpSum(p.salary * y[p.dk_id] for p in live)
        prob += spend <= min(smax, cfg.salary_max)
        if smin:
            prob += spend >= smin

        # -- Parts 7/8 stacking and bringback, linearized per QB --
        for q in qbs:
            if q.dk_id not in y:
                continue
            if cfg.min_stack:
                cat = [p for p in catchers[q.team] if p.dk_id in y]
                rbs = [p for p in live if p.pos == "RB" and p.team == q.team]
                # The stack must never be an RB standing alone: at least one
                # pass catcher, unless a back explicitly exempted this week is
                # rostered. Any RB counts toward the size of the stack itself.
                solo = [p for p in rbs if p.dk_id in cfg.solo_stack_rbs]
                prob += (pulp.lpSum(y[p.dk_id] for p in cat)
                         + pulp.lpSum(y[p.dk_id] for p in solo) >= y[q.dk_id])
                prob += (pulp.lpSum(y[p.dk_id] for p in cat + rbs)
                         >= cfg.min_stack * y[q.dk_id])
            if cfg.require_bringback:
                back = [p for p in opp_skill.get(q.team, []) if p.dk_id in y]
                if back:
                    prob += pulp.lpSum(y[p.dk_id] for p in back) >= y[q.dk_id]
        # -- a DST never shares a lineup with a player it faces (hard rule) --
        if cfg.ban_dst_vs_players:
            for d in live:
                if d.pos != "DST":
                    continue
                for p in live:
                    if p.pos != "DST" and p.team == d.opp:
                        prob += y[d.dk_id] + y[p.dk_id] <= 1

        # -- pool-size caps (Parts 9/10/11/13) --
        # Must be a CONSTRAINT, not a pre-filter. A lineup can take two players
        # at one position (TE plus TE-in-FLEX), so filtering out new faces only
        # once the pool is already full lets it jump 5 -> 7 in a single solve and
        # blow straight through Part 11's hard maximum of 6. Caught 2026-08-26.
        for pos, limit in cfg.max_pool.items():
            room = limit + pool_slack - len(seen[pos])
            newcomers = [p for p in live if p.pos == pos and p.dk_id not in seen[pos]]
            if newcomers:
                prob += pulp.lpSum(y[p.dk_id] for p in newcomers) <= max(0, room)

        if cfg.max_per_team:
            for t in teams:
                prob += pulp.lpSum(y[p.dk_id] for p in live
                                   if p.team == t and p.pos != "DST") <= cfg.max_per_team

        for dk_id in cfg.locked:
            if dk_id in y:
                prob += y[dk_id] == 1

        # -- Parts 19/20 uniqueness, including across contests via `existing` --
        for prior in cuts:
            prob += (pulp.lpSum(y[i] for i in prior if i in y)
                     <= ROSTER_SIZE - (cfg.min_unique if uniq is None else uniq))

        if pulp.LpStatus[prob.solve(pulp.PULP_CBC_CMD(msg=0))] != "Optimal":
            return None
        return [idx[i] for i, v in y.items() if v.value() and v.value() > 0.5]

    for n in range(cfg.n_lineups):
        band = (cfg.salary_schedule[n] if n < len(cfg.salary_schedule)
                else (cfg.salary_min, cfg.salary_max))
        wide = (cfg.salary_min, cfg.salary_max)

        # Relaxation ladder, least damaging first. Part 29: "If constraints
        # conflict, identify the conflict and discuss the least damaging
        # adjustment." Everything above the line is guidance in the rules;
        # nothing below it is ever touched -- the correlation floor, the D/ST
        # ban, stacking, bringback, the TE cap, the Part 2 QB gate and roster
        # legality all hold or the lineup does not get built.
        #
        # This exists because a realistic ~150-player pool went infeasible at
        # lineup 16 of the fourth set where the 386-player placeholder pool did
        # not. Failing outright there would have surfaced on a Sunday.
        ladder = (
            ("",                    band, 0, None),
            ("salary band dropped", wide, 0, None),
            ("pool caps +2",        wide, 2, None),
            ("uniqueness 2 -> 1",   wide, 2, 1),
            ("exposure caps +10pt", wide, 2, 1),
        )
        chosen = why = None
        for i, (label, (smin, smax), slack, uniq) in enumerate(ladder):
            live = eligible(0.10 if i == 4 else 0.0)
            chosen = solve(live, smin, smax, pool_slack=slack, uniq=uniq)
            if chosen is not None:
                why = label
                break
        if chosen is None:
            return lineups, ("infeasible on lineup %d of %d even after every "
                             "permitted relaxation -- the pool is too thin for "
                             "the correlation and stacking rules"
                             % (n + 1, cfg.n_lineups))
        if why:
            relaxed.setdefault(why, 0)
            relaxed[why] += 1

        lu = Lineup(chosen)
        lu.slots = assign_slots(chosen)
        lineups.append(lu)
        cuts.append(lu.ids)
        for p in chosen:
            used[p.dk_id] += 1
            seen[p.pos].add(p.dk_id)
        if verbose:
            print("  %2d  $%s  %.1f  stack %d  corr %d  %s"
                  % (n + 1, format(lu.salary, ","), sum(score(p) for p in chosen),
                     lu.stack_size(), lu.correlated_count(), why or ""))

    note = None
    if relaxed:
        note = "relaxed: " + "; ".join("%s x%d" % (k, v) for k, v in relaxed.items())
    return lineups, note
