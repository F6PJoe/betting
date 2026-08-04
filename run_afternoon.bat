@echo off
cd /d "C:\Users\corpo\Betting Models"

python fetch_odds.py
python analyze_edges.py --force
