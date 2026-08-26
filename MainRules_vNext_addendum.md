# MainRules_vNext.1 — ADDENDUM

Rules that are **not in MainRules_vNext.1** but are used in every build.
Numbered as continuations so they can be pasted onto the end of the master
prompt. Where an item amends an existing Part, that is stated.

Established 2026-08-26.

---

## PART 31 — CORRELATION (MANDATORY, EVERY LINEUP)

Every lineup must contain **at least 5 correlated players**.

A player is *correlated* when the lineup takes **two or more players from his
game**. A player alone in his game correlates with nobody and counts zero.

Worked example:

```
QB + his WR + an opposing WR        = 3 correlated  (one game)
a WR elsewhere + an opposing player = 2 correlated  (second game)
                                      -----------
                                        5  -> minimum satisfied
```

A double-stacked QB makes the first group 4, so that lineup carries at least 6.

**5 is a floor, not a target.** More correlation is better and should not be
argued down, but do not force construction past 5 purely to raise the number.

**D/ST is never counted toward correlation**, even alongside a running back
from its own team.

Observed in practice: single-stack sets land near 5.0–5.3, double-stack sets
near 6.0–6.2. Correlation naturally runs higher in the larger-field portfolios,
which is the correct direction.

### 31a — Correlation belongs in POOL construction, not just lineup assembly

A player's value is not his projection alone — it is his projection **plus the
quality of what he correlates with**. A QB with two genuine ceiling pass
catchers in a strong game environment is worth more than his raw number; a QB
whose only realistic stack partners are weak is worth less, and a slightly
lower-projected QB with excellent correlation pieces can and should overtake
him.

The same logic applies to receivers: a WR whose game offers no attractive
bringback is a weaker tournament play than his projection suggests.

This extends Part 6, which already lists *stack quality* and *bringback
quality* among QB evaluation criteria, to the pool-building step generally.

### 31b — Correlation quality is capped inside Part 5's budget

Correlation quality is one more context adjustment, sharing the same **~±5%**
ceiling as aFPA, DVP and the rest. It is enough to flip players already close
together — the "slightly weaker play with really good correlation" case — and
deliberately not enough to lift a player over a genuine projection gap.

Partner quality is measured on **ceiling, not projection**: correlation only
pays in the scenario where the game actually goes off.

---

## PART 31c — RUNNING BACKS AS STACK PARTNERS

**Any running back may be part of a QB stack.** Burrow with Ja'Marr Chase *and*
Chase Brown is a perfectly good double stack, and the RB counts toward both the
stack size and the correlation total.

**What is banned is the RB being the ONLY stack partner.** Burrow with Chase
Brown and no receiver, or Cam Ward with Tony Pollard, is the construction to
avoid. At least one WR/TE is required alongside.

**The rare exemption.** Once in a blue moon a back may stand alone as the whole
stack. This is decided per week, for a named player, and asked about explicitly
before the build — never inferred, and usually the answer is none.

An RB as a **bringback** against an opposing QB stack needs no exemption and is
always fine.

---

## PART 31d — NO UNCORRELATED TEAM CONCENTRATION

**At most 2 players from any one team** when neither that team's QB nor the
opposing QB is in the lineup.

LaPorta + Gibbs + ARSB with no Goff and no Saints QB is banned. Gibbs + ARSB is
fine — and in practice that pair only appears when something like Olave is also
there tying the game together.

Either quarterback lifts the cap: their own makes it a stack, the opponent's
makes them bringbacks off the other side of the same game.

D/ST does not count toward the two. It shares no scoring with the offense, and
counting it would work against the RB + own-D/ST pairing in Part 32.

---

## PART 31e — ONE RUNNING BACK PER TEAM

Never roster two running backs from the same team. They split one workload, so
they are closer to mutually exclusive than correlated.

---

## PART 31f — AT MOST 5 PLAYERS FROM ONE GAME

Five is the ceiling of a legitimate construction: a double-stacked QB with a
double bringback is QB + 2 + 2. Beyond that it is concentration rather than
correlation. Rare but entirely possible, so it is a cap and not a target.

D/ST is not counted — the Part 32 ban already keeps a defense out of any game
the lineup is stacking both sides of.

Note this cap rarely binds on its own: Part 31d already holds each side to two
players unless a QB from that game is rostered, which puts the natural ceiling
at four. It only matters once a QB in that game lifts those caps.

---

## PART 31g — SALARY FLOOR

Lineups are built between **88% and 100% of the cap — $44,000 to $50,000**.
Part 15 asks for roughly 10% of lineups at or below $49,000 but never says how
far below; this is the floor.

---

## PART 31h — EXPOSURE

**There is no global exposure ceiling**, deliberately. Every exposure is set
per player, per week, once the pool is trimmed — weighing ownership projections,
point projections and the shape of the pool together.

A blanket cap would pre-empt that decision silently, which is why the solver
runs at 100% rather than at some default.

**Minimums matter as much as maxima.** Part 10's Option C requires meaningful
minimum exposure on the top receivers so strong plays are not accidentally
eliminated, and Part 16 asks the same for top RB/WR/TE. Without a floor a
player can quietly reach zero and nobody notices.

---

## PART 32 — D/ST PAIRING

**Hard rule — never violate.** No player may appear in a lineup with the D/ST
he is playing against. If the Lions D/ST is rostered, no Saints player may be.
This applies to every position, not just the QB.

Consequence worth knowing: once a QB stack carries a bringback, the lineup
holds players on both sides of that game, so **neither** defense from it is
legal. The D/ST must come from another game.

**Small bonus, never a force.** A running back rostered alongside his own D/ST
earns a slight bump for shared game script. This must stay small enough that it
breaks ties without steering construction — a rate around 10–15% of lineups is
the pairing occurring naturally, not being manufactured.

---

## PART 33 — TE AT FLEX (amends Part 12)

Part 12 states there is "no arbitrary cap on TE FLEX." **That is superseded.**

FLEX should be a WR or RB in essentially every week. TE at FLEX is expected
roughly **1–2 weeks per season**, and only when:

- a genuine "free square" TE exists, or
- salaries are extraordinarily tight

It is a deliberate weekly decision, discussed before the build — never a default
the optimizer drifts into. The mechanism is capping TE at one per lineup, since
a TE only reaches FLEX in a two-TE roster.

Side effect to expect: capping TE at one removes about half the TE slots, so
the TE pool naturally runs 3–5 rather than the 4–6 of Part 11. This is
structural, not a construction error.

---

## PART 34 — POOL TARGETS ARE GUIDANCE (amends Parts 9/10/11/13)

The positional pool ranges are **not black-and-white**. Most weeks will land
inside them, but going slightly above or below is correct when the slate
warrants it. Treat a miss of one or two players as information, not a defect.

Part 11's TE maximum of 6 remains a hard ceiling.

---

## PART 35 — UNIQUENESS (amends Part 19)

Every lineup must differ from **every other lineup built that week** by at
least **2 players** — across contests, not merely within a set.

Differentiation should come from applying modest randomness to projections
(~5%, scaled by field size) rather than from forcing lineups apart, which
degrades quality fast. Separate solves over one pool with one objective
otherwise converge on the same answer.

---

## PART 36 — LEVERAGE FOLLOWS FIELD SIZE (amends Parts 17/18)

Ownership aggression and differentiation are driven by **actual weekly entry
counts**, not by entry fee. Entry counts are supplied each week before the
build.

Parts 17–18 assume the $3 is the largest field. Week 1 2026 disproved it:

```
$5 Milly  832,342      $3       158,541
$1        178,359      $0.25    118,906
                       $0.10      2,972
```

The $1 was larger than the $3, and the structure is not a gradient — three
large fields clustered within 1.5x of each other, then a 40x cliff to the dime.

Where two ownership columns are available, large-field projections drive every
contest except the $0.10, which uses small-field.

---

## PART 37 — THE MILLIONAIRE IS A FIFTH SET

The $5 Millionaire is built as its own independent portfolio, not as a variant
of the four. At 10 entries in an 832,000-entry field with $1M to first, only
tournament-winning equity matters: maximum ceiling, strongest correlation,
lowest duplication. Median projection is close to irrelevant.

Full portfolio is therefore **90 lineups across 170 entries** — the $0.10 set of
20 replicated across all five Dime Package contests.

---

## PART 38 — DUPLICATION CANNOT BE PREDICTED FROM OWNERSHIP

Measured against real contest results from the 2025 season:

| Week | Entries | Most-duplicated lineup | Naive model predicted |
|------|---------|------------------------|-----------------------|
| 11 | 237,600 | **443x** | 0.12 |
| 12 | 237,604 | **419x** | 0.08 |

Multiplying player ownerships together understates real duplication by three to
four orders of magnitude, because duplication comes from **correlated
construction** — a popular stack landing with a popular value play at a popular
salary point — not from independent player selection.

Therefore: manage duplication through salary, stack and ownership
*combinations* (Part 19), never by reaching for low-owned players. Any
duplication estimate must be calibrated against real contest results, never
derived from first principles.
