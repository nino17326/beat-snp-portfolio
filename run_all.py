# ============================================================
#  run_all.py  -  update everything with one command:
#
#      py run_all.py              (add --no-open to skip opening the dashboard)
#
#  Runs each script in turn. If one fails, it prints the error and
#  carries on with the rest; a summary at the end shows what worked.
#  Order matters: later steps read what earlier steps saved.
#  Which data it uses (yours or the demo) is decided by settings.py.
# ============================================================

import json
import os
import subprocess
import sys
import time
from datetime import datetime

from settings import CODE_FOLDER, OUTPUT_FOLDER, where

TIMES_FILE = OUTPUT_FOLDER / "run_times.json"     # every run's step times, to compare runs

TRACKER = CODE_FOLDER

STEPS = [
    ("shadow_portfolio.py", "account vs. CSPX shadow (dashboard top numbers + chart)"),
    ("regime.py",           "market regime history (dashboard regime chart, ~1 min)"),
    ("scanner.py",          "scanner (before weekly_check, which shows its results)"),
    ("weekly_check.py",     "weekly report"),
    ("risk.py",             "risk report"),
    ("dashboard.py",        "dashboard (opens in your browser)"),
]


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    # MPLBACKEND=Agg: scripts save their charts instead of opening chart
    # windows, which would otherwise pause everything until you close them.
    env = {**os.environ, "MPLBACKEND": "Agg", "PYTHONIOENCODING": "utf-8"}
    results = []
    print(f"Data: {where()}", flush=True)

    for script, what in STEPS:
        print(f"\n{'=' * 70}\n>>> {script}: {what}\n{'=' * 70}", flush=True)
        start = time.time()
        try:
            # Each script runs as its own Python process, so a crash in one
            # can't take down the others.
            extra = ["--no-open"] if script == "dashboard.py" and "--no-open" in sys.argv else []
            done = subprocess.run([sys.executable, "-W", "ignore", str(TRACKER / script), *extra], env=env)
            error = None if done.returncode == 0 else f"stopped with exit code {done.returncode} (see the error above)"
        except Exception as e:                       # e.g. the file is missing
            error = f"{type(e).__name__}: {e}"
        results.append((script, error, time.time() - start))
        if error:
            print(f"\n!!! {script} failed: {error} - carrying on with the next step.", flush=True)

    # Step times: this run next to the previous one (e.g. before/after the data cache).
    try:
        history = json.loads(TIMES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        history = []
    last = history[-1] if history else None
    print(f"\n{'=' * 70}\nSummary" + (f"   (previous run {last['when']} → this run)" if last else ""))
    for script, error, seconds in results:
        before = f"{last['steps'][script]:5.0f} s →" if last and script in last["steps"] else ""
        print(f"  {'✅' if error is None else '❌'} {script:<20} {before:>9} {seconds:5.0f} s  {error or ''}")
    total = sum(sec for _, _, sec in results)
    before = f"{sum(last['steps'].values()):5.0f} s →" if last else ""
    print(f"     {'total':<20} {before:>9} {total:5.0f} s")
    history.append({"when": f"{datetime.now():%Y-%m-%d %H:%M}",
                    "steps": {script: round(sec, 1) for script, _, sec in results}})
    OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
    TIMES_FILE.write_text(json.dumps(history[-50:], indent=1), encoding="utf-8")
    failed = [s for s, e, _ in results if e]
    print("\nAll steps finished." if not failed else f"\n{len(failed)} step(s) failed: {', '.join(failed)}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
