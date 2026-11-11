# ============================================================
#  settings.py  -  where the data lives (one place for every path)
#
#  No personal data is stored in the code. It all lives in ONE folder,
#  named by the environment variable BEAT_SNP_DATA_DIR. The easy way
#  to set it is a file called .env next to this script (copy
#  .env.example to .env and fill in the path).
#
#  That folder holds:
#    Daglig udvikling*.csv    Nordnet: daily account value + deposits
#    transactions*.csv        Nordnet: every transaction
#    Aktietabel*.csv          Nordnet: today's holdings
#    rules.toml               my rules, holdings map, theses
#    output\                  everything the scripts write
#    rules_backup\            dated copies, made before the lookup page edits rules.toml
#    cache\                   downloaded Yahoo data (data_cache.py)
#
#  If BEAT_SNP_DATA_DIR isn't set, everything uses demo_data\ instead
#  (synthetic files that ship with the project).
# ============================================================

import os
import tomllib
from pathlib import Path

CODE_FOLDER = Path(__file__).resolve().parent      # the folder this script is in
ENV_FILE = CODE_FOLDER / ".env"

# python-dotenv reads .env and puts its lines into the environment, as
# if they had been set by hand. It never overrides a variable that is
# already set, so a value set in the terminal wins.
try:
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)
except ImportError:
    if ENV_FILE.exists():
        print("WARNING: .env found but python-dotenv isn't installed, so it was ignored "
              "(using demo data). Install it with: py -m pip install python-dotenv")

_setting = os.environ.get("BEAT_SNP_DATA_DIR", "").strip()
DEMO = _setting == ""                               # True = no data folder set: use the demo
DATA_FOLDER = CODE_FOLDER / "demo_data" if DEMO else Path(_setting).expanduser()
if not DATA_FOLDER.is_dir():
    raise SystemExit(f"BEAT_SNP_DATA_DIR points to a folder that doesn't exist: {DATA_FOLDER}")

OUTPUT_FOLDER = DATA_FOLDER / "output"              # dashboard, reports, charts, CSVs
RULES_FILE = DATA_FOLDER / "rules.toml"
BACKUP_FOLDER = DATA_FOLDER / "rules_backup"

# Nordnet's export names, found by pattern (never by account number).
HISTORY_FILES = "Daglig udvikling*.csv"
TRANSACTIONS_FILES = "transactions*.csv"
HOLDINGS_FILES = "Aktietabel*.csv"


def data_files(pattern):
    """Every file in the data folder whose name matches, sorted by name."""
    return sorted(DATA_FOLDER.glob(pattern))


def newest_data_file(pattern):
    """The most recently saved file in the data folder whose name matches."""
    files = data_files(pattern)
    if not files:
        raise SystemExit(f"No '{pattern}' file found in {DATA_FOLDER}")
    return max(files, key=lambda f: f.stat().st_mtime)


def load_rules():
    """rules.toml from the data folder, as a dictionary."""
    if not RULES_FILE.exists():
        raise SystemExit(f"No rules.toml in {DATA_FOLDER} - copy demo_data\\rules.toml there and edit it")
    with open(RULES_FILE, "rb") as f:
        return tomllib.load(f)


def where():
    """One line saying which data is being used (printed by run_all.py)."""
    return f"demo data ({DATA_FOLDER})" if DEMO else f"BEAT_SNP_DATA_DIR = {DATA_FOLDER}"
