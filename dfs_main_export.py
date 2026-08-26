"""
dfs_main_export.py — DraftKings bulk-upload writer for NFL MAIN SLATE DFS.

MainRules_vNext.1 Part 26. Fills a DKEntries export with finished lineups and
writes an upload-ready CSV.

THIS IS THE MODULE THAT COSTS MONEY WHEN IT IS WRONG
Everything upstream fails loudly. This one fails silently: lineups written to
the wrong contest's Entry IDs, a dime set that only replicated to four of five
contests, a name DK cannot parse. None of those throw. All of them are found
on Sunday afternoon, by which point the entries are locked.

So write_entries() ALWAYS round-trips: it re-reads what it just wrote and
reconstructs every lineup from the file on disk, comparing against what it was
asked to write. Anything that does not match is raised, not warned.

DK'S OWN RULES, taken from the Instructions column of the export:
  #4  "Use data from the Name+ID column or the ID column; you cannot use just
       the player's name."  -> we write DK's verbatim Name + ID string.
  #5  "For faster processing only include entries you are changing in the file
       you upload."         -> cash-game rows are dropped, not blanked.

The header row is preserved byte-for-byte from the source export and data rows
are padded to its width, so the uploaded file is structurally identical to what
DK handed out.
"""

import csv
import os
import sys
from collections import defaultdict

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SLOT_ORDER = ("QB", "RB1", "RB2", "WR1", "WR2", "WR3", "TE", "FLEX", "DST")
SLOT_COLS = 9          # QB..DST occupy columns 4..12 of a DKEntries row
FIRST_SLOT_COL = 4


class ExportError(RuntimeError):
    """Raised when the written file does not reproduce the lineups given."""


def assign_contests(contests, sets):
    """
    Map contest_id -> lineups using the caller's {label: lineups} plan.

    `sets` keys are matched against DK's own Entry Fee strings ('$3', '$0.10'),
    so one entry in the plan naturally fans out to every contest at that fee —
    which is exactly how the five Dime Package contests each receive the same
    20 lineups.

    Cash games are excluded here rather than downstream, so a Double Up can
    never receive a GPP lineup even if the caller passes a matching fee.
    """
    plan = {}
    for c in contests:
        if c.is_cash:
            continue
        lus = sets.get(c.fee)
        if not lus:
            continue
        if len(lus) < c.n:
            raise ExportError(
                "contest %s (%s, %s) has %d entries but only %d lineups were "
                "built for it" % (c.contest_id, c.fee, c.name, c.n, len(lus)))
        plan[c.contest_id] = lus[:c.n]
    return plan


def _read_entries(path):
    raw = list(csv.reader(open(path, encoding="utf-8-sig")))
    header = raw[0]
    rows = [r for r in raw[1:] if r and r[0].strip().isdigit()]
    return header, rows


def write_entries(entries_path, out_path, plan, players_by_id=None):
    """
    Write an upload-ready DKEntries CSV.

    `plan` is {contest_id: [Lineup, ...]}; lineups are assigned to that
    contest's Entry IDs in file order. Returns a summary dict.

    Round-trips before returning. Raises ExportError on any mismatch.
    """
    header, rows = _read_entries(entries_path)
    width = len(header)

    queues = {cid: list(lus) for cid, lus in plan.items()}
    written, out_rows = [], []

    for r in rows:
        cid = r[2].strip()
        if cid not in queues or not queues[cid]:
            continue                                  # DK instruction #5
        lu = queues[cid].pop(0)

        row = list(r) + [""] * (width - len(r))
        row = row[:width]
        for i, slot in enumerate(SLOT_ORDER):
            row[FIRST_SLOT_COL + i] = lu.slots[slot].name_id
        out_rows.append(row)
        written.append((r[0].strip(), cid, lu))

    leftover = {c: len(q) for c, q in queues.items() if q}
    if leftover:
        raise ExportError("lineups left unassigned (contest -> count): %s" % leftover)
    if not out_rows:
        raise ExportError("no entry rows matched the plan -- nothing to upload")

    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(out_rows)

    _verify(out_path, written, players_by_id)

    per_contest = defaultdict(int)
    for _, cid, _ in written:
        per_contest[cid] += 1
    return {
        "path": out_path,
        "entries": len(out_rows),
        "contests": dict(per_contest),
        "unique_lineups": len({lu.ids for _, _, lu in written}),
    }


def _verify(path, written, players_by_id):
    """
    Re-read the file from disk and reconstruct every lineup, comparing against
    what we intended to write. This is the whole point of the module.
    """
    header, rows = _read_entries(path)
    if len(rows) != len(written):
        raise ExportError("wrote %d rows but re-read %d" % (len(written), len(rows)))

    for row, (entry_id, cid, lu) in zip(rows, written):
        if row[0].strip() != entry_id:
            raise ExportError("row order drifted: expected entry %s, found %s"
                              % (entry_id, row[0].strip()))
        if row[2].strip() != cid:
            raise ExportError("entry %s landed in contest %s, expected %s"
                              % (entry_id, row[2].strip(), cid))

        cells = row[FIRST_SLOT_COL:FIRST_SLOT_COL + SLOT_COLS]
        if any(not c.strip() for c in cells):
            raise ExportError("entry %s has an empty roster slot" % entry_id)

        ids = []
        for slot, cell in zip(SLOT_ORDER, cells):
            cell = cell.strip()
            if cell != cell.rstrip(".") or "  " in cell:
                raise ExportError(
                    "entry %s slot %s is malformed: %r -- trailing period or "
                    "double space will break the DK upload" % (entry_id, slot, cell))
            if not (cell.endswith(")") and "(" in cell):
                raise ExportError("entry %s slot %s is not Name + ID format: %r"
                                  % (entry_id, slot, cell))
            dk_id = cell[cell.rindex("(") + 1:-1]
            if not dk_id.isdigit():
                raise ExportError("entry %s slot %s has a non-numeric ID: %r"
                                  % (entry_id, slot, cell))
            ids.append(dk_id)
            if cell != lu.slots[slot].name_id:
                raise ExportError("entry %s slot %s says %r, lineup says %r"
                                  % (entry_id, slot, cell, lu.slots[slot].name_id))

        if len(set(ids)) != SLOT_COLS:
            raise ExportError("entry %s rosters a duplicate player" % entry_id)
        if frozenset(ids) != lu.ids:
            raise ExportError("entry %s did not round-trip to the same lineup"
                              % entry_id)

        if players_by_id:
            try:
                sal = sum(players_by_id[i].salary for i in ids)
            except KeyError as e:
                raise ExportError("entry %s references unknown DK ID %s"
                                  % (entry_id, e))
            if sal > 50_000:
                raise ExportError("entry %s is $%s over the cap"
                                  % (entry_id, format(sal - 50_000, ",")))


def write_review_csv(out_path, sets, own_attr="own_large"):
    """
    Part 26's human-readable format:
        Contest, Lineup, QB, RB1, RB2, WR1, WR2, WR3, TE, FLEX, DST, Salary, Score

    Separate file from the DK upload on purpose. This one is for reading before
    you commit; the DKEntries file is for the machine.
    """
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Contest", "Lineup"] + list(SLOT_ORDER)
                   + ["Salary", "Proj", "Ceiling", "pOwn", "Stack", "Bringback"])
        for label, lus in sets:
            for i, lu in enumerate(lus, 1):
                w.writerow([label, i]
                           + [lu.slots[s].name_id for s in SLOT_ORDER]
                           + [lu.salary, round(lu.proj, 1), round(lu.ceiling, 1),
                              round(lu.own(own_attr), 1), lu.stack_size(),
                              "Y" if lu.has_bringback() else "N"])
    return out_path
