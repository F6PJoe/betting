"""
GT projection audit — the starting point for rebuilding the Game Total projection.

WHY THIS EXISTS (2026-08-07):
The GT edge formula was rewritten TT-style (shrink -> negative binomial -> vs book
implied) and REJECTED on out-of-sample evidence: fit on the first 70% of games and
applied to the unseen last 30%, it lost money on the training period at every
threshold and only "won" on a test period that was already profitable unfiltered.

Root cause is upstream of the edge math -- the projection itself is weaker than the
line it bets into:
    corr(our projection, actual) = 0.187
    corr(book line,      actual) = 0.274
    our projection's contribution beyond the line = +0.0027 R^2

So the fix is the projection, not the formula. The book beats us using the same
public inputs we already have. This script finds WHICH input is wrong.

Run it, then read the four sections in order. Section 4 is the payoff: it refits
the projection's own weights against actual outcomes, so a component whose fitted
weight disagrees sharply with the constant in analyze_edges.py is the culprit.

    python gt_projection_audit.py

Reads the 'Game Totals' shadow tab (every evaluated game, all star levels) rather
than Bet History, to avoid selection bias.

NOTE: reads with UNFORMATTED_VALUE for numerics. Sheets returns DISPLAY strings by
default, and several columns are percent-formatted -- a naive read turns 14.25 into
1425. Dates are read from the display grid because unformatted returns serials.
"""
import os, statistics
os.chdir(os.path.dirname(os.path.abspath(__file__)))
import gspread
from google.oauth2.service_account import Credentials

ODDS_SHEET_ID = '1RaSm1ogJtNykM7WbYfQ3b9L7MUePcRBqlFMKuQfA_I4'
SCOPES = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = Credentials.from_service_account_file('google_credentials.json', scopes=SCOPES)
gc = gspread.authorize(creds)
ws = gc.open_by_key(ODDS_SHEET_ID).worksheet("Game Totals")

disp = ws.get_all_values()
unf  = ws.get_all_values(value_render_option='UNFORMATTED_VALUE')
ci   = {h: i for i, h in enumerate(disp[0])}

def U(r, k):
    i = ci.get(k)
    if i is None or len(r) <= i: return None
    v = r[i]
    if isinstance(v, (int, float)): return float(v)
    try: return float(str(v).replace("%", "").replace("+", "").strip())
    except: return None
def D(r, k):
    i = ci.get(k)
    return str(r[i]).strip() if i is not None and len(r) > i else ""

games = []
for d_row, u_row in zip(disp[1:], unf[1:]):
    if not d_row or not d_row[0].strip(): continue
    rec = {
        "date": D(d_row, "Date") or d_row[0].strip(),
        "venue": D(d_row, "Venue"), "game": D(d_row, "Game"),
        "actual": U(u_row, "Actual Total"), "line": U(u_row, "Book Line"),
        "proj":   U(u_row, "Our Projection"),
        "pa": U(u_row, "Proj Away Runs"), "ph": U(u_row, "Proj Home Runs"),
        "aera": U(u_row, "Away ERA Est"), "hera": U(u_row, "Home ERA Est"),
        "axfip": U(u_row, "Away xFIP"),   "hxfip": U(u_row, "Home xFIP"),
        "aoff": U(u_row, "Away Offense Adj"), "hoff": U(u_row, "Home Offense Adj"),
        "abp": U(u_row, "Away Bullpen ERA"), "hbp": U(u_row, "Home Bullpen ERA"),
        "pf": U(u_row, "Park Factor"), "ump": U(u_row, "Ump Factor"),
        "wind": U(u_row, "Wind MPH"), "temp": U(u_row, "Temp (F)"),
        "wadj": U(u_row, "Weather Adj"),
    }
    if rec["actual"] is None or rec["proj"] is None or rec["line"] is None: continue
    if not (0 < rec["actual"] < 40 and 3 < rec["line"] < 20 and 3 < rec["proj"] < 25): continue
    rec["err"]      = rec["proj"] - rec["actual"]     # + = we over-projected
    rec["line_err"] = rec["line"] - rec["actual"]
    games.append(rec)

print("=" * 78)
print(f"GT PROJECTION AUDIT — {len(games)} graded games")
print("=" * 78)

def corr(pairs):
    xs = [a for a, b in pairs]; ys = [b for a, b in pairs]
    if len(xs) < 3: return None
    sx, sy = statistics.stdev(xs), statistics.stdev(ys)
    if sx == 0 or sy == 0: return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    return sum((a-mx)*(b-my) for a, b in pairs)/len(pairs)/(sx*sy)

def bucket(name, keyfn, edges):
    print(f"\n  --- {name} ---")
    print(f"     {'bucket':>18} {'n':>5} {'our err':>9} {'book err':>9} {'|ours|':>8} {'|book|':>8}")
    for lo, hi in edges:
        sub = [g for g in games if keyfn(g) is not None and lo <= keyfn(g) < hi]
        if len(sub) < 8: continue
        oe = statistics.mean(g["err"] for g in sub)
        be = statistics.mean(g["line_err"] for g in sub)
        oa = statistics.mean(abs(g["err"]) for g in sub)
        ba = statistics.mean(abs(g["line_err"]) for g in sub)
        flag = "  <<<" if abs(oe) > abs(be) + 0.4 else ""
        print(f"     {lo:>8.1f}-{hi:<8.1f} {len(sub):5d} {oe:+9.2f} {be:+9.2f} {oa:8.2f} {ba:8.2f}{flag}")

# ── 1. overall bias ───────────────────────────────────────────────────────
print("\n" + "=" * 78)
print("1. OVERALL — are we biased, or just noisy?")
print("=" * 78)
oe = [g["err"] for g in games]; be = [g["line_err"] for g in games]
print(f"   our mean error : {statistics.mean(oe):+.3f} runs   (mean |error| {statistics.mean(abs(x) for x in oe):.2f})")
print(f"   book mean error: {statistics.mean(be):+.3f} runs   (mean |error| {statistics.mean(abs(x) for x in be):.2f})")
print(f"   our error sd   : {statistics.stdev(oe):.2f}    book error sd: {statistics.stdev(be):.2f}")
print("\n   A large mean = systematic bias (fixable by a constant).")
print("   A large sd with ~0 mean = the inputs are not discriminating.")

# ── 2. where does our error concentrate? ─────────────────────────────────
print("\n" + "=" * 78)
print("2. WHERE IS OUR ERROR WORSE THAN THE BOOK'S?   ('<<<' = we are materially worse)")
print("=" * 78)
bucket("by book line (game environment)", lambda g: g["line"],
       [(6,7.5),(7.5,8.5),(8.5,9.5),(9.5,10.5),(10.5,14)])
bucket("by park factor", lambda g: g["pf"],
       [(80,95),(95,100),(100,105),(105,112),(112,140)])
bucket("by combined SP quality (avg ERA est)",
       lambda g: (g["aera"]+g["hera"])/2 if g["aera"] and g["hera"] else None,
       [(0,3.2),(3.2,3.8),(3.8,4.3),(4.3,5.0),(5.0,9)])
bucket("by temperature", lambda g: g["temp"], [(30,60),(60,70),(70,80),(80,88),(88,120)])
bucket("by wind mph", lambda g: g["wind"], [(0,5),(5,10),(10,15),(15,40)])

print("\n  --- worst venues (min 8 games, by our mean signed error) ---")
by_v = {}
for g in games:
    if g["venue"]: by_v.setdefault(g["venue"], []).append(g)
ranked = sorted(((v, s) for v, s in by_v.items() if len(s) >= 8),
                key=lambda kv: -abs(statistics.mean(x["err"] for x in kv[1])))
print(f"     {'venue':>26} {'n':>4} {'our err':>9} {'book err':>9}")
for v, s in ranked[:10]:
    print(f"     {v[:26]:>26} {len(s):4d} {statistics.mean(x['err'] for x in s):+9.2f} "
          f"{statistics.mean(x['line_err'] for x in s):+9.2f}")

# ── 3. which input drives the error? ─────────────────────────────────────
print("\n" + "=" * 78)
print("3. WHICH INPUT CORRELATES WITH OUR ERROR?")
print("=" * 78)
print("   A non-zero correlation means that input is MIS-WEIGHTED: our error moves")
print("   with it, so the model is not extracting it correctly.\n")
print(f"     {'input':>26} {'corr with our error':>22} {'n':>6}")
for label, fn in [
    ("park factor",            lambda g: g["pf"]),
    ("umpire factor",          lambda g: g["ump"]),
    ("weather adj",            lambda g: g["wadj"]),
    ("temperature",            lambda g: g["temp"]),
    ("wind mph",               lambda g: g["wind"]),
    ("avg SP ERA est",         lambda g: (g["aera"]+g["hera"])/2 if g["aera"] and g["hera"] else None),
    ("avg SP xFIP",            lambda g: (g["axfip"]+g["hxfip"])/2 if g["axfip"] and g["hxfip"] else None),
    ("avg bullpen ERA",        lambda g: (g["abp"]+g["hbp"])/2 if g["abp"] and g["hbp"] else None),
    ("combined offense adj",   lambda g: (g["aoff"]+g["hoff"])/2 if g["aoff"] is not None and g["hoff"] is not None else None),
    ("book line",              lambda g: g["line"]),
    ("our projection",         lambda g: g["proj"]),
]:
    pairs = [(fn(g), g["err"]) for g in games if fn(g) is not None]
    c = corr(pairs)
    if c is None: continue
    flag = "  <<< MIS-WEIGHTED" if abs(c) > 0.15 else ""
    print(f"     {label:>26} {c:+22.3f} {len(pairs):6d}{flag}")

# ── 4. refit the weights ─────────────────────────────────────────────────
print("\n" + "=" * 78)
print("4. REFIT — what weights SHOULD the projection use?")
print("=" * 78)
print("   Regresses actual total on the raw components via normal equations.")
print("   Compare each fitted weight to the constant in analyze_edges.py; a large")
print("   disagreement is the component to fix first.\n")

feats = [
    ("avg SP ERA",   lambda g: (g["aera"]+g["hera"])/2 if g["aera"] and g["hera"] else None),
    ("avg bullpen",  lambda g: (g["abp"]+g["hbp"])/2 if g["abp"] and g["hbp"] else None),
    ("offense adj",  lambda g: (g["aoff"]+g["hoff"])/2 if g["aoff"] is not None and g["hoff"] is not None else None),
    ("park factor",  lambda g: g["pf"]),
    ("ump factor",   lambda g: g["ump"]),
]
rows = [g for g in games if all(f(g) is not None for _, f in feats)]
print(f"   complete-case rows: {len(rows)}")
if len(rows) < 40:
    print("   Not enough complete rows to refit -- check which columns are empty.")
else:
    X = [[1.0] + [f(g) for _, f in feats] for g in rows]
    y = [g["actual"] for g in rows]
    p = len(feats) + 1
    XtX = [[sum(X[i][a]*X[i][b] for i in range(len(X))) for b in range(p)] for a in range(p)]
    Xty = [sum(X[i][a]*y[i] for i in range(len(X))) for a in range(p)]
    # gaussian elimination with partial pivoting
    M = [XtX[r][:] + [Xty[r]] for r in range(p)]
    for c in range(p):
        piv = max(range(c, p), key=lambda r: abs(M[r][c]))
        if abs(M[piv][c]) < 1e-12: continue
        M[c], M[piv] = M[piv], M[c]
        for r in range(p):
            if r == c: continue
            fct = M[r][c]/M[c][c]
            for k in range(c, p+1): M[r][k] -= fct*M[c][k]
    beta = [M[i][p]/M[i][i] if abs(M[i][i]) > 1e-12 else 0.0 for i in range(p)]
    print(f"\n     {'component':>18} {'fitted weight':>16}   meaning")
    print(f"     {'(intercept)':>18} {beta[0]:16.4f}")
    names = [n for n, _ in feats]
    hints = {
        "avg SP ERA":  "runs added per 1.00 of starter ERA",
        "avg bullpen": "runs added per 1.00 of bullpen ERA",
        "offense adj": "runs added per 1.0 of offense adj (OFFENSE_WEIGHT=0.08)",
        "park factor": "runs added per 1 point of park factor (100=neutral)",
        "ump factor":  "runs added per 1.0 of ump factor",
    }
    for i, n in enumerate(names, start=1):
        print(f"     {n:>18} {beta[i]:16.4f}   {hints.get(n,'')}")
    pred = [sum(b*x for b, x in zip(beta, X[i])) for i in range(len(X))]
    ma = statistics.mean(y)
    sse = sum((y[i]-pred[i])**2 for i in range(len(y)))
    sst = sum((v-ma)**2 for v in y)
    print(f"\n     refit R^2                : {1-sse/sst:.4f}")
    cp = corr([(rows[i]['proj'], rows[i]['actual']) for i in range(len(rows))])
    cl = corr([(rows[i]['line'], rows[i]['actual']) for i in range(len(rows))])
    print(f"     current projection R^2   : {(cp or 0)**2:.4f}")
    print(f"     book line R^2            : {(cl or 0)**2:.4f}   <-- the bar to clear")
    print("\n   If refit R^2 does not clear the book line's R^2, the current INPUTS")
    print("   cannot beat the market no matter how they are weighted, and the next")
    print("   step is new inputs (lineups, catcher framing, bullpen usage/rest,")
    print("   batted-ball profile) rather than re-tuning these.")
