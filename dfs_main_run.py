"""
dfs_main_run.py — Weekly entry point for DraftKings NFL MAIN SLATE DFS.

    python dfs_main_run.py                      # auto-discover files in SLATE_DIR
    python dfs_main_run.py <entries> <salaries> <projections>
    python dfs_main_run.py --no-projections <entries> <salaries>

Runs the whole chain: ingest -> validation gate -> five portfolios -> Part 24
QA per contest -> Part 25 cross-contest -> DK upload CSV. Refuses to write the
upload file if any QA check FAILs (Part 24: "If QA fails: DO NOT GIVE ME THE
CSV AND CALL IT FINAL.")

CONTEST PLAN — 90 lineups across 170 entries
Leverage is driven by ACTUAL FIELD SIZE, not entry fee. Approved by the user
2026-08-26 after Week 1's counts contradicted Parts 17-18's fee-based ladder:
the $1 field (178,359) is larger than the $3 (158,541), and the $0.10 (2,972)
is 40x smaller than anything else rather than one step down a gradient.

Build order runs SMALLEST field first. Cross-contest uniqueness cuts mean each
successive set is solved against a shrinking space, so the set that should be
most projection-forward has to be cut first. That is what makes Part 25's
"projection rises as field size falls" true by construction rather than by luck.

EVERY LINEUP IS UNIQUE ACROSS ALL FIVE SETS. The user diversifies his entry
portfolio deliberately, so `existing` carries the running portfolio into every
subsequent solve.

PLACEHOLDER MODE (--no-projections)
Before the weekly projection file exists, the runner can still produce a
structurally valid upload using DK's own AvgPointsPerGame and a presumed-starter
QB list derived from salary. Every lineup is legal and rule-compliant, and the
player choices are MEANINGLESS. It exists to prove the upload format, not to be
entered as-is. The runner says so loudly, in the console and in the filename.
"""

import os
import sys

import dfs_main_build as B
import dfs_main_export as EX
import dfs_main_ingest as ING
import dfs_main_qa as QA

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Week 1 2026 counts, supplied by the user. Update every week — these drive the
# leverage ladder, so a stale number silently mis-tunes the whole portfolio.
FIELD_SIZES = {"$5": 832_342, "$1": 178_359, "$3": 158_541,
               "$0.25": 118_906, "$0.10": 2_972}

# label, DK fee string, lineups, min_stack, randomness, salary-banded?
# Ordered smallest field first — see the build-order note above.
#
# Differentiation is driven by JITTER, not by forcing lineups N players apart.
# min_unique stays at 1 everywhere, which is all "no lineup repeats" requires;
# the leverage ladder then rides on how hard each set's projections are shaken,
# scaling with field size. This is how the user's own solver already works, and
# it is also the only version that solves: min_unique of 3-4 stacked on top of
# exposure caps, pool caps and a 2-deep stack requirement went infeasible around
# lineup 18 of the third set.
CONTEST_PLAN = (
    ("$0.10", "$0.10", 20, 1, 0.03, False),  # 2,972 — Part 17: do not leave salary here
    ("$0.25", "$0.25", 20, 1, 0.05, True),   # 118,906
    ("$3",    "$3",    20, 2, 0.06, True),   # 158,541
    ("$1",    "$1",    20, 2, 0.06, True),   # 178,359 — largest of the four
    ("MILLY", "$5",    10, 2, 0.09, True),   # 832,342 — its own set, ceiling only
)

# Exposure ceilings that make the Part 9/10/11/13 POOL targets achievable.
# These are deliberately tighter than the "elite plays may reach 40-60%"
# allowance in Parts 9/10/16, because without real projections there is no
# elite play to protect. Once the weekly projections land, the tier framework
# sets these per player and the ceilings loosen for the genuine studs.
EXPOSURE_CAPS = {"QB": 0.40, "RB": 0.45, "WR": 0.35, "TE": 0.45, "DST": 0.20}
# Pool ceilings. The user is explicit these are NOT black-and-white: "each
# week is different and you could go slightly above or below the target when
# it makes sense." So QA carries a +/-2 tolerance and only reports a real miss
# outside it. The SOLVER still aims at the stated maxima, because they turn out
# to be reachable -- headroom of +1 or +2 here just made every pool drift up to
# whatever ceiling it was given without improving anything.
POOL_CAPS = {"RB": 10, "WR": 16, "TE": 6, "DST": 7}

# User rules added 2026-08-26, neither of which appears in MainRules_vNext.1:
MIN_CORRELATED = 5  # players in games contributing 2+ to the lineup
MIN_UNIQUE = 2      # players differing between ANY two lineups, all week
MAX_TE = 1          # 2 allows TE at FLEX -- a deliberate weekly call, ~1-2
                    # weeks a year (free-square TE, or brutal salaries)
RB_DST_BONUS = 0.5  # points for an RB alongside his own DST. ~0.3% of a
                    # lineup: enough to break a tie, not to steer the build

SEED = 20260913     # deterministic portfolios; bump to reshuffle


def approved_qb_pool(players, size=5):
    """
    The Part 2 approved starter pool, as dk_ids.

    Preferred path: a QB carries a row in the projection source, which lists
    exactly one QB per team and has therefore already resolved starters.

    Fallback (placeholder mode): the highest-salaried QB per team. DK prices
    starters well clear of backups, so this is a reasonable proxy — but it IS a
    proxy, it is never presented as verified, and it is the single thing most
    worth a human glance before any real money rides on it.
    """
    qbs = [p for p in players if p.pos == "QB" and p.playable]
    projected = [q for q in qbs if q.proj > 0]

    if projected:
        ranked = sorted(projected, key=lambda q: -q.proj)
        return frozenset(q.dk_id for q in ranked[:size]), "projection source", ranked[:size]

    best = {}
    for q in qbs:
        if q.team not in best or q.salary > best[q.team].salary:
            best[q.team] = q
    ranked = sorted(best.values(), key=lambda q: -q.salary)
    return (frozenset(q.dk_id for q in ranked[:size]),
            "PRESUMED starters (highest salary per team) -- UNVERIFIED",
            ranked[:size])


def run(entries, salaries, projections=None, out_dir=None):
    out_dir = out_dir or os.path.dirname(os.path.abspath(entries))

    if projections:
        players, contests, findings = ING.load_slate(entries, salaries, projections)
    else:
        contests, dk_pool = ING.load_dk_entries(entries)
        status = ING.load_dk_salaries(salaries)
        players, report = ING.join_players(dk_pool, [], status)
        findings = ING.validate(players, contests, report)

    for f in findings:
        if not f.startswith("INFO"):
            print(f)
    gate_fails = sum(1 for f in findings if f.startswith("FAIL"))

    scored = [p for p in players if p.proj > 0]
    placeholder = not scored
    if placeholder:
        print("\n" + "=" * 74)
        print("PLACEHOLDER MODE -- no projections joined.")
        print("Objective is DK's AvgPointsPerGame (last season, rookies at zero).")
        print("Lineups will be LEGAL and RULE-COMPLIANT but the players are")
        print("MEANINGLESS. Do not leave these in ahead of lock.")
        print("=" * 74)
        score = lambda p: p.avg_pts
        pool = [p for p in players if p.playable and (p.avg_pts > 0 or p.pos == "DST")]
    else:
        score = lambda p: p.proj
        pool = [p for p in players if p.playable and p.proj > 0]
        if gate_fails:
            print("\nValidation gate FAILED -- not building.")
            return 1

    # Measured 2026-08-26: the full rule set solves cleanly at 120+ players and
    # fails outright around 100, even after every permitted relaxation. The
    # correlation floor and the D/ST ban are what consume the room.
    if len(pool) < 120:
        print("
WARN  pool is only %d players. The full rule set needs ~120+; "
              "below ~100 it cannot solve at all." % len(pool))

    approved, source, qb_list = approved_qb_pool(pool)
    print("\nApproved QB pool (%s):" % source)
    for q in qb_list:
        print("   %-22s %-4s $%-7s %s" % (q.name, q.team, format(q.salary, ","),
                                          "proj %.1f" % q.proj if q.proj else
                                          "avg %.1f" % q.avg_pts))

    print("\nBuilding (smallest field first, uniqueness carried across sets):")
    sets, portfolio = [], []
    for i, (label, fee, n, stack, jitter, banded) in enumerate(CONTEST_PLAN):
        cfg = B.BuildConfig(
            n_lineups=n, approved_qbs=approved, min_stack=stack,
            require_bringback=True, min_unique=MIN_UNIQUE,
            min_correlated=MIN_CORRELATED, max_te=MAX_TE,
            ban_dst_vs_players=True, rb_dst_bonus=RB_DST_BONUS,
            randomness=jitter, seed=SEED + i,
            max_exposure=EXPOSURE_CAPS, max_pool=POOL_CAPS,
            salary_schedule=B.salary_bands(n) if banded else ())
        lus, note = B.build_portfolio(pool, cfg, score=score, existing=portfolio)
        print("  %-6s %2d lineups  field %9s  %s"
              % (label, len(lus), format(FIELD_SIZES.get(fee, 0), ","), note or "ok"))
        if len(lus) < n:
            print("     SHORT -- %s" % note)
            return 1
        sets.append((label, lus))
        portfolio.extend(lus)

    print("\n=== Part 24 QA ===")
    fails = 0
    for label, lus in sets:
        for f in QA.qa_contest(lus, label, approved_qbs=approved,
                               min_correlated=MIN_CORRELATED):
            if f.startswith("FAIL"):
                fails += 1
                print(f)
            elif f.startswith("WARN"):
                print(f)

    print("\n=== Part 25 cross-contest ===")
    for line in QA.qa_portfolio(sets):
        print(line)

    uniq_all = len({lu.ids for lu in portfolio})
    print("\nunique lineups across all sets: %d/%d" % (uniq_all, len(portfolio)))
    if uniq_all != len(portfolio):
        print("FAIL  lineups repeat across sets")
        fails += 1

    if fails:
        print("\n%d QA FAIL -- refusing to write the upload file (Part 24)." % fails)
        return 1

    tag = "PLACEHOLDER_" if placeholder else ""
    upload = os.path.join(out_dir, "DKEntries_UPLOAD_%s.csv" % tag.rstrip("_")
                          if placeholder else "DKEntries_UPLOAD.csv")
    review = os.path.join(out_dir, "%sreview.csv" % tag)

    plan = EX.assign_contests(contests, {fee: lus for (_, fee, *_), (_, lus)
                                         in zip(CONTEST_PLAN, sets)})
    summary = EX.write_entries(entries, upload, plan,
                               players_by_id={p.dk_id: p for p in players})
    EX.write_review_csv(review, sets)

    print("\nround-trip verify PASSED")
    print("  %s" % summary["path"])
    print("  %d entries / %d contests / %d unique lineups"
          % (summary["entries"], len(summary["contests"]), summary["unique_lineups"]))
    print("  %s" % review)
    return 0


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    no_proj = "--no-projections" in argv

    if len(args) >= 2:
        entries, salaries = args[0], args[1]
        projections = None if no_proj else (args[2] if len(args) > 2 else None)
    else:
        entries = ING._newest(r"^DKEntries.*\.csv$")
        salaries = ING._newest(r"^DKSalaries.*\.csv$")
        projections = None if no_proj else ING._newest(r"Projections.*Main Slate.*\.csv$")
        if not entries or not salaries:
            print("FAIL  need DKEntries and DKSalaries in %s" % ING.SLATE_DIR)
            return 2
    return run(entries, salaries, projections)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
