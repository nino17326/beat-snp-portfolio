# ============================================================
#  check_setup.py  -  is everything in place to run the tracker?
#
#      py check_setup.py
#
#  Checks, each with ✅ / ⚠️ / ❌ and a fix:
#    Python version, installed packages (vs. requirements.txt), .env,
#    the data folder, the Nordnet files, rules.toml, and that the output
#    folder can be written. launch.py runs it by itself if an update fails.
#  Exit code 0 = nothing blocking, 1 = something must be fixed.
# ============================================================

import importlib.metadata as meta
import os
import re
import sys
from pathlib import Path

CODE = Path(__file__).resolve().parent
OK, WARN, FAIL = "✅", "⚠️", "❌"
MIN_PYTHON = (3, 11)


def item(status, name, detail, fix=None):
    return {"status": status, "name": name, "detail": detail, "fix": fix}


def check_python():
    v = sys.version_info
    text = f"Python {v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) < MIN_PYTHON:
        return item(FAIL, "Python", text, f"install Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer from python.org")
    return item(OK, "Python", text)


def pinned():
    """{package: version} from requirements.txt."""
    out = {}
    for line in (CODE / "requirements.txt").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*([A-Za-z0-9_.\-]+)\s*==\s*([^\s#]+)", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def check_packages():
    items = []
    for package, wanted in pinned().items():
        try:
            have = meta.version(package)
        except meta.PackageNotFoundError:
            items.append(item(FAIL, f"package {package}", "not installed",
                              "py -m pip install -r requirements.txt"))
            continue
        if have != wanted:
            items.append(item(WARN, f"package {package}", f"{have} installed, {wanted} tested",
                              f"if something breaks: py -m pip install {package}=={wanted}"))
        else:
            items.append(item(OK, f"package {package}", have))
    return items


def check_env():
    env = CODE / ".env"
    if not env.exists():
        return item(OK, ".env", "not there - using the demo data",
                    None)
    text = env.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^\s*BEAT_SNP_DATA_DIR\s*=\s*(.*)$", text, re.M)
    if not m or not m.group(1).strip():
        return item(OK, ".env", "BEAT_SNP_DATA_DIR is empty - using the demo data")
    try:
        import dotenv  # noqa: F401
    except ImportError:
        return item(FAIL, ".env", "found, but python-dotenv isn't installed, so it's ignored",
                    "py -m pip install -r requirements.txt")
    return item(OK, ".env", "BEAT_SNP_DATA_DIR is set")


def check_data():
    """The data folder, the Nordnet files, rules.toml, writable output."""
    items = []
    try:
        import settings
    except SystemExit as e:                              # the folder in .env doesn't exist
        return [item(FAIL, "data folder", str(e), "fix the path in .env (or empty it to use the demo)")]
    folder = settings.DATA_FOLDER
    items.append(item(OK, "data folder", "demo_data" if settings.DEMO else "the folder named in .env"))

    for pattern, what, needed in [(settings.HISTORY_FILES, "Daglig udvikling export", True),
                                  (settings.TRANSACTIONS_FILES, "transaction export", True),
                                  (settings.HOLDINGS_FILES, "holdings export (Aktietabel)", False)]:
        files = settings.data_files(pattern)
        if files:
            items.append(item(OK, what, f"{len(files)} file(s)"))
        else:
            items.append(item(FAIL if needed else WARN, what, f"no '{pattern}' file in the data folder",
                              "export it from Nordnet and save it in the data folder"
                              + ("" if needed else " (only portfolio.py and the stale-data check use it)")))

    import checks
    if not settings.RULES_FILE.exists():
        items.append(item(FAIL, "rules.toml", "missing", "copy demo_data\\rules.toml to the data folder and edit it"))
    else:
        problems = checks.check_rules()
        errors = [p for p in problems if p["level"] == checks.ERROR]
        if errors:
            first = errors[0]
            where = f"line {first['line']}: " if first.get("line") else ""
            items.append(item(FAIL, "rules.toml", f"{len(errors)} problem(s), e.g. {where}{first['text']}",
                              first.get("fix") or "run py run_all.py to see every problem with its fix"))
        elif problems:
            items.append(item(WARN, "rules.toml", f"valid, {len(problems)} thing(s) worth checking",
                              "run py run_all.py to see them"))
        else:
            items.append(item(OK, "rules.toml", "valid"))

    try:
        settings.OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
        settings.write_text_safely(settings.OUTPUT_FOLDER / ".write_test", "ok")
        (settings.OUTPUT_FOLDER / ".write_test").unlink()
        items.append(item(OK, "output folder", "writable"))
    except OSError as e:
        items.append(item(FAIL, "output folder", f"can't write ({type(e).__name__})",
                          "check that the data folder isn't read-only or full"))
    return items


def run_checks():
    """Every check, in order (the package checks first: the rest needs them)."""
    results = [check_python()] + check_packages() + [check_env()]
    if any(r["status"] == FAIL for r in results):
        results.append(item(FAIL, "data and rules", "not checked - fix the items above first"))
        return results
    return results + check_data()


def report(results):
    lines = []
    for r in results:
        lines.append(f"{r['status']} {r['name']}: {r['detail']}")
        if r["fix"] and r["status"] != OK:
            lines.append(f"     Fix: {r['fix']}")
    failed = sum(r["status"] == FAIL for r in results)
    lines.append("")
    lines.append("Everything needed is in place." if not failed
                 else f"{failed} thing(s) must be fixed before the tracker can run.")
    return "\n".join(lines)


if __name__ == "__main__":
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")
    os.chdir(CODE)
    sys.path.insert(0, str(CODE))
    results = run_checks()
    print(report(results))
    sys.exit(1 if any(r["status"] == FAIL for r in results) else 0)
