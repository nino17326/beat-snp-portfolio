# ============================================================
#  tools/windows_setup.py  -  used by the .bat files in the main folder
#
#      shortcut [--to FOLDER]   "Portfolio" shortcut (Desktop by default)
#      task-install             Monday 07:30 update in Task Scheduler
#      task-remove              remove that task again
#      task-check               build the task without installing it (a test)
#
#  Every path is worked out from where this repository is, so it works on
#  any PC and in any folder (also with spaces or letters like š in it).
# ============================================================

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LAUNCH = REPO / "launch.py"
ICON = REPO / "assets" / "portfolio.ico"
TASK_NAME = "Portfolio weekly update"


def pythonw():
    """pythonw.exe of THIS Python: runs scripts without a black window."""
    w = Path(sys.executable).with_name("pythonw.exe")
    if not w.exists():
        raise SystemExit(f"pythonw.exe not found next to {sys.executable}")
    return w


def q(text):
    """A PowerShell single-quoted string."""
    return "'" + str(text).replace("'", "''") + "'"


def powershell(script):
    done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-Command", script], capture_output=True, text=True)
    if done.returncode != 0:
        raise SystemExit("PowerShell said:\n" + (done.stderr or done.stdout).strip())
    return done.stdout.strip()


def shortcut(folder=None):
    folder = q(folder) if folder else "[Environment]::GetFolderPath('Desktop')"
    out = powershell(
        f"$path = Join-Path {folder} 'Portfolio.lnk';"
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($path);"
        f"$s.TargetPath = {q(pythonw())};"
        f"$s.Arguments = {q(chr(34) + str(LAUNCH) + chr(34))};"
        f"$s.WorkingDirectory = {q(REPO)};"
        f"$s.IconLocation = {q(str(ICON) + ',0')};"
        "$s.Description = 'Update (if needed) and open my portfolio dashboard. Hold Shift to force an update.';"
        "$s.Save(); $path")
    print(f"Created: {out}")
    print("Double-click it to open the dashboard (hold Shift while starting it to force an update).")


def task_script(install=True):
    build = (
        f"$action = New-ScheduledTaskAction -Execute {q(pythonw())} "
        f"-Argument {q(chr(34) + str(LAUNCH) + chr(34) + ' --scheduled')} -WorkingDirectory {q(REPO)};"
        "$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At '07:30';"
        # StartWhenAvailable = catch up as soon as possible if the PC was off at 07:30.
        "$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew;")
    if not install:
        return build + "'action: ' + $action.Execute + ' ' + $action.Arguments; 'start in: ' + $action.WorkingDirectory;" \
                       "'trigger: weekly ' + $trigger.DaysOfWeek + ' at ' + $trigger.StartBoundary;" \
                       "'catch up if missed: ' + $settings.StartWhenAvailable"
    return build + (f"Register-ScheduledTask -TaskName {q(TASK_NAME)} -Action $action -Trigger $trigger "
                    "-Settings $settings -Description 'Updates the portfolio dashboard (beat-snp-portfolio).' "
                    "-Force | Out-Null; 'ok'")


def task_install():
    powershell(task_script(install=True))
    print(f"Installed the task '{TASK_NAME}': every Monday at 07:30 (or as soon as the PC is on, if it was off).")
    print("It runs launch.py --scheduled: update, then a Windows notification. Remove it with remove_weekly_update.bat.")


def task_remove():
    out = powershell(f"if (Get-ScheduledTask -TaskName {q(TASK_NAME)} -ErrorAction SilentlyContinue) "
                     f"{{ Unregister-ScheduledTask -TaskName {q(TASK_NAME)} -Confirm:$false; 'removed' }} "
                     "else { 'none' }")
    print(f"Removed the task '{TASK_NAME}'." if out == "removed" else f"There was no task '{TASK_NAME}'.")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "shortcut":
        shortcut(sys.argv[sys.argv.index("--to") + 1] if "--to" in sys.argv else None)
    elif cmd == "task-install":
        task_install()
    elif cmd == "task-remove":
        task_remove()
    elif cmd == "task-check":
        print(powershell(task_script(install=False)))
    else:
        print(__doc__ or "use: shortcut | task-install | task-remove | task-check")
