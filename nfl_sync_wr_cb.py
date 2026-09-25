"""
nfl_sync_wr_cb.py — pull this week's WR-CB matchup sheet out of the DFS folders
into the repo, so the automated runs can see it.

WHY THIS IS A COPY AND NOT A READ
---------------------------------
The owner already downloads this sheet every week for DFS, so asking them to
save a second copy is duplicated work. But the model does NOT run on this
machine — the morning, snapshot and nightly workflows run on GitHub's servers,
which cannot see a local Desktop or OneDrive folder. Anything the pipeline
reads has to be committed to the repo. So this finds the newest sheet, checks
it is actually for the upcoming week, copies it in and pushes it.

Run it once a week, after the DFS sheet is downloaded:

    python nfl_sync_wr_cb.py

It refuses to copy a sheet that does not match the upcoming week's games,
because that is the exact failure that went unnoticed for two weeks: a stale
sheet does not error, it just silently contributes nothing (wr_cb_factor only
applies a row when its defense is the receiver's real opponent).
"""

import argparse
import os
import shutil
import subprocess
import sys

import nfl_data_py as nfl_data

import nfl_props_data as props_data

# Where the DFS work already keeps these sheets. Both are searched recursively,
# so the weekly file is found whether it is loose in the folder or already
# filed into an archive/<date>/ subfolder.
SEARCH_ROOTS = [
    os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop", "Main Slate Files"),
    os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop", "Showdown Files"),
]

# Deliberately loose: any PDF whose name mentions both WR and CB. The file gets
# named differently week to week ("wr-cb-matchup.pdf", "WR-CB Matchup Main Slate
# Week 2.pdf", "WR vs CB Matchups Week 3.pdf"), and an exact-phrase list missed
# the third of those. Over-matching is harmless — every candidate is parsed and
# checked against this week's actual games below, which is the real filter. A
# name test that silently skips the right file is the expensive kind of wrong.


def upcoming_week_games(season: int) -> tuple[int, set]:
    """(week number, {frozenset({home, away})}) for the next unplayed week."""
    s = nfl_data.import_schedules([season])
    s = s[s["game_type"] == "REG"]
    unplayed = s[s["home_score"].isna()]
    if unplayed.empty:
        raise SystemExit("No unplayed regular-season games left — nothing to sync.")
    week = int(unplayed["week"].min())
    games = {frozenset((g["home_team"], g["away_team"]))
             for _, g in s[s["week"] == week].iterrows()}
    return week, games


def candidates() -> list[str]:
    out = []
    for root in SEARCH_ROOTS:
        if not os.path.isdir(root):
            print(f"  [warn] folder not found, skipping: {root}")
            continue
        for dirpath, _, files in os.walk(root):
            for f in files:
                low = f.lower()
                if low.endswith(".pdf") and "wr" in low and "cb" in low:
                    out.append(os.path.join(dirpath, f))
    return sorted(out, key=lambda p: os.path.getmtime(p), reverse=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--no-push", action="store_true",
                    help="copy into the repo but do not commit/push")
    ap.add_argument("--force", action="store_true",
                    help="copy even if no row matches the upcoming week (use only "
                         "when you know the sheet is right and the check is wrong)")
    args = ap.parse_args()

    week, games = upcoming_week_games(args.season)
    print(f"Upcoming week: {week} ({len(games)} games)")

    found = candidates()
    if not found:
        raise SystemExit(f"No WR-CB PDF found under: {', '.join(SEARCH_ROOTS)}")
    print(f"Found {len(found)} candidate sheet(s); newest first:")

    best = None
    for path in found[:12]:
        try:
            rows = props_data.load_wr_cb_matchups(path)
        except Exception as e:
            print(f"  [skip] {os.path.basename(path)} — unreadable ({e})")
            continue
        live = [r for r in rows
                if frozenset((r.get("off_team"), r.get("def_team"))) in games]
        print(f"  {len(rows):4} rows, {len(live):4} match Week {week}  "
              f"{os.path.basename(path)}")
        if live and best is None:
            best = (path, rows, live)

    if best is None:
        if not args.force:
            raise SystemExit(
                f"\nNo sheet matches Week {week}'s games. The newest download is "
                f"probably still last week's — grab this week's and re-run. "
                f"(--force overrides.)")
        best = (found[0], [], [])

    path, rows, live = best
    dest = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        props_data.WR_CB_PDF_PATH)
    shutil.copy2(path, dest)
    print(f"\nCopied -> {os.path.basename(dest)}")
    print(f"  source : {path}")
    print(f"  {len(rows)} rows, {len(live)} for Week {week}, "
          f"{len({r.get('def_team') for r in rows})} defenses covered")

    if args.no_push:
        print("\n--no-push: not committing. The automated runs will NOT see this "
              "until it is committed and pushed.")
        return

    repo = os.path.dirname(os.path.abspath(__file__))
    subprocess.run(["git", "add", props_data.WR_CB_PDF_PATH], cwd=repo, check=True)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo)
    if staged.returncode == 0:
        print("\nAlready up to date — this exact sheet is committed.")
        return
    subprocess.run(["git", "commit", "-m",
                    f"NFL: WR-CB matchup sheet for Week {week}"], cwd=repo, check=True)
    subprocess.run(["git", "push"], cwd=repo, check=True)
    print(f"\nPushed. The next scheduled run will use the Week {week} sheet.")


if __name__ == "__main__":
    main()
