# Notes for the Showdown chat — carried over from the Main Slate build

Paste this into the Showdown conversation. It is everything learned building the
Main Slate pipeline that applies to Showdown, plus an explicit list of what does
NOT, so Main Slate rules never get applied to a single-game slate by mistake.

---

## 1. DraftKings file facts (all confirmed, all format-level)

- **The DKEntries export contains the full player pool**, not just entry rows.
  It starts at the row/column where a cell reads `Position`, and carries
  `Name + ID`, `Roster Position`, `Salary`, `Game Info`, `TeamAbbrev`. Locate it
  by searching for that header, never a fixed offset — DK moves these columns.
- **DKSalaries has a `Status` column that DKEntries lacks** (Q / OUT / IR / D).
  It is the only injury feed in the file drop. OUT and IR are hard excludes.
- **DK's two files disagree on D/ST names.** DKEntries writes `'Chargers '`
  with a trailing space; DKSalaries writes `'Chargers'`. Concatenated to
  `Name (ID)` that produces a double space. Always rebuild the upload string
  from a stripped name rather than copying DK's own `Name + ID`.
- **DK's bulk upload accepts `Name (ID)` or the bare ID, never a bare name**
  (their instruction #4), and you should **submit only the rows you are
  changing** (#5) — so cash-game rows get dropped from the upload, not blanked.
- **The weekly projections file carries Showdown-specific columns**:
  `CPT Salary`, `CPT Projection`, `CPT Own` alongside the FLEX equivalents, plus
  `Small Field` and `Large Field` ownership. Both ownership columns sum to ~900%
  across the roster, so they are true normalized pOwn, not ratings.
- The projections `id` column is *probably* the DK Player ID — 8-digit, unique,
  position-blocked exactly how DK assigns them. **Not yet confirmed.** Verify by
  joining a projections file and a salary file from the SAME slate; if it
  matches, name-matching disappears from the primary join entirely.

## 2. Process lessons that cost real time to learn

- **Your written prompt is not the authoritative rule set.** The Main Slate
  prompt was ChatGPT describing only its own half of a two-part workflow — it
  did the pool, your solver did construction — so no construction rules were in
  it. Nine rules had to be recovered conversationally. **Screenshot your
  Showdown solver's settings early** and hand them over in one pass.
- **Entry counts drive leverage, not entry fee.** Ask for them every week and
  never fall back to a previous slate's numbers. Week 1 2026 Main Slate: the $1
  field (178,359) was LARGER than the $3 (158,541), which inverts the usual
  assumption.
- **Exposure is a per-player, per-week decision.** No global cap. Set every min
  and max by hand once the pool is trimmed, weighing ownership projections,
  point projections and pool shape together.
- **Minimum exposure matters as much as maximum.** Without a floor, a strong
  play can silently reach zero. This is a known past failure ("zero-exposure
  stars") and it is mechanically preventable.
- **Verify the upload file by re-reading it from disk** and reconstructing every
  lineup, rather than trusting the code that wrote it. Wrong-contest Entry IDs,
  a set that only replicated to four of five contests, and a name DK cannot
  parse are all silent failures that surface on Sunday.

## 3. Duplication — this matters MORE in Showdown

Measured against real contest results:

| Slate | Entries | Most-duplicated lineup | Naive model predicted |
|---|---|---|---|
| Wk 11 Main | 237,600 | **443x** | 0.12 |
| Wk 12 Main | 237,604 | **419x** | 0.08 |

Multiplying player ownerships together **understates real duplication by three
to four orders of magnitude.** Duplication comes from correlated *construction*,
not independent player selection.

Showdown is worse: a six-player roster drawn from ~40 players duplicates far
more readily than a nine-player roster drawn from 300. Manage it through
Captain choice, salary and construction combinations — never by reaching for
low-owned players. Any duplication estimate must be calibrated against real
contest results, never derived.

## 4. What does NOT transfer — do not apply these to Showdown

Every one of these is a Main Slate rule that is meaningless or wrong in a
single-game format:

- **The correlation floor of 5.** In Showdown every player is in the same game,
  so the metric is degenerate. Showdown correlation is about Captain choice and
  game-script pairing instead.
- **QB stacking, bringback, and mini-correlation** as defined for Main Slate.
- **Max 5 players from one game.** All six are, by definition.
- **Max 2 players from one team without that team's QB.** Replaced by Showdown
  team-split logic (4-2, 3-3, 5-1 and so on).
- **FLEX late-swap.** One game means everything locks together; there is no
  latest-starting player to place.
- **Positional pool targets** (QB 3-5, RB 6-10, WR 12-16, TE 4-6, DST 5-7) and
  the TE-at-FLEX rule. Both are Main Slate roster constructs.
- **Part 15 salary bands and the $44,000 floor.** The Captain multiplier changes
  the salary structure entirely.
- **One RB per team** probably still holds on workload-split logic, but it is
  worth re-deciding rather than assuming.

## 5. Things worth deciding early in the Showdown chat

- Captain rules — is any position ever excluded from Captain? Minimum projection
  or ownership thresholds for the Captain slot?
- Team split constraints, and whether any split is banned outright.
- Whether kickers and D/ST are in play, and any rules pairing or opposing them.
- Portfolio shape — the note on file says 40 unique lineups across 80 entries.
- Whether the same "no lineup repeats across sets" rule applies.
