"""Run this once after installing Python to install all required packages."""
import subprocess, sys

packages = [
    "gspread", "google-auth", "requests", "scipy",
    "pandas", "nfl_data_py", "appdirs", "fastparquet",  # NFL model (nflverse data)
    "pdfplumber",  # NFL props model (parses the weekly WR-CB matchup PDF)
]
for pkg in packages:
    print(f"Installing {pkg} ...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])

print("\nAll packages installed successfully!")
