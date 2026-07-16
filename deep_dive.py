import json
from collections import defaultdict
import statistics

with open("bh_dump.json", encoding="utf-8") as f:
    bh = json.load(f)

with open("shadow_dump.json", encoding="utf-8") as f:
    sh = json.load(f)

header = bh["header"]
rows = bh["rows"]

def col(row, name, h=None):
    h = h or header
    try:
        return row[h.index(name)]
    except (ValueError, IndexError):
        return ""

def parse_stars(s):
    s = s.strip()
    try:
        return int(s)
    except:
        return s.count("*")

graded = [r for r in rows if col(r, "Result") in ("Win", "Loss", "Push")]

by_stars = defaultdict(lambda: {"W":0,"L":0,"P":0,"units":0.0,"overs":0,"unders":0})
by_direction = defaultdict(lambda: {"W":0,"L":0,"units":0.0})
by_date = defaultdict(lambda: {"W":0,"L":0,"units":0.0})
proj_errors = []
big_misses = []

for r in graded:
    stars = parse_stars(col(r, "Stars"))
    result = col(r, "Result")
    try:
        units = float(col(r, "Units Result").replace("+",""))
    except:
        units = 0.0
    direction = col(r, "Direction")
    date = col(r, "Date")
    game = col(r, "Game")
    book_line = col(r, "Book Line")

    try:
        proj = float(col(r, "Our Projection"))
        actual = float(col(r, "Actual Total"))
        err = actual - proj
        proj_errors.append({
            "proj": proj, "actual": actual, "direction": direction,
            "edge": col(r,"Edge (runs)"), "stars": stars, "date": date,
            "game": game, "err": err, "line": book_line
        })
        if abs(err) > 4:
            big_misses.append((date, game, direction, proj, actual, err, stars))
    except:
        pass

    by_stars[stars]["units"] += units
    by_date[date]["units"] += units

    if result == "Win":
        by_stars[stars]["W"] += 1
        by_direction[direction]["W"] += 1
        by_date[date]["W"] += 1
    elif result == "Loss":
        by_stars[stars]["L"] += 1
        by_direction[direction]["L"] += 1
        by_date[date]["L"] += 1
    elif result == "Push":
        by_stars[stars]["P"] += 1

    if direction == "Over":
        by_stars[stars]["overs"] += 1
    else:
        by_stars[stars]["unders"] += 1

out = []
def p(s=""):
    out.append(s)

p("=== BET HISTORY DEEP DIVE ===")
p(f"Total graded bets: {len(graded)}")
total_w = sum(v['W'] for v in by_stars.values())
total_l = sum(v['L'] for v in by_stars.values())
total_p = sum(v['P'] for v in by_stars.values())
total_u = sum(v['units'] for v in by_stars.values())
p(f"Overall: {total_w}-{total_l}-{total_p}  {total_u:+.2f}u")
p()

p("--- By Star Level ---")
for stars in sorted(by_stars.keys()):
    d = by_stars[stars]
    total = d['W'] + d['L']
    pct = d['W']/total*100 if total else 0
    p(f"  {stars}-star: {d['W']}-{d['L']}  {pct:.0f}%  {d['units']:+.2f}u  [O:{d['overs']} U:{d['unders']}]")
p()

p("--- By Direction ---")
for direction in ["Over", "Under"]:
    d = by_direction[direction]
    total = d['W'] + d['L']
    pct = d['W']/total*100 if total else 0
    p(f"  {direction}: {d['W']}-{d['L']}  {pct:.0f}%")
p()

p("--- By Date ---")
for date in sorted(by_date.keys()):
    d = by_date[date]
    p(f"  {date}: {d['W']}-{d['L']}  {d['units']:+.2f}u")
p()

p("--- Projection Error Analysis ---")
if proj_errors:
    errs = [x["err"] for x in proj_errors]
    p(f"  Mean error (actual - proj): {statistics.mean(errs):+.2f} runs")
    p(f"  Median error: {statistics.median(errs):+.2f} runs")
    p(f"  Std dev: {statistics.stdev(errs):.2f} runs")
    over_bets = [x for x in proj_errors if x["direction"]=="Over"]
    under_bets = [x for x in proj_errors if x["direction"]=="Under"]
    if over_bets:
        over_errs = [x["err"] for x in over_bets]
        p(f"  Over bets mean err: {statistics.mean(over_errs):+.2f} (+ = game scored more than projected)")
    if under_bets:
        under_errs = [x["err"] for x in under_bets]
        p(f"  Under bets mean err: {statistics.mean(under_errs):+.2f} (- = game scored less than projected)")
    p()
    p("  All games edge vs result:")
    for x in sorted(proj_errors, key=lambda x: float(x["edge"]) if x["edge"] else 0, reverse=True):
        try:
            line = float(x["line"])
            won = (x["direction"]=="Over" and x["actual"] > line) or (x["direction"]=="Under" and x["actual"] < line)
            outcome = "WIN" if won else "LOSS"
        except:
            outcome = "?"
        p(f"    {x['date']} {x['game'][:30]} {x['direction']} {x['line']} | proj:{x['proj']:.1f} act:{x['actual']:.1f} err:{x['err']:+.1f} edge:{x['edge']} {outcome}")
p()

p("--- Big Misses (|actual-proj| > 4 runs) ---")
for x in sorted(big_misses, key=lambda x: abs(x[5]), reverse=True):
    date, game, direction, proj, actual, err, stars = x
    p(f"  {date} {game} {direction} | proj:{proj:.1f} act:{actual:.1f} err:{err:+.1f} | {stars}*")
p()

# ── SHADOW ──────────────────────────────────────────────────────────────────
sh_header = sh["header"]
sh_rows = sh["rows"]

def sc(row, name):
    try:
        return row[sh_header.index(name)]
    except (ValueError, IndexError):
        return ""

graded_sh = [r for r in sh_rows if sc(r,"Result") in ("Win","Loss","Push")]
p("=== ML RL SHADOW DEEP DIVE ===")
p(f"Total shadow rows: {len(sh_rows)}")
p(f"Graded shadow rows: {len(graded_sh)}")
p()

sh_by_type = defaultdict(lambda: {"W":0,"L":0,"units":0.0})
sh_by_stars = defaultdict(lambda: {"W":0,"L":0,"units":0.0})
sh_by_date = defaultdict(lambda: {"W":0,"L":0,"units":0.0})
fav_dog = {"Fav":{"W":0,"L":0,"units":0.0},"Dog":{"W":0,"L":0,"units":0.0}}

for r in graded_sh:
    result = sc(r,"Result")
    try:
        units = float(sc(r,"Units Result").replace("+",""))
    except:
        units = 0.0
    bet_type = sc(r,"Bet Type")
    date = sc(r,"Date")
    stars = parse_stars(sc(r,"Stars"))
    try:
        juice = int(sc(r,"Book Juice").replace("+",""))
    except:
        juice = -110
    is_dog = juice > 0
    cat = "Dog" if is_dog else "Fav"

    sh_by_type[bet_type]["units"] += units
    sh_by_stars[stars]["units"] += units
    sh_by_date[date]["units"] += units
    fav_dog[cat]["units"] += units

    if result == "Win":
        sh_by_type[bet_type]["W"] += 1
        sh_by_stars[stars]["W"] += 1
        sh_by_date[date]["W"] += 1
        fav_dog[cat]["W"] += 1
    elif result == "Loss":
        sh_by_type[bet_type]["L"] += 1
        sh_by_stars[stars]["L"] += 1
        sh_by_date[date]["L"] += 1
        fav_dog[cat]["L"] += 1

p("--- Shadow by Bet Type ---")
for bt in ["Moneyline", "Run Line"]:
    d = sh_by_type[bt]
    total = d['W']+d['L']
    pct = d['W']/total*100 if total else 0
    p(f"  {bt}: {d['W']}-{d['L']}  {pct:.0f}%  {d['units']:+.2f}u")
p()

p("--- Shadow by Stars ---")
for stars in sorted(sh_by_stars.keys()):
    d = sh_by_stars[stars]
    total = d['W']+d['L']
    pct = d['W']/total*100 if total else 0
    p(f"  {stars}-star: {d['W']}-{d['L']}  {pct:.0f}%  {d['units']:+.2f}u")
p()

p("--- Shadow by Date ---")
for date in sorted(sh_by_date.keys()):
    d = sh_by_date[date]
    p(f"  {date}: {d['W']}-{d['L']}  {d['units']:+.2f}u")
p()

p("--- Shadow Favorite vs Underdog ---")
for cat in ["Fav", "Dog"]:
    d = fav_dog[cat]
    total = d['W']+d['L']
    pct = d['W']/total*100 if total else 0
    p(f"  {cat}: {d['W']}-{d['L']}  {pct:.0f}%  {d['units']:+.2f}u")
p()

p("--- All Graded Shadow Rows ---")
for r in graded_sh:
    p(f"  {sc(r,'Date')} | {sc(r,'Game')} | {sc(r,'Bet Type')} | {sc(r,'Side')} | {sc(r,'Stars')}* | Juice:{sc(r,'Book Juice')} | {sc(r,'Result')} | {sc(r,'Units Result')}")

with open("analysis_out.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))

print("Done")
