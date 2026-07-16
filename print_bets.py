"""Print today's bets sorted best to worst."""
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import os

SCOPES = ["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_file(
    os.path.join(os.path.dirname(__file__), "google_credentials.json"), scopes=SCOPES)
gc = gspread.authorize(creds)
ODDS_SHEET_ID = "1RaSm1ogJtNykM7WbYfQ3b9L7MUePcRBqlFMKuQfA_I4"
sh = gc.open_by_key(ODDS_SHEET_ID)
today = datetime.now().strftime("%Y-%m-%d")

def col(hdr, name):
    try: return hdr.index(name)
    except ValueError: return -1

def safe(row, i, default=""):
    return row[i] if i >= 0 and i < len(row) else default

# ── Bet History (Game Totals + ML/RL) ────────────────────────────────────────
bh = sh.worksheet("Bet History").get_all_values()
hdr = bh[0] if bh else []
rows = [r for r in bh[1:] if r and r[0] == today]

ci_stars   = col(hdr, "Stars")
ci_game    = col(hdr, "Game")
ci_time    = col(hdr, "Time (ET)")
ci_type    = col(hdr, "Bet Type")
ci_dir     = col(hdr, "Direction")
ci_beton   = col(hdr, "Bet On")
ci_line    = col(hdr, "Book Line")
ci_juice   = col(hdr, "Book Juice")
ci_units   = col(hdr, "Units Bet")
ci_conf    = col(hdr, "Confidence %")
ci_our_pct = col(hdr, "Our Projection")
ci_book    = col(hdr, "Book")

print("=" * 65)
print(f"  FANTASY SIX PACK — TODAY'S BETS  ({today})")
print("=" * 65)
print()
print("── GAME TOTALS / ML / RL (Official Bets) ──────────────────")
for r in rows:
    stars   = safe(r, ci_stars)
    game    = safe(r, ci_game)
    time_et = safe(r, ci_time)
    btype   = safe(r, ci_type)
    direc   = safe(r, ci_dir)
    beton   = safe(r, ci_beton)
    line    = safe(r, ci_line)
    juice   = safe(r, ci_juice)
    units   = safe(r, ci_units)
    conf    = safe(r, ci_conf)
    our_pct = safe(r, ci_our_pct)
    book    = safe(r, ci_book)

    if btype == "Game Total":
        bet_str = f"{btype} {direc}  {line}  ({juice})"
    elif btype == "Moneyline":
        bet_str = f"ML — {beton}  ({juice})"
    else:
        bet_str = f"RL — {beton}  ({juice})"

    print(f"  {stars}  {game}  [{time_et}]")
    print(f"      {bet_str}")
    print(f"      Our Win%: {our_pct}  |  Conf: {conf}  |  Units: {units}u  |  Book: {book}")
    print()

# ── Player Props (Team Totals) ────────────────────────────────────────────────
pp = sh.worksheet("Player Props").get_all_values()
ph = pp[0] if pp else []
prows = [r for r in pp[1:] if r and r[0] == today]

pc_stars = col(ph, "Stars")
pc_game  = col(ph, "Game")
pc_plyr  = col(ph, "Player")
pc_type  = col(ph, "Prop Type")
pc_dir   = col(ph, "Direction")
pc_book  = col(ph, "Best Book")
pc_line  = col(ph, "Book Line")
pc_juice = col(ph, "Book Juice")
pc_proj  = col(ph, "Our Projection")
pc_edge  = col(ph, "Edge %")
pc_units = col(ph, "Units")

if prows:
    print("── TEAM TOTALS (Player Props Tab) ──────────────────────────")
    for r in prows:
        stars = safe(r, pc_stars)
        game  = safe(r, pc_game)
        team  = safe(r, pc_plyr)
        direc = safe(r, pc_dir)
        book  = safe(r, pc_book)
        line  = safe(r, pc_line)
        juice = safe(r, pc_juice)
        proj  = safe(r, pc_proj)
        edge  = safe(r, pc_edge)
        units = safe(r, pc_units)
        print(f"  {stars}  {game}")
        print(f"      Team Total {direc}  {line}  ({juice})  — {team}")
        print(f"      Proj: {proj}  |  Edge: {edge}  |  Units: {units}u  |  Book: {book}")
        print()

print("=" * 65)
print(f"  {len(rows)} official bet(s)  |  {len(prows)} team total(s)")
print("=" * 65)
