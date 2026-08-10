@echo off
cd /d "C:\Users\corpo\Betting Models"

REM Line snapshot pass: --lines-only skips player props (1 credit/game instead of 5).
REM Props only need fetching once a day, in the morning run.
python fetch_odds.py --lines-only
python analyze_edges.py --force
