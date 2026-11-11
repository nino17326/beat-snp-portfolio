# ============================================================
#  run_all.py  -  update everything with one command:
#
#      py run_all.py              (add --no-open to skip opening the dashboard)
#
#  Runs each script in turn. If one fails, it prints the error and
#  carries on with the rest; a summary at the end shows what worked,
#  and the dashboard says which part failed and when it was last good.
#  Order matters: later steps read what earlier steps saved.
#  Before any step, checks.py validates rules.toml and the Nordnet
#  exports; a broken rules.toml stops everything with a clear message.
#  Which data it uses (yours or the demo) is decided by settings.py.
#
#  Only one update runs at a time (a lock file): starting a second one,
#  e.g. by double-clicking twice or while the Monday task runs, just
#  waits for the first to finish. Everything printed also goes to
#  output\run.log with timestamps (the last 10 runs are kept).
#  At the end the lookup page is started in the background (no window).
#
#  Options: --no-open (don't open the dashboard), --no-lookup (don't
#  start the lookup), --notify (Windows notification when done).
# ============================================================

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

from settings import CODE_FOLDER, OUTPUT_FOLDER, where
from settings import write_text_safely   # crash-safe writes (temp file, then rename)
import checks                             # rules.toml + export checks, run before anything else
import procs                              # lock, status file, hidden processes

TIMES_FILE = OUTPUT_FOLDER / "run_times.json"     # every run's step times, to compare runs
KEEP_RUNS = 10                                    # runs kept in run.log
RUN_MARK = "===== RUN START"

TRACKER = CODE_FOLDER

STEPS = [
    ("shadow_portfolio.py", "account vs. CSPX shadow (dashboard top numbers + chart)"),
    ("regime.py",           "market regime history (dashboard regime chart, ~1 min)"),
    ("scanner.py",          "scanner (before weekly_check, which shows its results)"),
    ("weekly_check.py",     "weekly report"),
    ("risk.py",             "risk report"),
    ("dashboard.py",        "dashboard (opens in your browser)"),
]


# --- The log: console + run.log with timestamps ----------------

class Log:
    def __init__(self):
        OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
        try:                                       # keep the last KEEP_RUNS - 1 runs, then add this one
            old = procs.LOG_FILE.read_text(encoding="utf-8")
            runs = old.split(RUN_MARK)[1:]
            write_text_safely(procs.LOG_FILE, "".join(RUN_MARK + r for r in runs[-(KEEP_RUNS - 1):]))
        except OSError:
            pass
        self.file = open(procs.LOG_FILE, "a", encoding="utf-8")
        self.file.write(f"{RUN_MARK} {procs.now_text()} (process {os.getpid()}) =====\n")

    def __call__(self, text=""):
        for line in str(text).split("\n"):
            if sys.stdout:
                print(line, flush=True)
            self.file.write(f"{datetime.now():%H:%M:%S}  {line}\n")
        self.file.flush()

    def close(self):
        self.file.close()


def attention_count():
    """'N items need attention' from this week's report (None if unknown)."""
    reports = sorted((OUTPUT_FOLDER / "weekly").glob("*.md"))
    if not reports:
        return None
    text = reports[-1].read_text(encoding="utf-8")
    m = re.search(r"\*\*(\d+) item\(s\) need attention", text)
    return int(m.group(1)) if m else (0 if "All clear" in text else None)


def notify(title, text):
    """A small Windows notification (best effort; silently skipped if it can't)."""
    if os.name != "nt":
        return
    q = lambda s: s.replace("'", "''")                                  # noqa: E731 - PowerShell quoting
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
        "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$x = $t.GetElementsByTagName('text');"
        f"$x.Item(0).AppendChild($t.CreateTextNode('{q(title)}')) > $null;"
        f"$x.Item(1).AppendChild($t.CreateTextNode('{q(text)}')) > $null;"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe')"
        ".Show([Windows.UI.Notifications.ToastNotification]::new($t))")
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, timeout=30, creationflags=procs.CREATE_NO_WINDOW)
    except Exception:                                                   # noqa: BLE001 - optional
        pass


def run_step(script, extra, env, log):
    """One script as its own process (a crash can't take down the others),
    its output passed on line by line to the console and run.log."""
    proc = subprocess.Popen([procs.console_python(), "-W", "ignore", str(TRACKER / script), *extra],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace",
                            creationflags=procs.CREATE_NO_WINDOW if os.name == "nt" else 0)
    for line in proc.stdout:
        log(line.rstrip("\n"))
    return proc.wait()


def main():
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")

    # Only one update at a time.
    if not procs.take_lock():
        pid, started = procs.lock_holder() or ("?", "?")
        print(f"Already updating (started {started}, process {pid}) - waiting for it to finish ...", flush=True)
        procs.wait_for_unlock()
        print("The other update has finished.")
        sys.exit(0)

    log = Log()
    failed = []
    try:
        failed = update(log)
    except SystemExit:                             # a deliberate stop (e.g. rules.toml problems)
        raise
    except BaseException as e:                     # noqa: BLE001 - log it, then let it end the run
        log(f"!!! run_all stopped: {type(e).__name__}: {e}")
        procs.write_status(state="done", failed=["run_all"], step=None)
        raise
    finally:
        log.close()
        procs.release_lock()
    sys.exit(1 if failed else 0)


def update(log):
    # MPLBACKEND=Agg: scripts save their charts instead of opening chart
    # windows, which would otherwise pause everything until you close them.
    env = {**os.environ, "MPLBACKEND": "Agg", "PYTHONIOENCODING": "utf-8",
           "BEAT_SNP_CHECKED": "1"}                 # the steps don't repeat the warnings below
    results = []
    total_steps = len(STEPS) + 1
    log(f"Data: {where()}")

    # 0. Check rules.toml and the Nordnet files BEFORE anything runs.
    #    A broken rules.toml stops here with line numbers and fixes.
    procs.write_status(state="running", step="Checking rules.toml and the exports", index=0, total=total_steps)
    log(f"\n{'=' * 70}\n>>> checks: rules.toml and the Nordnet exports\n{'=' * 70}")
    rules_problems, export_problems = checks.check_everything()
    if any(p["level"] == checks.ERROR for p in rules_problems):
        log(checks.rules_report(rules_problems))
        log("\nNothing was run. Fix the lines above in rules.toml and start again.")
        procs.write_status(state="done", failed=["rules.toml"], step=None,
                           message="rules.toml has problems - see output\\run.log")
        sys.exit(2)
    for p in rules_problems:
        log("  " + checks.describe(p).replace("\n", "\n  "))
    for p in export_problems:
        log("  " + checks.describe(p, "export").replace("\n", "\n  "))
    if not rules_problems and not export_problems:
        log("  ✅ rules.toml and the exports look fine")

    # Settle the lookup's port now, so the scanner report and the dashboard
    # link to the right one (the lookup itself is started at the end).
    if "--no-lookup" not in sys.argv:
        import lookup_server
        log(f"  Lookup port: {lookup_server.reserve_port()}")

    for i, (script, what) in enumerate(STEPS, start=1):
        procs.write_status(state="running", step=f"{script}: {what}", index=i, total=total_steps,
                           label=what.split(" (")[0])
        log(f"\n{'=' * 70}\n>>> {script}: {what}\n{'=' * 70}")
        start = time.time()
        try:
            extra = ["--no-open"] if script == "dashboard.py" and "--no-open" in sys.argv else []
            code = run_step(script, extra, env, log)
            error = None if code == 0 else f"stopped with exit code {code} (see the error above)"
        except Exception as e:                       # e.g. the file is missing
            error = f"{type(e).__name__}: {e}"
        procs.record_step(script, ok=error is None, error=error)   # the dashboard shows failures
        results.append((script, error, time.time() - start))
        if error:
            log(f"\n!!! {script} failed: {error} - carrying on with the next step.")

    # Step times: this run next to the previous one (e.g. before/after the data cache).
    try:
        history = json.loads(TIMES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        history = []
    last = history[-1] if history else None
    log(f"\n{'=' * 70}\nSummary" + (f"   (previous run {last['when']} → this run)" if last else ""))
    for script, error, seconds in results:
        before = f"{last['steps'][script]:5.0f} s →" if last and script in last["steps"] else ""
        log(f"  {'✅' if error is None else '❌'} {script:<20} {before:>9} {seconds:5.0f} s  {error or ''}")
    total = sum(sec for _, _, sec in results)
    before = f"{sum(last['steps'].values()):5.0f} s →" if last else ""
    log(f"     {'total':<20} {before:>9} {total:5.0f} s")
    history.append({"when": f"{datetime.now():%Y-%m-%d %H:%M}",
                    "steps": {script: round(sec, 1) for script, _, sec in results}})
    write_text_safely(TIMES_FILE, json.dumps(history[-50:], indent=1))
    failed = [s for s, e, _ in results if e]
    log("\nAll steps finished." if not failed else f"\n{len(failed)} step(s) failed: {', '.join(failed)}")

    # The lookup page, hidden in the background (if it isn't running yet).
    if "--no-lookup" not in sys.argv:
        procs.write_status(state="running", step="Starting the lookup page", index=total_steps, total=total_steps)
        try:
            import lookup_server
            lookup_server.start()
            log(f"Lookup: http://localhost:{lookup_server.saved_port()} (output\\lookup.log)")
        except Exception as e:                       # noqa: BLE001 - the update itself is done
            log(f"⚠️ The lookup page couldn't be started: {type(e).__name__}: {e}")

    procs.write_status(state="done", failed=failed, step=None, finished=procs.now_text())
    if "--notify" in sys.argv:
        n = attention_count()
        text = (f"{n} item{'s' if n != 1 else ''} need{'s' if n == 1 else ''} attention" if n is not None
                else "see the dashboard")
        if failed:
            text += f" · {len(failed)} step(s) failed"
        notify("Portfolio updated", text)
    return failed


if __name__ == "__main__":
    main()
