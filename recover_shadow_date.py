"""
recover_shadow_date.py
Re-snapshots a past day's edges into ML RL Shadow when rows went missing.
Usage: python recover_shadow_date.py 2026-06-08
Pulls the Bet History rows for that date and reconstructs minimal Shadow rows,
then appends them (skips if that date is already present in Shadow).
"""

import sys, os, time
import gspread
from google.oauth2.service_account import Credentials

sys.stdout.reconfigure(encoding="utf-8")

ODDS_SHEET_ID = "1RaSm1ogJtNykM7WbYfQ3b9L7MUePcRBqlFMKuQfA_I4"
CREDS_FILE    = os.path.join(os.path.dirname(__file__), "google_credentials.json")
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def auth():
    creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)

def main():
    target_date = sys.argv[1] if len(sys.argv) > 1 else None
    if not target_date:
        print("Usage: python recover_shadow_date.py YYYY-MM-DD")
        return

    print(f"Checking ML RL Shadow for {target_date} ...")
    gc = auth()
    sh = gc.open_by_key(ODDS_SHEET_ID)

    ws_shadow = sh.worksheet("ML RL Shadow")
    shadow_vals = ws_shadow.get_all_values()

    # Check if date already present
    if shadow_vals:
        dates_in_shadow = [r[0] for r in shadow_vals[1:] if r]
        if target_date in dates_in_shadow:
            count = dates_in_shadow.count(target_date)
            print(f"  {target_date} already has {count} rows in Shadow — nothing to recover.")
            return
        print(f"  {target_date} NOT found in Shadow — will attempt recovery from Bet History.")
    else:
        print("  Shadow tab appears empty.")

    # Pull Bet History rows for that date
    ws_hist = sh.worksheet("Bet History")
    hist_vals = ws_hist.get_all_values()
    if not hist_vals:
        print("  Bet History is empty — cannot recover.")
        return

    header = hist_vals[0]
    try:
        c_date  = header.index("Date")
        c_game  = header.index("Game")
        c_time  = header.index("Time (ET)")
        c_away_sp = header.index("Away SP")
        c_home_sp = header.index("Home SP")
        c_type  = header.index("Bet Type")
        c_dir   = header.index("Direction")
        c_stars = header.index("Stars")
        c_units = header.index("Units Bet")
        c_book  = header.index("Book")
        c_line  = header.index("Book Line")
        c_juice = header.index("Book Juice")
        c_proj  = header.index("Our Projection")
        c_edge  = header.index("Edge (runs)")
        c_pf    = header.index("Park Factor")
        c_venue = header.index("Venue")
    except ValueError as e:
        print(f"  Missing column in Bet History: {e}")
        return

    target_rows = [r for r in hist_vals[1:] if len(r) > c_date and r[c_date] == target_date]
    if not target_rows:
        print(f"  No Bet History rows found for {target_date} — cannot recover.")
        return

    print(f"  Found {len(target_rows)} Bet History rows for {target_date}.")
    print("  Note: Shadow recovery from Bet History only captures Game Total bets.")
    print("        ML/RL shadow rows require the original analyze_edges.py run data.")
    print()

    # Shadow header (partial — we fill what we have, leave ML/RL columns blank)
    shadow_header_check = shadow_vals[0] if shadow_vals else []
    if not shadow_header_check:
        print("  Shadow tab has no header — please run analyze_edges.py first to initialize it.")
        return

    sh_header = shadow_header_check
    n_cols = len(sh_header)

    recovered = []
    for r in target_rows:
        while len(r) < len(header):
            r.append("")
        # Build a row with as many columns as the shadow header, fill what we know
        row = [""] * n_cols
        def sc(name):
            try: return sh_header.index(name)
            except: return None

        for col_name, hist_col in [
            ("Date", c_date), ("Game", c_game), ("Time (ET)", c_time),
            ("Away SP", c_away_sp), ("Home SP", c_home_sp),
            ("Bet Type", c_type), ("Direction", c_dir), ("Stars", c_stars),
            ("Units", c_units), ("Book", c_book), ("Book Line", c_line),
            ("Book Juice", c_juice), ("Our Projection", c_proj),
            ("Edge (runs)", c_edge), ("Park Factor", c_pf), ("Venue", c_venue),
        ]:
            idx = sc(col_name)
            if idx is not None and idx < n_cols:
                row[idx] = r[hist_col]

        # Mark as recovered
        run_at_idx = sc("Run at")
        if run_at_idx is not None:
            row[run_at_idx] = f"{target_date} [recovered from Bet History]"

        recovered.append(row)

    print(f"  Appending {len(recovered)} recovered rows to Shadow ...")
    ws_shadow.append_rows(recovered, value_input_option="USER_ENTERED")
    time.sleep(0.5)
    print(f"  Done. {len(recovered)} rows added for {target_date}.")
    print()
    print("  NOTE: These recovered rows will be missing ML/RL projection columns")
    print("  (away/home win%, run line probabilities, offense adj, etc.).")
    print("  They CAN still be graded for actual scores by grade_bets.py.")

if __name__ == "__main__":
    main()
