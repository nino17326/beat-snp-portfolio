@echo off
rem Starts the ticker lookup page in the background (no window) and opens
rem it in your browser. It is never started twice: if it already runs,
rem this just opens it. Stop it with "Stop lookup.bat".
rem (Every update - launch.py / run_all.py - also starts it by itself.)
cd /d "%~dp0"
py lookup_server.py start --open
timeout /t 4 >nul
