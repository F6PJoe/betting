import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import gspread
from google.oauth2.service_account import Credentials

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
creds = Credentials.from_service_account_file('google_credentials.json', scopes=SCOPES)
gc = gspread.authorize(creds)

sh = gc.open_by_key('1_PQ19dvvD51uYCZcNw6EzV37RkXdDTZpemE9xbDwgTw')
ws = sh.worksheet('Sheet1')
rows = ws.get_all_values()
print(f"All rows ({len(rows)} total):")
for i, row in enumerate(rows):
    print(f"  {i+1}: {row}")
