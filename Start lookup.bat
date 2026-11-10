@echo off
rem Starts the ticker lookup page (lookup.py, in this folder) on http://localhost:8501
rem Keep this window open while you use the lookup; close it to stop.
rem Which data it uses (yours via .env, or the demo) is decided by settings.py.
cd /d "%~dp0"
title Ticker lookup - keep this window open
echo.
echo  Ticker lookup is starting on http://localhost:8501
echo  Use the search box on the dashboard, or open:
echo      http://localhost:8501/?ticker=SAF.PA
echo  Keep this window open while you use it. Close it to stop.
echo.
py -m streamlit run lookup.py --server.port 8501 --server.headless true --browser.gatherUsageStats false
echo.
echo  The lookup has stopped. If you see an error above, it says what went wrong.
pause
