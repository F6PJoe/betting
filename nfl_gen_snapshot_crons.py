"""
nfl_gen_snapshot_crons.py — regenerate the snapshot cron grid from the REAL
remaining schedule.

Run this and paste the output of crons.txt into the `schedule:` block of
.github/workflows/nfl_line_snapshots.yml:

    python nfl_gen_snapshot_crons.py

WHY THIS IS GENERATED AND NOT HAND-MAINTAINED
---------------------------------------------
The grid has to change whenever the clocks change or the schedule gains a slot,
and a hand-kept list silently rots. The 2026 season crossed the 1 Nov DST
change with an EDT-built grid: every kickoff moved an hour later in UTC (the
Sunday slate alone, 93 games, went 17:00 -> 18:00 UTC) and the old crons would
have fired an hour early on all of them. Holiday games make it worse — the
Thanksgiving, Black Friday, Saturday and Christmas slots do not exist in the
weekly pattern at all, so they can only come from the schedule itself.

PLACEMENT
---------
GitHub cron fires LATE — measured on this repo: median 54 min, max 302. So a
cron scheduled at T-x actually lands near T-(x-54). We want the LAST snapshot
before kickoff to sit inside T-90..T-0, after the inactive report, because that
is the line CLV should be measured against. Leads run T-230..T-20 per window,
putting the median landing zone across T-176..T+34 and guaranteeing several
inside the window that matters.

Nothing is scheduled on :00 or :30 — measured, every worst-case delay came from
those slots, which are the most contested minutes on GitHub's scheduler.
"""

import os
import sys
import collections
import datetime as dt

import nfl_data_py as nfl

SEASON = 2026
DST_ENDS = dt.date(2026, 11, 1)          # EDT (UTC-4) before, EST (UTC-5) after
# LEADS, in minutes before kickoff. Consecutive gaps must stay <= 60 or a lag
# value can slip BETWEEN two firings and miss the window entirely.
#
# A firing scheduled at lead l lands at (l - lag) minutes before kickoff, so it
# lands inside the final 60 minutes exactly when lag is in [l-60, l). Union
# those intervals across the leads below and the safe range is a continuous
# 0..350 minutes — i.e. any lag up to 350 is guaranteed to put at least one
# firing inside the hour before kickoff.
#
# WHY IT REACHES 350: measured over 100 real scheduled runs of this workflow,
# lag was median 61, 90th pct 166, 95th pct 197, max 243. A 230-minute ceiling
# left about 1% of firings outside it. The danger is not one slow run but
# CORRELATED delay — when GitHub's scheduler is congested every cron in the
# window is late together, so the per-run 1% is not the per-game risk. The
# early leads cost almost nothing (they land far from kickoff, so they are
# cheap lines-only firings) and they buy the tail.
LEADS = [350, 320, 290, 260, 230, 200, 170, 155, 140, 125, 110, 95, 80, 65, 50, 35, 20]
BAD_MINUTES = {0, 30}
PY2CRON = {0: 1, 1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 0}   # python Mon=0 -> cron Mon=1


def kickoffs(from_date: dt.date) -> list:
    """Every remaining regular-season kickoff, in UTC, offset-correct."""
    s = nfl.import_schedules([SEASON])
    s = s[s["game_type"] == "REG"]
    out = []
    for _, g in s.iterrows():
        try:
            d = dt.date.fromisoformat(str(g["gameday"]))
            hh, mm = str(g["gametime"]).split(":")
        except (ValueError, TypeError):
            continue
        if d < from_date:
            continue
        off = -4 if d < DST_ENDS else -5
        out.append(dt.datetime(d.year, d.month, d.day, int(hh), int(mm),
                               tzinfo=dt.timezone(dt.timedelta(hours=off)))
                   .astimezone(dt.timezone.utc))
    return out


def main():
    today = dt.date.today()
    games = kickoffs(today)
    if not games:
        print("No remaining regular-season games.")
        return

    slots = collections.Counter((k.weekday(), k.hour, k.minute) for k in games)
    print(f"remaining games: {len(games)} across {len(slots)} kickoff slots (UTC)")

    # Merge slots within 20 minutes of each other on the same weekday, aiming at
    # the EARLIER kickoff so the later one is still covered.
    merged = []
    for (wd, hh, mm), n in sorted(slots.items()):
        t = hh * 60 + mm
        for m in merged:
            if m["wd"] == wd and abs(m["t"] - t) <= 20:
                m["t"] = min(m["t"], t)
                m["n"] += n
                break
        else:
            merged.append({"wd": wd, "t": t, "n": n})
    print(f"merged into {len(merged)} windows")

    crons = set()
    for m in merged:
        for lead in LEADS:
            tm, wd = m["t"] - lead, m["wd"]
            while tm < 0:                 # rolled back past midnight UTC
                tm += 1440
                wd = (wd - 1) % 7
            hh, mi = divmod(tm, 60)
            if mi in BAD_MINUTES:
                mi += 3
            crons.add((PY2CRON[wd], hh, mi))

    out = "\n".join(f"    - cron: '{mi} {hh} * * {wd}'"
                    for wd, hh, mi in sorted(crons))
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crons.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(out)
    print(f"{len(crons)} crons written to {path}")

    # Simulate: would every remaining game get a snapshot inside the window?
    print("\nsimulated coverage of remaining games:")
    for lag in (0, 54, 150, 250):
        i90 = i30 = none = 0
        for ko in games:
            hits = []
            for wd, hh, mi in crons:
                for delta in (-1, 0):
                    cand = (ko + dt.timedelta(days=delta)).replace(
                        hour=hh, minute=mi, second=0, microsecond=0)
                    if PY2CRON[cand.weekday()] != wd:
                        continue
                    fire = cand + dt.timedelta(minutes=lag)
                    if fire < ko:
                        hits.append((ko - fire).total_seconds() / 60)
            if not hits:
                none += 1
                continue
            best = min(hits)
            i90 += best <= 90
            i30 += best <= 30
        print(f"  lag {lag:3} min: inside90 {i90:3}/{len(games)} "
              f"({i90 / len(games) * 100:5.1f}%)  inside30 {i30:3}  uncovered {none}")


if __name__ == "__main__":
    main()
