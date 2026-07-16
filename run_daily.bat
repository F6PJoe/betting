@echo off
echo ============================================================
echo  Fantasy Six Pack — Daily MLB Model Run
echo ============================================================
echo.

cd /d "%~dp0"

echo [1/3] Fetching odds...
python fetch_odds.py
if errorlevel 1 ( echo ERROR in fetch_odds.py & pause & exit /b 1 )

echo.
echo [2/3] Analyzing edges...
python analyze_edges.py
if errorlevel 1 ( echo ERROR in analyze_edges.py & pause & exit /b 1 )

echo.
echo [3/3] Grading yesterday's bets...
python grade_bets.py
if errorlevel 1 ( echo ERROR in grade_bets.py & pause & exit /b 1 )

echo.
echo ============================================================
echo  All done! Check your Google Sheet.
echo ============================================================
pause
