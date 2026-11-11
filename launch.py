# ============================================================
#  launch.py  -  the one-click launcher (the "Portfolio" desktop icon)
#
#  - Today's data already there: opens the dashboard straight away.
#  - Otherwise: a small window "Updating your portfolio..." with the
#    current step, runs everything (run_all.py), then opens the dashboard.
#  - Hold Shift while starting it to force an update.
#  - If an update is already running (double-click, or the Monday task),
#    it doesn't start a second one: it shows "Already updating" and waits.
#  - The lookup page is (re)started in the background every time.
#  - If an update fails (or this launcher does), check_setup.py runs by
#    itself and a small window says what to fix.
#
#  Run it with pythonw (no black window): install_shortcut.bat does that.
#  Options: --update (force an update), --scheduled (the Monday task: update,
#  no window, a Windows notification at the end, don't open the dashboard).
# ============================================================

import ctypes
import os
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from datetime import date, datetime
from pathlib import Path

CODE = Path(__file__).resolve().parent


def setup_check_window(only_if_problems=True):
    """Run check_setup.py and show what's wrong in a small window (also
    saved as setup_check.txt in the output folder). Returns True if it
    found something to fix."""
    import check_setup
    results = check_setup.run_checks()
    problems = [r for r in results if r["status"] != check_setup.OK]
    text = check_setup.report(results)
    try:
        import settings as s
        s.OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
        s.write_text_safely(s.OUTPUT_FOLDER / "setup_check.txt", text)
    except (SystemExit, OSError):
        pass                                               # the data folder itself is the problem
    if only_if_problems and not problems:
        return False
    import tkinter as tk
    root = tk.Tk()
    root.title("Portfolio - setup check")
    try:
        root.iconbitmap(str(CODE / "assets" / "portfolio.ico"))
    except tk.TclError:
        pass
    tk.Label(root, font=("Segoe UI", 11, "bold"),
             text="Something needs fixing before the update can work:" if any(
                 r["status"] == check_setup.FAIL for r in results) else "The setup check found something to look at:"
             ).pack(padx=14, pady=(12, 4), anchor="w")
    box = tk.Text(root, width=96, height=min(24, len(text.splitlines()) + 1), font=("Consolas", 9), wrap="word")
    box.insert("1.0", text)
    box.config(state="disabled")
    box.pack(padx=14, pady=4)
    tk.Button(root, text="Close", width=10, command=root.destroy).pack(pady=(4, 12))
    root.mainloop()
    return bool(problems)


try:
    import settings
    import procs
except SystemExit:                                         # e.g. .env names a folder that doesn't exist
    setup_check_window(only_if_problems=False)
    sys.exit(1)

DASHBOARD = settings.OUTPUT_FOLDER / "dashboard.html"
ICON = settings.CODE_FOLDER / "assets" / "portfolio.ico"
RUN_ALL = settings.CODE_FOLDER / "run_all.py"


def shift_held():
    """True if Shift is held down right now (Windows)."""
    try:
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x10) & 0x8000)
    except AttributeError:
        return False


def up_to_date():
    """Today's update has finished and the dashboard exists."""
    status = procs.read_status()
    finished_today = status.get("state") == "done" and str(status.get("finished", "")).startswith(f"{date.today()}")
    built_today = DASHBOARD.exists() and date.fromtimestamp(DASHBOARD.stat().st_mtime) == date.today()
    return finished_today and built_today


def start_update(notify=False):
    """run_all.py as a hidden process (its output goes to output/run.log)."""
    args = [procs.console_python(), str(RUN_ALL), "--no-open"] + (["--notify"] if notify else [])
    return subprocess.Popen(args, cwd=settings.CODE_FOLDER, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=procs.CREATE_NO_WINDOW if os.name == "nt" else 0)


def open_dashboard():
    if DASHBOARD.exists() and "--no-open" not in sys.argv:      # --no-open: for testing
        webbrowser.open(DASHBOARD.as_uri())


def start_lookup_in_background():
    """Make sure the lookup page runs (it takes a few seconds; don't wait for it)."""
    def go():
        try:
            import lookup_server
            lookup_server.start()
        except Exception:                                   # noqa: BLE001 - the dashboard still works
            pass
    t = threading.Thread(target=go)
    t.start()
    return t


# --- The small window ------------------------------------------

def update_window():
    """Show 'Updating your portfolio...' with the current step until the
    update (ours, or one that was already running) has finished."""
    import tkinter as tk
    from tkinter import ttk

    waiting = procs.lock_holder() is not None              # someone else is already updating
    proc = None if waiting else start_update()

    root = tk.Tk()
    root.title("Portfolio")
    root.resizable(False, False)
    try:
        root.iconbitmap(str(ICON))
    except tk.TclError:
        pass
    w, h = 420, 140
    root.geometry(f"{w}x{h}+{(root.winfo_screenwidth() - w) // 2}+{(root.winfo_screenheight() - h) // 3}")
    title = tk.Label(root, font=("Segoe UI", 12, "bold"),
                     text="Already updating - waiting for it to finish ..." if waiting
                     else "Updating your portfolio ...")
    title.pack(pady=(16, 6))
    step = tk.Label(root, font=("Segoe UI", 9), fg="#555", text="Starting ...", wraplength=w - 30)
    step.pack()
    bar = ttk.Progressbar(root, length=w - 40, mode="determinate", maximum=100)
    bar.pack(pady=10)

    def finished():
        if proc is not None:
            return proc.poll() is not None
        return procs.lock_holder() is None

    def tick():
        s = procs.read_status()
        if s.get("state") == "running":
            label = s.get("label") or s.get("step") or ""
            step.config(text=f"Step {s.get('index', 0) + 1} of {s.get('total', 0) + 1}: {label}" if s.get("total")
                        else label)
            if s.get("total"):
                bar["value"] = 100 * (s.get("index", 0)) / (s["total"] + 1)
        if finished():
            s = procs.read_status()
            failed = s.get("failed") or []
            bar["value"] = 100
            if "rules.toml" in failed:
                title.config(text="Not updated: rules.toml has problems")
                step.config(text="The line numbers and fixes are in output\\run.log in your data folder. "
                                 "Opening the last dashboard ...")
                open_dashboard()
                root.after(9000, root.destroy)
                return
            title.config(text="Done" if not failed else f"Done - {len(failed)} step(s) failed")
            step.config(text="Opening the dashboard ..." + (" Failed steps are listed at the top of it."
                                                            if failed else ""))
            open_dashboard()
            root.after(2500 if failed else 1200, root.destroy)
            return
        root.after(500, tick)

    root.after(300, tick)
    root.mainloop()


def main():
    forced = "--update" in sys.argv or shift_held()
    if "--scheduled" in sys.argv:
        # The Monday task: update (run_all waits by itself if one is running),
        # a notification at the end, no window, no dashboard.
        start_update(notify=True).wait()
        return
    if procs.lock_holder() is None and not forced and up_to_date():
        open_dashboard()
        start_lookup_in_background().join()                 # finish starting it before we exit
        return
    update_window()                                         # run_all starts the lookup at the end
    if procs.read_status().get("failed"):                   # a step failed: is the setup the cause?
        setup_check_window(only_if_problems=True)
    start_lookup_in_background().join()                     # (also if we only waited for another run)


if __name__ == "__main__":
    try:
        main()
    except Exception:                                       # noqa: BLE001 - pythonw has no console
        settings.OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
        with open(settings.OUTPUT_FOLDER / "launch_error.log", "a", encoding="utf-8") as f:
            f.write(f"\n{datetime.now():%Y-%m-%d %H:%M:%S}\n{traceback.format_exc()}")
        try:                                                # is the setup the cause? then say what to fix
            if not setup_check_window(only_if_problems=True):
                import tkinter.messagebox as mb
                mb.showerror("Portfolio", "Something went wrong - see output\\launch_error.log in the data folder.")
        except Exception:                                   # noqa: BLE001
            pass
