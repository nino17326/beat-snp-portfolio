# ============================================================
#  run_all.py  -  update everything with one command:
#
#      py run_all.py              (add --no-open to skip opening the dashboard)
#
#  Runs each script in turn. If one fails, it prints the error and
#  carries on with the rest; a summary at the end shows what worked.
#  Order matters: later steps read what earlier steps saved.
#  Before any step, checks.py validates rules.toml and the Nordnet
#  exports; a broken rules.toml stops everything with a clear message.
#  Which data it uses (yours or the demo) is decided by settings.py.
# ============================================================

import json
import os
import subprocess
import sys
import time
from datetime import datetime

from settings import CODE_FOLDER, OUTPUT_FOLDER, where
from settings import write_text_safely   # crash-safe writes (temp file, then rename)
import checks                             # rules.toml + export checks, run before anything else

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
    env = {**os.environ, "MPLBACKEND": "Agg", "PYTHONIOENCODING": "utf-8",
           "BEAT_SNP_CHECKED": "1"}                 # the steps don't repeat the warnings below
    results = []
    print(f"Data: {where()}", flush=True)

    # 0. Check rules.toml and the Nordnet files BEFORE anything runs.
    #    A broken rules.toml stops here with line numbers and fixes.
    print(f"\n{'=' * 70}\n>>> checks: rules.toml and the Nordnet exports\n{'=' * 70}", flush=True)
    rules_problems, export_problems = checks.check_everything()
    if any(p["level"] == checks.ERROR for p in rules_problems):
        print(checks.rules_report(rules_problems))
        print("\nNothing was run. Fix the lines above in rules.toml and start again.")
        sys.exit(2)
    for p in rules_problems:
        print("  " + checks.describe(p).replace("\n", "\n  "))
    for p in export_problems:
        print("  " + checks.describe(p, "export").replace("\n", "\n  "))
    if not rules_problems and not export_problems:
        print("  ✅ rules.toml and the exports look fine")

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
    write_text_safely(TIMES_FILE, json.dumps(history[-50:], indent=1))
    failed = [s for s, e, _ in results if e]
    print("\nAll steps finished." if not failed else f"\n{len(failed)} step(s) failed: {', '.join(failed)}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
