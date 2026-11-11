@echo off
rem Removes the "Portfolio weekly update" task from Windows Task Scheduler.
cd /d "%~dp0"
py tools\windows_setup.py task-remove
pause
