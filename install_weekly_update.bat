@echo off
rem Adds a Windows Task Scheduler task "Portfolio weekly update": every
rem Monday at 07:30 it updates everything and shows a notification. If the
rem PC was off at 07:30, it runs as soon as possible afterwards.
rem Remove it again with remove_weekly_update.bat.
cd /d "%~dp0"
py tools\windows_setup.py task-install
pause
