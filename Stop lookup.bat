@echo off
rem Stops the ticker lookup page that runs in the background.
cd /d "%~dp0"
py lookup_server.py stop
timeout /t 3 >nul
