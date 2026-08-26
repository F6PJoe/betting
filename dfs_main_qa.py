"""
dfs_main_qa.py — Mandatory lineup QA for DraftKings NFL MAIN SLATE DFS.

MainRules_vNext.1 Parts 24 (per-contest QA) and 25 (cross-contest comparison).

Part 24 is blunt about the contract: "If QA fails: DO NOT GIVE ME THE CSV AND
CALL IT FINAL." So every check here returns a finding, and the export step is
expected to refuse on any FAIL rather than warn and continue.

WHY THIS IS A SEPARATE MODULE FROM THE SOLVER
The solver can only guarantee the constraints it was given. QA re-derives every
property from the finished lineups independently, so a constraint that was
silently dropped, mis-linearized, or never wired up still gets caught. A solver
checking its own work proves nothing.

Every check here was chosen because it caught something real in the user's own
Week 13 portfolio, or because it guards a rule with a hard limit:
  - TE pool of 9 and 7 against Part 11's stated MAXIMUM of 6
  - RB pools of 15-16 (target 6-10), WR pools of 22-23 (target 12-16)
  - three orphan QBs at one lineup each, against Part 6
  - salary diversification inverted between the $3 and $1 sets
"""

import sys
from collections import Counter, defaultdict
from statistics import mean, median

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SALARY_CAP = 50_000
SLOT_ORDER = ("QB", "RB1", "RB2", "WR1", "WR2", "WR3", "TE", "FLEX", "DST")

# Pool sizes straight from the rules. TE's upper bound is the only HARD maximum
# the rules state ("Hard maximum: 6"), so it is the only one that FAILs rather
# than WARNs. Parts 6/9/10/11/13.
POOL_TARGETS = {
    "QB": (3, 5, "Part 6"),
    "RB": (6, 10, "Part 9"),
    "WR": (12, 16, "Part 10"),
    "TE": (4, 6, "Part 11"),
    "DST": (5, 7, "Part 13"),
}
TE_HARD_MAX = 6

# Part 15 large-field salary guides. Stated as approximate portfolio guides, so
# these WARN — they are sanity checks, not constraints, and Part 15 is explicit
# that they are "not excuses to destroy projection."
SALARY_GUIDES = (
    ("at $49,800+", lambda s: s >= 49_800, None, 0.40),
    ("in $49,000-$49,400", lambda s: 49_000 <= s <= 49_400, 0.25, None),
    ("at $49,000 or below", lambda s: s <= 49_000, 0.10, None),
)


def _pool(lineups, pos):
    return {p.dk_id for lu in lineups for p in lu.players if p.pos == pos}


def qa_contest(lineups, label, approved_qbs=None, check_pools=None,
               own_attr="own_large", bringback_target=0.90,
               min_correlated=5, pool_tolerance=2, max_per_game=5):
    """
    Run Part 24 against ONE contest's lineups. Returns a list of finding strings
    prefixed FAIL / WARN / INFO.

    `check_pools` defaults to True only for full 20-lineup sets — a 10-lineup
    Milly portfolio will naturally use fewer players, and flagging it against
    20-lineup pool targets would be noise rather than signal.
    """
    out = []
    F = lambda m: out.append("FAIL  [%s] %s" % (label, m))
    W = lambda m: out.append("WARN  [%s] %s" % (label, m))
    I = lambda m: out.append("INFO  [%s] %s" % (label, m))

    n = len(lineups)
    if not n:
        return ["FAIL  [%s] no lineups" % label]
    if check_pools is None:
        check_pools = n >= 20

    # ── legality ──────────────────────────────────────────────────────────────
    for i, lu in enumerate(lineups, 1):
        pos = Counter(p.pos for p in lu.players)
        if len(lu.players) != 9:
            F("lineup %d has %d players" % (i, len(lu.players)))
        if len({p.dk_id for p in lu.players}) != len(lu.players):
            F("lineup %d rosters the same player twice" % i)
        if lu.salary > SALARY_CAP:
            F("lineup %d is $%s over the cap" % (i, format(lu.salary - SALARY_CAP, ",")))
        if pos["QB"] != 1 or pos["DST"] != 1:
            F("lineup %d has %d QB / %d DST" % (i, pos["QB"], pos["DST"]))
        if not (2 <= pos["RB"] <= 3 and 3 <= pos["WR"] <= 4 and 1 <= pos["TE"] <= 2):
            F("lineup %d position counts illegal: RB %d WR %d TE %d"
              % (i, pos["RB"], pos["WR"], pos["TE"]))
        if set(lu.slots) != set(SLOT_ORDER):
            F("lineup %d slot map is malformed: %s" % (i, sorted(lu.slots)))
        for p in lu.players:
            if not p.playable:
                F("lineup %d rosters %s (DK status %s)" % (i, p.name, p.status))
            if p.pos in ("RB", "WR", "TE") and not p.flex_eligible:
                W("lineup %d: %s is not FLEX-eligible per DK" % (i, p.name))

    # ── Part 2: QB validation ─────────────────────────────────────────────────
    qb_counts = Counter(lu.qb.dk_id for lu in lineups if lu.qb)
    names = {lu.qb.dk_id: lu.qb.name for lu in lineups if lu.qb}
    if approved_qbs is not None:
        for dk_id in qb_counts:
            if dk_id not in approved_qbs:
                F("%s is not in the approved QB pool -- Part 2 violation"
                  % names[dk_id])
    I("QB pool %d: %s" % (len(qb_counts), ", ".join(
        "%s %d (%.0f%%)" % (names[k], v, 100 * v / n)
        for k, v in qb_counts.most_common())))
    for dk_id, c in qb_counts.items():
        if c == 1:
            W("%s appears in exactly 1 lineup -- Part 6 orphan QB. If he only "
              "deserves one, reconsider whether he belongs in the pool at all."
              % names[dk_id])

    # ── Parts 7/8: correlation ────────────────────────────────────────────────
    stacks = Counter(lu.stack_size() for lu in lineups)
    backs = sum(1 for lu in lineups if lu.has_bringback())
    naked = stacks.get(0, 0)
    I("stacks: " + ", ".join("%d-stack x%d (%.0f%%)" % (k, v, 100 * v / n)
                             for k, v in sorted(stacks.items())))
    I("bringback %d/%d = %.0f%% (Part 8 target ~%.0f%%)"
      % (backs, n, 100 * backs / n, 100 * bringback_target))
    if backs / n < bringback_target - 0.15:
        W("bringback rate %.0f%% is well under the ~%.0f%% target -- Part 8 says "
          "if a game is consistently unattractive to bring back, reconsider "
          "whether that QB should be a major stacking target at all"
          % (100 * backs / n, 100 * bringback_target))
    if naked:
        W("%d naked QB lineup(s) -- Part 7 wants strong slate-specific "
          "justification, normally substantial rushing upside" % naked)

    # An RB may be part of a stack, never the whole of one, unless explicitly
    # exempted for the week. QB + Chase Brown alone is the case being blocked;
    # QB + Ja'Marr Chase + Chase Brown is a fine double stack.
    rb_only = [i for i, lu in enumerate(lineups, 1)
               if lu.stack_size() and not lu.pass_catcher_stack()]
    for i in rb_only[:6]:
        F("lineup %d stacks the QB with an RB and no pass catcher -- an RB may "
          "be part of a stack, not the whole of it" % i)
    with_rb = sum(1 for lu in lineups
                  if lu.stack_size() > lu.pass_catcher_stack())
    if with_rb:
        I("%d/%d lineups include an RB as a stack piece alongside a receiver"
          % (with_rb, n))

    # ── Part 12: FLEX ─────────────────────────────────────────────────────────
    # Only lineups with a well-formed slot map; the malformed ones already FAILed
    # above. QA exists to report broken input, so it must not crash on it.
    slotted = [lu for lu in lineups if set(lu.slots) == set(SLOT_ORDER)]
    if not slotted:
        out.append("WARN  [%s] no lineup has a usable slot map -- FLEX, late-swap "
                   "and TE-at-FLEX checks skipped" % label)
        slotted = []
    flex_pos = Counter(lu.slots["FLEX"].pos for lu in slotted)
    if flex_pos:
        I("FLEX: " + ", ".join("%s %d (%.0f%%)" % (k, v, 100 * v / len(slotted))
                               for k, v in flex_pos.most_common()))
    for i, lu in enumerate(slotted, 1):
        flex = lu.slots["FLEX"]
        peers = [p for p in lu.players if p.pos == flex.pos]
        best = max(peers, key=lambda p: (p.kickoff, p.salary))
        if best.dk_id != flex.dk_id:
            F("lineup %d: %s is in FLEX but %s starts later or costs more -- "
              "Part 12 late-swap violation" % (i, flex.name, best.name))
    te_flex = flex_pos.get("TE", 0)
    if te_flex and slotted:
        # Not banned, but rare: the user expects TE at FLEX in "maybe 1-2 weeks
        # out of the year" — a free-square TE, or salaries that are genuinely
        # desperate. So any occurrence is a prompt for that week's conversation,
        # and a heavy rate is a failure until someone says otherwise.
        (F if te_flex / len(slotted) > 0.25 else W)(
            "%d lineup(s) (%.0f%%) put a TE at FLEX -- expected ~never. Confirm "
            "this week has a free-square TE or genuinely tight salaries"
            % (te_flex, 100 * te_flex / len(slotted)))

    # ── mini-correlation (user rule) ──────────────────────────────────────────
    corr = [lu.correlated_count() for lu in lineups]
    I("correlated players/lineup: min %d  mean %.1f  max %d  (floor %d)"
      % (min(corr), mean(corr), max(corr), min_correlated))
    short = [(i, c) for i, c in enumerate(corr, 1) if c < min_correlated]
    for i, c in short[:8]:
        F("lineup %d has only %d correlated players (floor %d) -- a player alone "
          "in his game counts zero" % (i, c, min_correlated))
    if len(short) > 8:
        F("...and %d more lineups under the correlation floor" % (len(short) - 8))
    solo = sum(1 for lu in lineups if len(lu.game_clusters()) < 2)
    if solo:
        I("%d lineup(s) draw their correlation from a single game -- fine, but "
          "the QB stack is doing all the work" % solo)

    # ── two RBs from one team, and game concentration ─────────────────────────
    for i, lu in enumerate(lineups, 1):
        for t, c in Counter(p.team for p in lu.players if p.pos == "RB").items():
            if c > 1:
                F("lineup %d rosters %d %s running backs -- they split one "
                  "workload" % (i, c, t))
        for g, c in Counter(p.game for p in lu.players if p.pos != "DST").items():
            if c > max_per_game:
                F("lineup %d takes %d players from %s (cap %d) -- that is "
                  "concentration, not correlation" % (i, c, g, max_per_game))
    deep = Counter(max(Counter(p.game for p in lu.players if p.pos != "DST").values())
                   for lu in lineups)
    I("heaviest game per lineup: " + ", ".join(
        "%d players x%d" % (k, v) for k, v in sorted(deep.items())))

    # ── no uncorrelated team concentration ────────────────────────────────────
    for i, lu in enumerate(lineups, 1):
        qb = lu.qb
        by_team = Counter(p.team for p in lu.players if p.pos in ("RB", "WR", "TE"))
        for t, c in by_team.items():
            if c <= 2:
                continue
            if qb and t in (qb.team, qb.opp):
                continue
            F("lineup %d has %d %s players with neither %s's QB nor the opposing "
              "QB rostered -- uncorrelated concentration" % (i, c, t, t))

    # ── DST vs the players it faces (hard rule) ───────────────────────────────
    for i, lu in enumerate(lineups, 1):
        dst = next((p for p in lu.players if p.pos == "DST"), None)
        if not dst:
            continue
        against = [p.name for p in lu.players if p.pos != "DST" and p.team == dst.opp]
        if against:
            F("lineup %d rosters %s DST against its own %s -- hard rule"
              % (i, dst.team, ", ".join(against)))
    paired = sum(1 for lu in lineups
                 if any(p.pos == "RB" and p.team == d.team
                        for d in lu.players if d.pos == "DST"
                        for p in lu.players))
    I("RB paired with his own DST in %d/%d lineups (%.0f%%) -- nudged, never forced"
      % (paired, n, 100 * paired / n))

    # ── Part 15: salary ───────────────────────────────────────────────────────
    sal = [lu.salary for lu in lineups]
    I("salary  min $%s  max $%s  mean $%s  median $%s"
      % (format(min(sal), ","), format(max(sal), ","),
         format(int(mean(sal)), ","), format(int(median(sal)), ",")))
    for desc, pred, floor_, ceil_ in SALARY_GUIDES:
        share = sum(1 for s in sal if pred(s)) / n
        msg = "%.0f%% %s" % (100 * share, desc)
        if ceil_ is not None and share > ceil_:
            W(msg + " (Part 15 guide: no more than ~%.0f%%)" % (100 * ceil_))
        elif floor_ is not None and share < floor_:
            W(msg + " (Part 15 guide: at least ~%.0f%%)" % (100 * floor_))
        else:
            I(msg)
    bands = Counter(s // 200 * 200 for s in sal)
    top_band, top_n = bands.most_common(1)[0]
    if top_n / n > 0.40:
        W("%.0f%% of lineups sit in the single $%s-$%s band -- Part 15 warns "
          "against clustering in one narrow $200 band"
          % (100 * top_n / n, format(top_band, ","), format(top_band + 199, ",")))

    # ── Part 14: ownership ────────────────────────────────────────────────────
    owns = [lu.own(own_attr) for lu in lineups]
    if sum(owns) == 0:
        W("cumulative ownership is zero for every lineup -- ownership QA did not "
          "run (no pOwn joined)")
    else:
        I("cum pOwn (%s)  min %.0f  max %.0f  mean %.0f  median %.0f"
          % (own_attr, min(owns), max(owns), mean(owns), median(owns)))
        hi = [i for i, o in enumerate(owns, 1) if o > mean(owns) + 2 * (
            max(owns) - mean(owns)) / 3]
        if hi:
            I("highest-pOwn lineups: %s" % hi[:6])

    # ── pool breadth ──────────────────────────────────────────────────────────
    for pos in ("RB", "WR", "TE", "DST"):
        size = len(_pool(lineups, pos))
        lo, hi_, part = POOL_TARGETS[pos]
        if pos == "TE" and size > TE_HARD_MAX:
            F("TE pool is %d -- Part 11 states a HARD MAXIMUM of %d"
              % (size, TE_HARD_MAX))
        elif not check_pools:
            I("%s pool %d (target %d-%d, %s -- not enforced on a %d-lineup set)"
              % (pos, size, lo, hi_, part, n))
        elif size < lo - pool_tolerance or size > hi_ + pool_tolerance:
            W("%s pool is %d, well outside the %d-%d target (%s)"
              % (pos, size, lo, hi_, part))
        elif size < lo or size > hi_:
            # User 2026-08-26: "not a black and white rule ... you could go
            # slightly above or below the target when it makes sense."
            I("%s pool %d, just outside the %d-%d target (%s) -- within tolerance"
              % (pos, size, lo, hi_, part))
        else:
            I("%s pool %d (target %d-%d)" % (pos, size, lo, hi_))

    qlo, qhi, _ = POOL_TARGETS["QB"]
    if check_pools and not (qlo - 1 <= len(qb_counts) <= qhi + 1):
        W("QB pool is %d, outside the %d-%d target (Part 6)"
          % (len(qb_counts), qlo, qhi))

    # ── exposure ──────────────────────────────────────────────────────────────
    expo = Counter(p.dk_id for lu in lineups for p in lu.players)
    nm = {p.dk_id: (p.name, p.pos) for lu in lineups for p in lu.players}
    I("top exposure: " + ", ".join(
        "%s(%s) %.0f%%" % (nm[k][0], nm[k][1], 100 * v / n)
        for k, v in expo.most_common(8)))
    for dk_id, c in expo.items():
        if c / n > 0.60 and nm[dk_id][1] != "DST":
            W("%s at %.0f%% exceeds the ~60%% ceiling the rules allow even for "
              "elite plays (Parts 9/10/16)" % (nm[dk_id][0], 100 * c / n))

    # ── duplicates ────────────────────────────────────────────────────────────
    sigs = Counter(lu.ids for lu in lineups)
    dupes = sum(v - 1 for v in sigs.values() if v > 1)
    if dupes:
        F("%d duplicate lineup(s) inside this contest" % dupes)
    else:
        I("no duplicate lineups")
    return out


def qa_portfolio(sets, own_attr="own_large"):
    """
    Part 25 cross-contest comparison. `sets` is an ordered list of
    (label, lineups) from largest field to smallest.

    The rules expect projection to RISE as field size falls: "If the $0.10
    lineups are materially lower projected than the $3 merely because they are
    being made different, something is wrong." That progression is checked here
    rather than left to the eye.
    """
    out = []
    rows = []
    for label, lus in sets:
        if not lus:
            continue
        rows.append({
            "label": label, "n": len(lus),
            "proj": mean(lu.proj for lu in lus),
            "ceil": mean(lu.ceiling for lu in lus),
            "own": mean(lu.own(own_attr) for lu in lus),
            "sal": mean(lu.salary for lu in lus),
            "qb": len({lu.qb.dk_id for lu in lus if lu.qb}),
            "rb": len(_pool(lus, "RB")), "wr": len(_pool(lus, "WR")),
            "te": len(_pool(lus, "TE")), "dst": len(_pool(lus, "DST")),
            "dbl": sum(1 for lu in lus if lu.stack_size() >= 2) / len(lus),
            "back": sum(1 for lu in lus if lu.has_bringback()) / len(lus),
            "corr": mean(lu.correlated_count() for lu in lus),
            "teflex": sum(1 for lu in lus if lu.slots["FLEX"].pos == "TE") / len(lus),
        })

    out.append("%-10s%4s %8s %8s %8s %9s %4s %4s %4s %4s %4s %7s %7s %5s %6s"
               % ("contest", "n", "proj", "ceiling", "pOwn", "salary",
                  "QB", "RB", "WR", "TE", "DST", "2stack", "bring",
                  "corr", "TEflx"))
    for r in rows:
        out.append("%-10s%4d %8.1f %8.1f %8.0f %9s %4d %4d %4d %4d %4d %6.0f%% %6.0f%% %5.1f %5.0f%%"
                   % (r["label"], r["n"], r["proj"], r["ceil"], r["own"],
                      "$" + format(int(r["sal"]), ","), r["qb"], r["rb"], r["wr"],
                      r["te"], r["dst"], 100 * r["dbl"], 100 * r["back"],
                      r["corr"], 100 * r["teflex"]))

    # overlap between portfolios
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            a = {lu.ids for lu in sets[i][1]}
            b = {lu.ids for lu in sets[j][1]}
            shared = len(a & b)
            if shared:
                out.append("WARN  %s and %s share %d identical lineup(s)"
                           % (sets[i][0], sets[j][0], shared))

    # Part 25 progression check, only meaningful once real projections exist
    scored = [r for r in rows if r["proj"] > 0]
    if len(scored) >= 2:
        for a, b in zip(scored, scored[1:]):
            if b["proj"] < a["proj"] - 0.5:
                out.append(
                    "WARN  %s mean projection (%.1f) is below %s (%.1f) -- Part 25 "
                    "expects projection to rise as field size falls"
                    % (b["label"], b["proj"], a["label"], a["proj"]))
    else:
        out.append("INFO  projection progression not checked -- no projections joined")
    return out
