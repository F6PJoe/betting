# Data / file-handling notes — carried over from the Main Slate build

Back-end only. Nothing here concerns lineup construction; Showdown has its own
rules for that. These are file formats, parsing hazards and upload mechanics
that were confirmed against real DraftKings files.

## The DKEntries export contains the full player pool

Not just entry rows. A second table starts at the row/column where a cell reads
`Position`, carrying `Name + ID`, `Name`, `ID`, `Roster Position`, `Salary`,
`Game Info`, `TeamAbbrev`, `AvgPointsPerGame`. Its ID set was verified identical
to DKSalaries.

**Find that block by searching for the `Position` header, never by a fixed row
or column offset** — DK has moved these columns before.

`Game Info` is the only source of kickoff times anywhere in the file drop.

## DKSalaries has one column DKEntries does not: `Status`

Values are `Q`, `OUT`, `IR`, `D`. It is the only injury feed in the files.
Treat OUT and IR as hard excludes and surface Q/D for a human call. On the Week
1 main slate this was 101 Q, 9 OUT, 24 IR, 1 D out of 719 players — not a
rounding error.

## DK's two files disagree with each other on D/ST names

```
DKSalaries      Name='Chargers'    Name+ID='Chargers (43728525)'
DKEntries pool  Name='Chargers '   Name+ID='Chargers  (43728525)'
```

Every D/ST in the entries export carries a trailing space; none in the salary
export do. **Always rebuild the upload string as `name.strip() + " (" + id + ")"`
rather than copying DK's own `Name + ID` field.** Copying it verbatim produces a
double space. The weekly projections file has the same trailing-space problem on
D/ST names independently.

## DraftKings' own upload rules, from the Instructions column

- Use `Name + ID` **or** the bare ID. A bare player name is rejected.
- **Include only the entries you are actually changing.** So cash-game rows
  should be dropped from the upload file, not left blank.
- Preserve the header row byte-for-byte from the export and pad data rows to its
  width, so the file you upload is structurally identical to the one DK issued.

## The weekly projections file

Columns confirmed: `Player`, `DK Pos`, `Team`, `Opp`, `DK Salary`, `DK Proj`,
`DK Value`, `Small Field`, `Large Field`, `DK Floor`, `DK Ceiling`, `id`. The
Showdown version adds `CPT Salary`, `CPT Projection`, `CPT Own` alongside the
FLEX equivalents — so the Captain slot has real projections and real ownership
rather than needing a 1.5x approximation.

**Both ownership columns are true normalized pOwn**, not ratings — they sum to
~900% across a nine-man roster. Check that sum on arrival; if it comes back far
off, the column is not what it appears to be.

**Validate the column names before reading a single row.** Every field access is
a lookup that quietly returns 0.0 on a rename, and a slate of zero projections
looks exactly like a file that has not posted yet.

## The `id` column is probably the DK Player ID — but unverified

It is 8-digit, unique, and blocked by position exactly the way DK assigns IDs
within a draft group. It has never been checked against a salary file from the
**same** slate, because the files on hand were from different weeks.

Verify it the first chance you get. If it holds, name-matching disappears from
the primary join entirely.

## Name and team normalization hazards, all confirmed real

Sources spell the same player differently:

```
Travis Etienne      vs  Travis Etienne Jr.
Amon-Ra St Brown    vs  Amon-Ra St. Brown
AJ Brown            vs  A.J. Brown
Kenneth Walker      vs  Kenneth Walker III
Michael Pittman     vs  Michael Pittman Jr.
Hollywood Brown     vs  Marquise Brown        <- normalization cannot fix this
```

Team abbreviations collide too: the projections file writes the Rams as `LA`
where DK writes `LAR`. Also watch `JAC`/`JAX`, `WSH`/`WAS`, `ARZ`/`ARI`.

A normalizer must strip accents, punctuation, whitespace and generational
suffixes — and still needs a hand-maintained alias table for cases like
Hollywood/Marquise Brown where the strings genuinely differ.

There is already a working one in the Betting Models repo:
`nfl_props_data.py` → `normalize_name()` and `NAME_ALIASES`. Reuse it rather
than writing a second.

## DK lists every backup QB; the projections file lists only starters

DK had 91 QBs across 24 teams on the Week 1 main slate. The projection source
had exactly one per team — it has already resolved starters. The intersection of
the two is therefore the eligible-QB list, and any disagreement between them
should be flagged rather than resolved automatically.

## Verify the finished upload file by re-reading it from disk

Do not trust the code that wrote it. Re-read the file, reconstruct every lineup
from it, and compare against what was intended. Wrong-contest Entry IDs, a set
that only replicated to some of its contests, an over-cap roster, or a name DK
cannot parse are all **silent** failures — nothing raises, and they surface on
Sunday when the entries are locked.

## Ask for contest entry counts each week

They are not in any file. Never carry over a previous slate's numbers.
