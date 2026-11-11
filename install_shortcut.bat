@echo off
rem Creates a "Portfolio" shortcut on your Desktop. It runs launch.py with
rem pythonw (no black window), with the chart icon from assets\, and this
rem folder as its working directory. Paths are worked out from where this
rem folder is, so it works on any PC; run it again if you move the folder.
cd /d "%~dp0"
py tools\windows_setup.py shortcut
pause
