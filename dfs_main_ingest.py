"""
dfs_main_ingest.py — Ingest + validation gate for DraftKings NFL MAIN SLATE DFS.

Implements Part 1 (zero-assumption data mode) and Part 4 (validation gate) of
MainRules_vNext.1. Nothing here builds lineups. Its only job is to turn the
weekly file drop into ONE authoritative player table, and to refuse to produce
that table quietly when something is wrong.

    python dfs_main_ingest.py            # print the validation report

Main Slate only. Showdown/mini-slate rules do not apply anywhere in this file.

WHY THIS EXISTS AS ITS OWN STEP
The user's previous (chat-based) process produced a Week 13 portfolio whose
defects were all mechanically detectable and none of which threw an error:
TE pool of 9 against a hard max of 6, three orphan QBs, RB/WR pools ~2x spec,
and every player name serialized with a trailing "." that would have broken a
DK bulk upload. Silent failure is the enemy — same lesson as pipeline_audit.py.

THREE WEEKLY FILES, AND WHY EACH IS NEEDED
  projections  — salary, proj, floor, ceiling, and BOTH ownership columns.
                 Lists exactly one QB per team (source resolves starters).
  DKEntries    — Entry IDs per contest, AND an embedded copy of the full DK
                 player pool carrying `Game Info`. That kickoff time is the
                 ONLY source for Part 12's latest-start FLEX rule.
  DKSalaries   — adds the `Status` column (Q/OUT/IR/D) that DKEntries lacks.

THE ID QUESTION — UNRESOLVED AS OF 2026-08-26
The projections `id` column is very likely the DK Player ID: 8-digit, unique,
and blocked by position exactly the way DK assigns them within a draft group.
It is NOT yet confirmed, because we have never held a projections file and a
salary file from the SAME week. So join_players() tries ID first, falls back to
name, and REPORTS which path carried the join. Do not delete the name path on
the first week the ID join succeeds — one good week is not proof.
"""

import csv
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from nfl_props_data import NAME_ALIASES, normalize_name

# Windows console is cp1252 and will hard-crash on a stray non-ASCII char
# mid-report. Fix it once here rather than policing every print().
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SLATE_DIR = r"C:\Users\corpo\OneDrive\Desktop\Main Slate Files"

# DK classic roster. FLEX takes RB/WR/TE. No kicker.
ROSTER = {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "FLEX": 1, "DST": 1}
SALARY_CAP = 50_000

# Team abbreviation collisions confirmed across the user's own sources: the
# weekly projections file writes the Rams as "LA" where DK writes "LAR".
# Left side = anything a non-DK source might emit; right side = DK's spelling.
TEAM_ALIASES = {
    "LA": "LAR", "STL": "LAR", "SD": "LAC", "OAK": "LV",
    "JAC": "JAX", "WSH": "WAS", "WFT": "WAS", "ARZ": "ARI",
    "CLV": "CLE", "BLT": "BAL", "HST": "HOU", "SL": "LAR",
}

# DK marks unavailable players in DKSalaries.Status. OUT/IR are hard excludes;
# Q/D are judgment calls that belong in the Part 22 late-news pass, not here.
STATUS_HARD_EXCLUDE = {"OUT", "IR"}
STATUS_FLAG = {"Q", "D"}


def _team(abbr):
    a = (abbr or "").strip().upper()
    return TEAM_ALIASES.get(a, a)


def _key(name):
    """Normalized join key, with the hand-maintained alias table applied."""
    k = normalize_name(name)
    return NAME_ALIASES.get(k, k)


def _money(s):
    return int(float(re.sub(r"[$,]", "", (s or "0").strip()) or 0))


def _pct(s):
    return float(re.sub(r"[%,]", "", (s or "0").strip()) or 0.0)


def _num(s):
    return float(re.sub(r"[,]", "", (s or "0").strip()) or 0.0)


# ── Authoritative row ─────────────────────────────────────────────────────────

@dataclass
class Player:
    """One row of the Part 4 authoritative slate table."""
    dk_id: str
    name: str
    # The upload string, "Name (ID)". Rebuilt from a STRIPPED name rather than
    # copied from DK, because DK's two files disagree with each other:
    #   DKSalaries          Name='Chargers'   Name+ID='Chargers (43728525)'
    #   DKEntries pool      Name='Chargers '  Name+ID='Chargers  (43728525)'
    # Every DST in the entries export carries a trailing space; none in the
    # salary export do. DK presumably accepts its own double-spaced form, but
    # only the single-spaced form is known-good from both files, so normalize.
    name_id: str
    pos: str                       # QB/RB/WR/TE/DST
    team: str
    opp: str
    salary: int
    roster_slots: tuple            # ('RB','FLEX') straight from DK eligibility
    game: str                      # 'NO@DET'
    kickoff: datetime | None
    status: str                    # '', 'Q', 'OUT', 'IR', 'D'
    proj: float = 0.0
    floor: float = 0.0
    ceiling: float = 0.0
    own_large: float = 0.0
    own_small: float = 0.0
    joined_by: str = "unmatched"   # 'id' | 'name' | 'unmatched'
    # DK's own season-average scoring, straight off the salary file. NOT a
    # projection — it ignores the offseason and zeroes out rookies — but it is
    # real, DK-sourced, and available before any projection file exists, which
    # makes it the right fuel for structural smoke tests.
    avg_pts: float = 0.0

    @property
    def flex_eligible(self):
        return "FLEX" in self.roster_slots

    @property
    def playable(self):
        return self.status not in STATUS_HARD_EXCLUDE


@dataclass
class Contest:
    name: str
    contest_id: str
    fee: str
    entry_ids: list = field(default_factory=list)

    @property
    def n(self):
        return len(self.entry_ids)

    @property
    def is_cash(self):
        """Double Ups are cash games — user enters those separately."""
        return "double up" in self.name.lower()


# ── Loaders ───────────────────────────────────────────────────────────────────

def load_dk_entries(path):
    """
    Parse a DKEntries export into (contests, dk_pool).

    The file is two tables sharing one CSV: entry rows on the left, and an
    embedded copy of the DK player pool starting at the row/col where a cell
    reads 'Position'. Verified byte-identical ID set to DKSalaries, and every
    embedded row carries Game Info. Located by search, never a fixed offset —
    DK has moved these columns before.
    """
    raw = list(csv.reader(open(path, encoding="utf-8-sig")))

    contests = {}
    for r in raw[1:]:
        if not r or not r[0].strip().isdigit():
            continue
        cid = r[2].strip()
        c = contests.setdefault(cid, Contest(r[1].strip(), cid, r[3].strip()))
        c.entry_ids.append(r[0].strip())

    hr = hc = None
    for i, r in enumerate(raw):
        for j, cell in enumerate(r):
            if cell.strip() == "Position" and j > 4:
                hr, hc = i, j
                break
        if hr is not None:
            break
    if hr is None:
        raise ValueError(os.path.basename(path) + ": embedded DK player pool not "
                         "found — cannot source kickoff times (Part 12 needs them).")

    hdr = [c.strip() for c in raw[hr][hc:hc + 9]]
    pool = {}
    for r in raw[hr + 1:]:
        if len(r) < hc + 9:
            continue
        rec = dict(zip(hdr, [c.strip() for c in r[hc:hc + 9]]))
        if not rec.get("ID", "").isdigit():
            continue
        pool[rec["ID"]] = rec
    return list(contests.values()), pool


def load_dk_salaries(path):
    """DK ID -> Status. The one field DKEntries does not carry."""
    return {r["ID"].strip(): (r.get("Status") or "").strip().upper()
            for r in csv.DictReader(open(path, encoding="utf-8-sig"))
            if r.get("ID", "").strip().isdigit()}


def load_projections(path):
    """
    Parse the weekly projections CSV.

    Every DST name in this file carries trailing whitespace ("Texans "). Left
    unstripped it concatenates to "Texans  (41199346)" — the double space seen
    throughout last season's lineup files. Stripped once, here.
    """
    out = []
    for r in csv.DictReader(open(path, encoding="utf-8-sig")):
        name = (r.get("Player") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "pos": (r.get("DK Pos") or "").strip().upper(),
            "team": _team(r.get("Team")),
            "opp": (r.get("Opp") or "").strip(),
            "salary": _money(r.get("DK Salary")),
            "proj": _num(r.get("DK Proj")),
            "floor": _num(r.get("DK Floor")),
            "ceiling": _num(r.get("DK Ceiling")),
            "own_small": _pct(r.get("Small Field")),
            "own_large": _pct(r.get("Large Field")),
            "id": (r.get("id") or "").strip(),
        })
    return out


# ── Join ──────────────────────────────────────────────────────────────────────

def _parse_kickoff(game_info):
    """'NO@DET 09/13/2026 01:00PM ET' -> ('NO@DET', datetime). Time is ET."""
    m = re.match(r"^(\S+)\s+(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2}(?:AM|PM))",
                 (game_info or "").strip())
    if not m:
        return (game_info or "").strip(), None
    try:
        return m.group(1), datetime.strptime(m.group(2) + " " + m.group(3),
                                             "%m/%d/%Y %I:%M%p")
    except ValueError:
        return m.group(1), None


def join_players(dk_pool, projections, status=None):
    """
    Build the authoritative table. DK is the spine — a player DK will not let
    you roster cannot be rescued by appearing in a projection file.

    Returns (players, join_report). Every DK player comes back, including ones
    the projection source omitted; those carry proj 0.0 and joined_by
    'unmatched' so the caller can see them rather than silently lose them.
    """
    status = status or {}

    by_id = {p["id"]: p for p in projections if p["id"]}
    by_name = defaultdict(list)
    for p in projections:
        by_name[(_key(p["name"]), p["pos"], p["team"])].append(p)

    players, used = [], set()
    counts = Counter()

    for dk_id, rec in dk_pool.items():
        pos = rec["Position"].strip().upper()
        team = _team(rec["TeamAbbrev"])
        game, kickoff = _parse_kickoff(rec["Game Info"])
        opp = next((t for t in game.split("@") if _team(t) != team), "")

        match, how = by_id.get(dk_id), "id"
        if match is None:
            cands = by_name.get((_key(rec["Name"]), pos, team), [])
            match, how = (cands[0], "name") if cands else (None, "unmatched")
        if match is not None:
            used.add(id(match))
        counts[how] += 1

        players.append(Player(
            dk_id=dk_id,
            name=rec["Name"].strip(),
            name_id="%s (%s)" % (rec["Name"].strip(), dk_id),
            pos=pos,
            team=team,
            opp=_team(opp),
            salary=_money(rec["Salary"]),
            roster_slots=tuple(rec["Roster Position"].split("/")),
            game=game,
            kickoff=kickoff,
            status=status.get(dk_id, ""),
            proj=match["proj"] if match else 0.0,
            floor=match["floor"] if match else 0.0,
            ceiling=match["ceiling"] if match else 0.0,
            own_large=match["own_large"] if match else 0.0,
            own_small=match["own_small"] if match else 0.0,
            joined_by=how,
            avg_pts=_num(rec.get("AvgPointsPerGame")),
        ))

    orphans = [p for p in projections if id(p) not in used]
    total = sum(counts.values()) or 1
    report = {
        "matched_by_id": counts["id"],
        "matched_by_name": counts["name"],
        "unmatched": counts["unmatched"],
        "id_join_rate": counts["id"] / total,
        # A projected player DK never listed. Usually harmless (different week,
        # or a name the alias table has not learned) but always worth eyeballing.
        "projection_orphans": [p["name"] + " (" + p["pos"] + "/" + p["team"] + ")"
                               for p in orphans],
    }
    return players, report


# ── Validation gate (Part 4) ──────────────────────────────────────────────────

def validate(players, contests, report):
    """
    Return a list of finding strings prefixed FAIL / WARN / INFO.

    FAIL means the slate table is provably not fit to build from. The caller
    must not proceed past a FAIL — Part 29: do not silently loosen rules
    because the build is difficult.
    """
    out = []

    def F(m):
        out.append("FAIL  " + m)

    def W(m):
        out.append("WARN  " + m)

    def I(m):
        out.append("INFO  " + m)

    live = [p for p in players if p.playable]
    teams = sorted({p.team for p in live})
    games = sorted({p.game for p in live if p.game})

    I("DK pool %d players | playable %d | %d teams | %d games"
      % (len(players), len(live), len(teams), len(games)))
    I("positions: " + ", ".join(k + " " + str(v) for k, v in
                                sorted(Counter(p.pos for p in live).items())))

    # -- join integrity --
    if report["id_join_rate"] >= 0.95:
        I("projections `id` matched DK ID on %.1f%% of the pool -- CONFIRMED as "
          "the DK Player ID" % (report["id_join_rate"] * 100))
    elif report["matched_by_id"] == 0:
        W("projections `id` matched NOTHING -- join fell back to names entirely. "
          "Expected if the two files are from different weeks; investigate if not.")
    else:
        F("projections `id` matched only %.1f%% of the pool -- partial ID match "
          "means the column is not what we think it is"
          % (report["id_join_rate"] * 100))

    if report["unmatched"]:
        pct = report["unmatched"] / max(len(players), 1)
        (W if pct < 0.60 else F)(
            "%d DK players (%.0f%%) got no projection row"
            % (report["unmatched"], pct * 100))

    scored = [p for p in live if p.proj > 0]
    if not scored:
        F("no player carries a projection -- nothing can be built")
    else:
        I("projection coverage: %d/%d playable players; %d project >= 5.0"
          % (len(scored), len(live), sum(1 for p in scored if p.proj >= 5)))

    # -- Part 2: QB eligibility --
    dk_qbs = [p for p in live if p.pos == "QB"]
    approved = [p for p in dk_qbs if p.proj > 0]
    by_team = defaultdict(list)
    for p in approved:
        by_team[p.team].append(p)
    I("QB gate: DK lists %d QBs; %d carry a projection across %d teams"
      % (len(dk_qbs), len(approved), len(by_team)))
    for t in teams:
        if t not in by_team:
            W("QB gate: %s has no projected QB -- team unusable until resolved" % t)
    for t, v in sorted(by_team.items()):
        if len(v) > 1:
            F("QB gate: %s has %d projected QBs (%s) -- starter ambiguous, "
              "Part 2 says flag not guess"
              % (t, len(v), ", ".join(x.name for x in v)))
    for p in sorted([q for q in approved if q.salary <= 5000], key=lambda q: q.salary):
        W("QB gate: confirm %s (%s) starts -- $%s / proj %s and no fallback row "
          "exists if the source is wrong" % (p.name, p.team, format(p.salary, ","), p.proj))

    # -- ownership sanity --
    for label, attr in (("Large Field", "own_large"), ("Small Field", "own_small")):
        tot = sum(getattr(p, attr) for p in live)
        if tot == 0:
            W(label + " ownership is entirely zero -- leverage rules cannot run")
        elif not 700 <= tot <= 1100:
            F("%s ownership sums to %.0f%% across 9 roster slots (expected ~900%%) "
              "-- not a normalized pOwn column" % (label, tot))
        else:
            I("%s ownership sums to %.0f%% -- normalized, usable" % (label, tot))

    # -- Part 12: kickoff times --
    no_time = [p for p in live if p.kickoff is None]
    if no_time:
        F("%d playable players have no kickoff time -- Part 12's latest-start "
          "FLEX rule cannot be satisfied" % len(no_time))
    else:
        waves = sorted({p.kickoff for p in live})
        I("kickoff waves: " + " | ".join(
            "%s x%d games" % (k.strftime("%I:%M%p"),
                              len({p.game for p in live if p.kickoff == k}))
            for k in waves))
        latest = waves[-1]
        late_teams = sorted({p.team for p in live if p.kickoff == latest})
        I("late-swap FLEX pool comes from %d teams: %s"
          % (len(late_teams), " ".join(late_teams)))

    # -- injuries --
    st = Counter(p.status for p in players if p.status)
    if st:
        I("DK status: " + ", ".join(k + " " + str(v) for k, v in sorted(st.items()))
          + "  (OUT/IR excluded; Q/D -> Part 22 late-news pass)")
    flagged = [p for p in live if p.status in STATUS_FLAG and p.proj >= 8]
    for p in sorted(flagged, key=lambda x: -x.proj)[:12]:
        W("status %s: %s (%s/%s) proj %s -- confirm before lock"
          % (p.status, p.name, p.pos, p.team, p.proj))

    # -- salary / structural --
    if any(p.salary <= 0 for p in live):
        F("some playable players carry salary 0 -- DK file is malformed")
    dupes = [k for k, v in Counter(p.dk_id for p in players).items() if v > 1]
    if dupes:
        F("duplicate DK IDs in the pool: %s" % dupes)
    bad_slots = [p.name for p in live
                 if p.pos in ("RB", "WR", "TE") and not p.flex_eligible]
    if bad_slots:
        W("%d skill players are not FLEX-eligible per DK (e.g. %s) -- unusual, "
          "verify" % (len(bad_slots), bad_slots[:3]))

    # -- contests --
    gpp = [c for c in contests if not c.is_cash]
    cash = [c for c in contests if c.is_cash]
    I("contests: %d GPP (%d entries), %d cash (%d entries, ignored)"
      % (len(gpp), sum(c.n for c in gpp), len(cash), sum(c.n for c in cash)))
    for c in sorted(gpp, key=lambda x: -x.n):
        I("  %-7s%4d entries  %s  %s" % (c.fee, c.n, c.contest_id, c.name[:52]))

    if report["projection_orphans"]:
        W("%d projected players absent from the DK pool (first 5: %s)"
          % (len(report["projection_orphans"]), report["projection_orphans"][:5]))
    return out


# ── Entry point ───────────────────────────────────────────────────────────────

def _newest(pattern, folder=SLATE_DIR):
    hits = [os.path.join(folder, f) for f in os.listdir(folder)
            if re.search(pattern, f, re.I)]
    return max(hits, key=os.path.getmtime) if hits else None


def load_slate(entries_path, salaries_path, projections_path):
    contests, dk_pool = load_dk_entries(entries_path)
    status = load_dk_salaries(salaries_path)
    projections = load_projections(projections_path)
    players, report = join_players(dk_pool, projections, status)
    findings = validate(players, contests, report)
    return players, contests, findings


def main(argv):
    if len(argv) == 4:
        entries, salaries, projections = argv[1:4]
    else:
        entries = _newest(r"^DKEntries.*\.csv$")
        salaries = _newest(r"^DKSalaries.*\.csv$")
        projections = _newest(r"Projections.*Main Slate.*\.csv$")
        missing = [n for n, p in (("DKEntries", entries), ("DKSalaries", salaries),
                                  ("projections", projections)) if not p]
        if missing:
            print("FAIL  missing from %s: %s" % (SLATE_DIR, ", ".join(missing)))
            print("      usage: python dfs_main_ingest.py "
                  "<entries.csv> <salaries.csv> <projections.csv>")
            return 2

    for label, p in (("entries", entries), ("salaries", salaries),
                     ("projections", projections)):
        print("  %-12s%s" % (label, os.path.basename(p)))
    print()

    players, contests, findings = load_slate(entries, salaries, projections)
    for f in findings:
        print(f)

    fails = sum(1 for f in findings if f.startswith("FAIL"))
    warns = sum(1 for f in findings if f.startswith("WARN"))
    print("\n%d FAIL / %d WARN" % (fails, warns))
    if fails:
        print("Validation gate BLOCKED -- do not build until every FAIL is resolved.")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
