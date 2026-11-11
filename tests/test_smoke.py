# ============================================================
#  tests/test_smoke.py  -  the whole update on the demo data
#
#      py -m pytest              (all tests, ~1 minute)
#
#  Runs run_all.py on a COPY of demo_data\ (so the real demo output isn't
#  touched) and checks that it completes, builds the dashboard, and that
#  the shadow-portfolio numbers and the time-weighted return still match
#  the known values. Run it after every change.
#  Needs internet the first time (Yahoo prices); with demo_data\cache\
#  present it reuses those prices.
# ============================================================

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

CODE = Path(__file__).resolve().parent.parent

# Known values for the demo portfolio (Nordnet history up to 2026-11-06).
# The account's own figures come straight from the demo exports, so they
# must match exactly; the CSPX shadow uses Yahoo prices, so it gets a
# small tolerance (Yahoo occasionally revises old prices a little).
KNOWN = {
    "date": "2026-11-06",
    "deposits": 148000.0,
    "value": 168476.97,
    "my_since_start": 26.853512105306688,     # time-weighted return since the start, %
    "my_ytd": 11.252725243556739,             # time-weighted return this year, %
    "shadow_value": 175553.82134943677,
    "shadow_since_start": 35.2941338137847,
    "shadow_ytd": 14.384182535203593,
}
EXACT = ["deposits", "value", "my_since_start", "my_ytd"]
SHADOW_TOLERANCE_PCT = 0.5                    # % of the value / percentage points


@pytest.fixture(scope="module")
def demo_run(tmp_path_factory):
    """run_all.py on a copy of the demo data. Returns (result, data folder)."""
    data = tmp_path_factory.mktemp("demo") / "data"
    shutil.copytree(CODE / "demo_data", data, ignore=shutil.ignore_patterns("output", "rules_backup"))
    env = {**os.environ, "BEAT_SNP_DATA_DIR": str(data), "PYTHONIOENCODING": "utf-8", "MPLBACKEND": "Agg"}
    result = subprocess.run([sys.executable, str(CODE / "run_all.py"), "--no-open", "--no-lookup"],
                            cwd=CODE, env=env, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=900)
    return result, data


def test_run_all_completes(demo_run):
    result, _ = demo_run
    assert result.returncode == 0, f"run_all.py failed:\n{result.stdout[-3000:]}\n{result.stderr[-2000:]}"
    assert "All steps finished." in result.stdout


def test_dashboard_is_created(demo_run):
    _, data = demo_run
    page = data / "output" / "dashboard.html"
    assert page.exists(), "dashboard.html wasn't written"
    text = page.read_text(encoding="utf-8")
    # (no "DEMO DATA" badge here: the copy runs as a custom data folder)
    assert "Portfolio dashboard" in text and "Actions (" in text


def test_shadow_and_time_weighted_return(demo_run):
    _, data = demo_run
    summary = json.loads((data / "output" / "shadow_summary.json").read_text(encoding="utf-8"))
    assert summary["date"] == KNOWN["date"]
    for key in EXACT:
        assert summary[key] == pytest.approx(KNOWN[key], abs=1e-6), key
    assert summary["shadow_value"] == pytest.approx(KNOWN["shadow_value"], rel=SHADOW_TOLERANCE_PCT / 100)
    for key in ["shadow_since_start", "shadow_ytd"]:
        assert summary[key] == pytest.approx(KNOWN[key], abs=SHADOW_TOLERANCE_PCT), key


def test_run_log_and_step_status(demo_run):
    _, data = demo_run
    out = data / "output"
    assert "===== RUN START" in (out / "run.log").read_text(encoding="utf-8")
    steps = json.loads((out / "steps.json").read_text(encoding="utf-8"))
    assert all(s["ok"] for s in steps.values()), steps
    assert not (out / "run.lock").exists(), "the run lock wasn't released"
