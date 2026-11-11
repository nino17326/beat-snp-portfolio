# ============================================================
#  checks.py  -  protection against mistakes in my files
#
#  1. rules.toml: syntax, missing sections, wrong types ("true" in
#     quotes, dates in quotes, numbers in quotes), ticker endings,
#     ask_ok values, duplicate tickers. Every problem gets a line
#     number and a plain-English fix, instead of a crash.
#  2. The Nordnet exports: new holdings that aren't set up, older
#     transaction exports, duplicate history exports, stale data.
#
#  run_all.py checks everything first; settings.load_rules() refuses to
#  run on a broken rules.toml; the dashboard shows the warnings at the top.
# ============================================================

import datetime as dt
import difflib
import re
import sys
import tomllib
from pathlib import Path

import pandas as pd

import settings

ERROR, WARN, INFO = "❌", "⚠️", "ℹ️"
FX_CURRENCIES = {"DKK", "EUR", "USD", "GBP", "CHF", "SEK", "NOK"}
# Yahoo endings for the exchanges a Nordnet ASK can trade.
ENDINGS = {".CO": "Copenhagen", ".ST": "Stockholm", ".OL": "Oslo", ".HE": "Helsinki", ".DE": "Xetra",
           ".F": "Frankfurt", ".PA": "Paris", ".AS": "Amsterdam", ".BR": "Brussels", ".MI": "Milan",
           ".MC": "Madrid", ".LS": "Lisbon", ".SW": "Swiss", ".L": "London", ".VI": "Vienna", ".IR": "Dublin"}
DEFAULT_MAX_EXPORT_AGE_DAYS = 35


class RulesError(SystemExit):
    """rules.toml can't be used. Subclass of SystemExit: the scripts stop
    with the plain-English message instead of a long traceback."""

    def __init__(self, message):
        try:                                   # so ❌ prints as ❌ in any console
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
        super().__init__(message)


def problem(level, text, line=None, fix=None):
    return {"level": level, "line": line, "text": text, "fix": fix}


def describe(p, file_name="rules.toml"):
    where = f"{file_name} line {p['line']}: " if p.get("line") else ""
    return f"{p['level']} {where}{p['text']}" + (f"\n     Fix: {p['fix']}" if p.get("fix") else "")


# --- Finding line numbers in rules.toml -----------------------

class Lines:
    """Line numbers of tables and keys in the rules.toml text."""

    HEADER = re.compile(r'^\s*\[\[?\s*([^\[\]]+?)\s*\]\]?\s*(#.*)?$')
    KEY = re.compile(r'^\s*("[^"]+"|[A-Za-z0-9_.\-]+)\s*=')

    def __init__(self, text):
        self.lines = text.splitlines()
        self.keys = []                       # (line no, table, key)
        self.tables = {}                     # table -> first line
        table = ""
        for n, line in enumerate(self.lines, 1):
            m = self.HEADER.match(line)
            if m:
                table = self._norm(m.group(1))
                self.tables.setdefault(table, n)
                continue
            m = self.KEY.match(line)
            if m:
                self.keys.append((n, table, m.group(1).strip('"')))

    @staticmethod
    def _norm(path):
        return ".".join(part.strip().strip('"') for part in re.findall(r'"[^"]*"|[^.]+', path))

    def key(self, table, key):
        return next((n for n, t, k in self.keys if t == table and k == key), self.tables.get(table))

    def table(self, table):
        return self.tables.get(table)

    def text(self, pattern, after=None):
        """First line matching a regex (optionally after a given line)."""
        rx = re.compile(pattern)
        return next((n for n, line in enumerate(self.lines, 1)
                     if (after is None or n > after) and rx.search(line)), None)


# --- Type checks with plain-English fixes ---------------------

def _shown(v):
    return f'"{v}"' if isinstance(v, str) else repr(v)


def _check_type(kind, value, name, line, out, choices=None):
    """Append a problem if `value` isn't of `kind`. Returns True if OK."""
    def bad(text, fix):
        out.append(problem(ERROR, text, line, fix))
        return False

    if kind == "number":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if isinstance(value, str) and re.fullmatch(r"-?[\d_]+([.,]\d+)?", value.strip()):
            return bad(f"{name} should be a number, but it's text ({_shown(value)}).",
                       f"remove the quotes (and use a dot for decimals): {name.split('.')[-1]} = "
                       f"{value.strip().replace(',', '.')}")
        return bad(f"{name} should be a number, but it's {_shown(value)}.",
                   f"write a plain number, e.g. {name.split('.')[-1]} = 15")
    if kind == "bool":
        if isinstance(value, bool):
            return True
        if isinstance(value, str) and value.strip().lower() in ("true", "false", "yes", "no"):
            word = "true" if value.strip().lower() in ("true", "yes") else "false"
            return bad(f"{name} should be true or false, but it's the text {_shown(value)}.",
                       f"write {word} without quotes: {name.split('.')[-1]} = {word}")
        return bad(f"{name} should be true or false, but it's {_shown(value)}.",
                   f"write true or false (no quotes)")
    if kind == "date":
        if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
            return True
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
            return bad(f"{name} should be a date, but it's text ({_shown(value)}).",
                       f"dates have no quotes: {name.split('.')[-1]} = {value.strip()}")
        return bad(f"{name} should be a date like 2026-11-08, but it's {_shown(value)}.",
                   "write the date as YYYY-MM-DD without quotes")
    if kind == "text":
        if isinstance(value, str):
            if choices and value not in choices:
                return bad(f"{name} is {_shown(value)}, which isn't one of: {', '.join(choices)}.",
                           f"use one of: {', '.join(_shown(c) for c in choices)}")
            return True
        return bad(f"{name} should be text in quotes, but it's {_shown(value)}.",
                   f'put it in quotes: {name.split(".")[-1]} = "{value}"')
    if kind == "list":
        if isinstance(value, list):
            return True
        return bad(f"{name} should be a list in [ ], but it's {_shown(value)}.",
                   f'write it as a list, e.g. {name.split(".")[-1]} = ["A", "B"]')
    if kind == "table":
        if isinstance(value, dict):
            return True
        return bad(f"{name} should be a section/table, but it's {_shown(value)}.", None)
    raise ValueError(kind)


def _fields(entry, spec, prefix, line, out):
    """Check the fields of one table/inline table against {field: (kind, required, choices)}."""
    for field, (kind, required, *rest) in spec.items():
        choices = rest[0] if rest else None
        if field not in entry:
            if required:
                out.append(problem(ERROR, f"{prefix} is missing '{field}'.", line,
                                   f"add {field} = ... (see demo_data\\rules.toml for an example)"))
            continue
        _check_type(kind, entry[field], f"{prefix}.{field}", line, out, choices)


def _ticker_problem(ticker, where, line, usd, out):
    """Warn about tickers without (or with an unknown) exchange ending."""
    if not isinstance(ticker, str) or ticker.startswith("^") or ticker.endswith("=X"):
        return
    if "." not in ticker:
        if not usd:
            out.append(problem(WARN, f"{where}: ticker {ticker} has no exchange ending. That's only right for "
                               "a US listing.", line,
                               f"for Europe add the ending, e.g. {ticker}.DE (Xetra), {ticker}.PA (Paris), "
                               f"{ticker}.CO (Copenhagen); check it on finance.yahoo.com"))
        return
    ending = "." + ticker.rsplit(".", 1)[1]
    if ending not in ENDINGS:
        out.append(problem(WARN, f"{where}: ticker {ticker} has an ending ({ending}) I don't recognise.", line,
                           "check the ticker on finance.yahoo.com; known endings: " + ", ".join(ENDINGS)))


# --- 1. rules.toml --------------------------------------------

def check_rules_text(text):
    """All problems in rules.toml text, as a list (errors and warnings)."""
    try:
        rules = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return [toml_syntax_problem(e, text)]
    out = []
    L = Lines(text)

    def section(name, required=True):
        value = rules
        for part in name.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if value is None:
            if required:
                out.append(problem(ERROR, f"The section [{name}] is missing.", None,
                                   f"copy [{name}] from demo_data\\rules.toml and adjust the values"))
            return None
        if not isinstance(value, dict):
            out.append(problem(ERROR, f"[{name}] should be a section.", L.table(name), None))
            return None
        return value

    def keys(name, spec):
        sec = section(name)
        if sec is None:
            return None
        for key, (kind, required, *rest) in spec.items():
            if key not in sec:
                if required:
                    out.append(problem(ERROR, f"[{name}] is missing '{key}'.", L.table(name),
                                       f"add a line {key} = ... under [{name}] (see demo_data\\rules.toml)"))
                continue
            _check_type(kind, sec[key], key, L.key(name, key), out, rest[0] if rest else None)
        return sec

    num, req, opt = "number", True, False
    known_sections = ["portfolio", "groups", "alerts", "calendar", "ask", "stocks", "risk", "scanner",
                      "watchlist", "results", "securities", "costs"]
    for name in rules:
        if name not in known_sections:
            close = difflib.get_close_matches(name, known_sections, n=1)
            out.append(problem(WARN, f"Unknown section [{name}] - the scripts ignore it.", L.table(name),
                               f"did you mean [{close[0]}]?" if close else "check the spelling"))
    securities = section("securities") or {}
    usd_tickers = set()
    tickers_by_isin = {}
    for isin, s in securities.items():
        line = L.key("securities", isin)
        if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", isin):
            out.append(problem(WARN, f"[securities] '{isin}' doesn't look like an ISIN (2 letters + 10 characters).",
                               line, "copy the ISIN exactly from the Nordnet transaction export"))
        if not isinstance(s, dict):
            out.append(problem(ERROR, f"[securities] {isin} should look like {{ ticker = \"...\", currency = \"...\" }}.",
                               line, None))
            continue
        _fields(s, {"ticker": ("text", req), "currency": ("text", req, sorted(FX_CURRENCIES))},
                f"[securities] {isin}", line, out)
        if s.get("currency") == "USD":
            usd_tickers.add(s.get("ticker"))
        tickers_by_isin[isin] = s.get("ticker")
        _ticker_problem(s.get("ticker"), f"[securities] {isin}", line, s.get("currency") == "USD", out)
    for ticker in {t for t in tickers_by_isin.values() if list(tickers_by_isin.values()).count(t) > 1}:
        isins = [i for i, t in tickers_by_isin.items() if t == ticker]
        out.append(problem(ERROR, f"[securities] ticker {ticker} is used for several ISINs: {', '.join(isins)}.",
                           L.key("securities", isins[1]), "each ISIN needs its own Yahoo ticker"))
    known = set(tickers_by_isin.values())

    p = keys("portfolio", {"core_ticker": ("text", req), "core_min_pct": (num, req),
                           "satellites_max_pct": (num, req), "single_stock_max_pct": (num, req),
                           "funds": ("list", req), "include_cash": ("bool", req)})
    if p and isinstance(p.get("core_ticker"), str) and securities and p["core_ticker"] not in known:
        out.append(problem(ERROR, f"core_ticker {p['core_ticker']} isn't in [securities].",
                           L.key("portfolio", "core_ticker"), "use the same Yahoo ticker as in [securities]"))

    groups = section("groups") or {}
    if "semiconductors" not in groups:
        out.append(problem(ERROR, "[groups.semiconductors] is missing (the risk and action checks use it).", None,
                           "copy [groups.semiconductors] from demo_data\\rules.toml"))
    for name, g in groups.items():
        line = L.table(f"groups.{name}")
        if isinstance(g, dict):
            _fields(g, {"tickers": ("list", req), "max_pct": (num, req)}, f"[groups.{name}]", line, out)

    keys("alerts", {"week_lag_points": (num, req), "four_week_lag_points": (num, req),
                    "max_red_checks": (num, req), "stale_after_days": (num, req),
                    "estimate_cut_pct": (num, opt), "max_export_age_days": (num, opt)})

    cal = keys("calendar", {"days_ahead": (num, req), "events": ("list", req)})
    for e in (cal or {}).get("events", []) if isinstance((cal or {}).get("events"), list) else []:
        line = L.text(re.escape(str(e.get("label", "@@"))) if isinstance(e, dict) else "@@") or L.key("calendar", "events")
        if isinstance(e, dict):
            _fields(e, {"date": ("date", req), "label": ("text", req)}, "[calendar] event", line, out)

    ask = keys("ask", {"tax_rate": (num, req), "cash_buffer": (num, req), "ceilings": ("table", req)})
    for year, ceiling in ((ask or {}).get("ceilings") or {}).items() if isinstance((ask or {}).get("ceilings"), dict) else []:
        _check_type(num, ceiling, f"ceilings.{year}", L.key("ask", "ceilings"), out)

    for ticker, stock in (section("stocks") or {}).items():
        line = L.table(f"stocks.{ticker}")
        if not isinstance(stock, dict):
            continue
        _fields(stock, {"name": ("text", req), "levels": ("list", opt), "thesis": ("text", opt)},
                f"[stocks.\"{ticker}\"]", line, out)
        _ticker_problem(ticker, f'[stocks."{ticker}"]', line, ticker in usd_tickers, out)
        for lv in stock.get("levels", []) if isinstance(stock.get("levels"), list) else []:
            if isinstance(lv, dict):
                _fields(lv, {"label": ("text", req), "at_or_below": (num, req)},
                        f'[stocks."{ticker}"] level', L.text(r"at_or_below", after=line) or line, out)
        checks_line = L.table(f"stocks.{ticker}.checks")
        for name, c in (stock.get("checks") or {}).items():
            cl = L.key(f"stocks.{ticker}.checks", name) or checks_line
            if not isinstance(c, dict):
                out.append(problem(ERROR, f'Story check "{name}" should look like '
                                   '{ status = "green", checked = 2026-11-08 }.', cl, None))
                continue
            _fields(c, {"status": ("text", req, ["green", "red", "unknown"])}, f'Story check "{name}"', cl, out)
            if "checked" not in c:
                out.append(problem(ERROR, f'Story check "{name}" is missing checked = ...', cl,
                                   'add checked = 2026-11-08 (no quotes), or checked = "never"'))
            elif c["checked"] != "never":
                _check_type("date", c["checked"], f'Story check "{name}".checked', cl, out)

    risk = keys("risk", {"lookback_days": (num, req), "semis_max_risk_share": (num, req),
                         "currency": ("table", req), "scenarios": ("list", req)})
    for ticker, cur in ((risk or {}).get("currency") or {}).items() if isinstance((risk or {}).get("currency"), dict) else []:
        line = L.key("risk.currency", ticker)
        mix = cur if isinstance(cur, dict) else {cur: 1}
        for c, share in mix.items():
            if c not in FX_CURRENCIES:
                out.append(problem(ERROR, f"[risk.currency] {ticker}: unknown currency {_shown(c)}.", line,
                                   "use one of " + ", ".join(sorted(FX_CURRENCIES))))
            if isinstance(cur, dict):
                _check_type(num, share, f"[risk.currency] {ticker}.{c}", line, out)
    for s in (risk or {}).get("scenarios", []) if isinstance((risk or {}).get("scenarios"), list) else []:
        if isinstance(s, dict):
            line = L.text(re.escape(str(s.get("name", "@@")))) or L.table("risk.scenarios")
            _fields(s, {"name": ("text", req), "kind": ("text", req, ["group", "market", "currency"]),
                        "shock_pct": (num, req)}, "[[risk.scenarios]]", line, out)
            need = {"group": "group", "market": "market", "currency": "currency"}.get(s.get("kind"))
            if need and need not in s:
                out.append(problem(ERROR, f"Scenario {_shown(s.get('name'))} (kind = {s['kind']}) needs '{need}'.",
                                   line, f"add {need} = ..."))
            elif need == "group" and s["group"] not in groups:
                out.append(problem(ERROR, f"Scenario {_shown(s.get('name'))} uses group {_shown(s['group'])}, "
                                   "which isn't a [groups.X] section.", line, None))

    sc = keys("scanner", {"min_market_cap_eur": (num, req), "max_share_price_dkk": (num, req),
                          "max_correlation": (num, req), "excluded_sectors": ("list", req),
                          "min_score_gap": (num, req), "max_swaps_per_quarter": (num, req),
                          "new_position_pct": (num, req), "max_sector_pct": (num, req),
                          "max_net_debt_ebitda_utilities": (num, req), "min_factors": (num, req),
                          "min_candidate_score": (num, req), "universe": ("list", req)})
    entry_spec = {"ticker": ("text", req), "name": ("text", opt), "type": ("text", req, ["stock", "etf"]),
                  "sector": ("text", req), "role": ("text", req, ["growth", "diversifier"])}
    seen = {}

    def candidate(c, where, line):
        if not isinstance(c, dict):
            out.append(problem(ERROR, f"{where}: each entry should look like {{ ticker = \"...\", ... }}.", line, None))
            return
        _fields(c, entry_spec, f"{where} {c.get('ticker', '?')}", line, out)
        ask_ok = c.get("ask_ok", "unknown")
        if not (isinstance(ask_ok, bool) or ask_ok == "unknown"):
            fix = ("write true or false without quotes, or \"unknown\" (with quotes) if you haven't checked"
                   if isinstance(ask_ok, str) else "use true, false or \"unknown\"")
            out.append(problem(ERROR, f"{where} {c.get('ticker', '?')}: ask_ok is {_shown(ask_ok)}.", line, fix))
        _ticker_problem(c.get("ticker"), f"{where} {c.get('ticker')}", line, c.get("ticker") in usd_tickers, out)
        t = c.get("ticker")
        if t in seen:
            out.append(problem(ERROR, f"{where}: {t} is listed twice (also at line {seen[t]}).", line,
                               "delete one of the two lines"))
        else:
            seen[t] = line

    if sc and isinstance(sc.get("universe"), list):
        start = L.key("scanner", "universe")
        pos = start
        for c in sc["universe"]:
            t = c.get("ticker") if isinstance(c, dict) else None
            line = L.text(rf'ticker\s*=\s*"{re.escape(str(t))}"', after=pos) if t else None
            pos = line or pos                                   # the next entry is further down
            candidate(c, "[scanner] universe", line or start)
    watch = rules.get("watchlist", [])
    if not isinstance(watch, list):
        out.append(problem(ERROR, "watchlist should be [[watchlist]] blocks.", L.table("watchlist"), None))
        watch = []
    after = None
    for c in watch:
        line = L.text(r"^\s*\[\[\s*watchlist\s*\]\]", after=after)
        after = line
        candidate(c, "[[watchlist]]", line)
        if isinstance(c, dict) and "added" in c:
            _check_type("date", c["added"], "[[watchlist]] added", line, out)

    roles = section("scanner.holding_roles") or {}
    for t, role in roles.items():
        line = L.key("scanner.holding_roles", t)
        _check_type("text", role, f"[scanner.holding_roles] {t}", line, out, ["growth", "diversifier"])
        if securities and t not in known:
            out.append(problem(WARN, f"[scanner.holding_roles] {t} isn't in [securities].", line,
                               "use the same Yahoo ticker as in [securities]"))
    for t, sec in (section("scanner.holding_sectors") or {}).items():
        _check_type("text", sec, f"[scanner.holding_sectors] {t}", L.key("scanner.holding_sectors", t), out)
    for role in ("growth", "diversifier"):
        w = section(f"scanner.weights.{role}")
        if w:
            for f, v in w.items():
                _check_type(num, v, f"[scanner.weights.{role}] {f}", L.key(f"scanner.weights.{role}", f), out)
            if all(isinstance(v, (int, float)) for v in w.values()) and abs(sum(w.values()) - 1) > 0.001:
                out.append(problem(ERROR, f"[scanner.weights.{role}] adds up to {sum(w.values()):g}, not 1.",
                                   L.table(f"scanner.weights.{role}"), "change the weights so they add up to 1"))

    if "results" in rules and isinstance(rules["results"], dict) and "compare_from" in rules["results"]:
        _check_type("date", rules["results"]["compare_from"], "compare_from", L.key("results", "compare_from"), out)
    costs = rules.get("costs")
    if isinstance(costs, dict):
        for k, v in costs.items():
            _check_type(num, v, f"[costs] {k}", L.key("costs", k), out)
    return out


def toml_syntax_problem(e, text):
    """tomllib's error -> plain English with the line and a likely fix."""
    msg = str(e)
    m = re.search(r"\(at line (\d+), column (\d+)\)", msg)
    line = int(m.group(1)) if m else None
    shown = text.splitlines()[line - 1].strip() if line and line <= len(text.splitlines()) else ""
    reason = re.sub(r"\s*\(at line.*\)", "", msg)
    low = reason.lower()
    if "expected newline" in low:
        fix = ("there is something extra after the value on this line - e.g. a number like 0.8.1, "
               "two values, or a note without # in front")
    elif "cannot overwrite" in low or "cannot declare" in low:
        fix = "the same setting or section appears twice - delete or rename one of them"
    elif "invalid value" in low or "invalid statement" in low:
        fix = ("text needs \"quotes\"; numbers, true/false and dates (2026-11-08) don't. "
               "Also check that every [ has a ] and every { has a }")
    elif "expected '='" in low:
        fix = "each setting needs the form name = value"
    elif "unclosed" in low or "illegal character" in low or "expected" in low:
        fix = "a quote, ] or } is missing or extra on this line"
    else:
        fix = "compare this line with demo_data\\rules.toml"
    return problem(ERROR, f"rules.toml can't be read: {reason}." + (f"  The line is:  {shown}" if shown else ""),
                   line, fix)


def check_rules(path=None):
    path = Path(path or settings.RULES_FILE)
    if not path.exists():
        return [problem(ERROR, f"No rules.toml in {path.parent}.", None,
                        "copy demo_data\\rules.toml there and edit it")]
    return check_rules_text(path.read_text(encoding="utf-8"))


def rules_report(problems):
    """The problems as one message (errors first)."""
    order = sorted(problems, key=lambda p: (p["level"] != ERROR, p.get("line") or 0))
    head = (f"rules.toml has {sum(p['level'] == ERROR for p in problems)} problem(s) to fix before anything can run:"
            if any(p["level"] == ERROR for p in problems) else "rules.toml - things worth checking:")
    return head + "\n" + "\n".join("  " + describe(p).replace("\n", "\n  ") for p in order)


# --- 2. The Nordnet exports -----------------------------------

def _transaction_dates(path):
    """(last booking day, number of rows) of a transaction export."""
    t = pd.read_csv(path, sep="\t", encoding="utf-16", usecols=["Bogføringsdag"])
    days = pd.to_datetime(t["Bogføringsdag"])
    return days.max(), len(t)


def pick_transactions_file():
    """The newest transaction export (each one holds the full history):
    the latest last booking day, then most rows, then newest file.
    Returns (path, [ignored older files])."""
    files = settings.data_files(settings.TRANSACTIONS_FILES)
    if not files:
        raise SystemExit(f"No '{settings.TRANSACTIONS_FILES}' file found in {settings.DATA_FOLDER}")
    if len(files) == 1:
        return files[0], []
    ranked = sorted(files, key=lambda f: (*_transaction_dates(f), f.stat().st_mtime), reverse=True)
    return ranked[0], ranked[1:]


def _history_period(path):
    d = pd.read_csv(path, sep="\t", encoding="utf-16")
    d.columns = [c.strip() for c in d.columns]
    dates = pd.to_datetime(d["Dato"])
    deposits = pd.to_numeric(d["Nettoindbetalinger (DKK)"], errors="coerce")
    frame = pd.DataFrame({"date": dates, "deposits": deposits}).sort_values("date")
    changed = frame[frame["deposits"].diff().abs() > 0.5]["date"]
    return dates.min(), dates.max(), (changed.max() if len(changed) else None)


def _holdings_export_date(path):
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", path.name)
    if m:
        return pd.Timestamp(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return pd.Timestamp(dt.date.fromtimestamp(path.stat().st_mtime))


def _holdings_by_isin(path, names):
    """Shares per ISIN from Nordnet's holdings export. Nordnet leaves the
    ISIN empty for some (e.g. US) shares: those rows are matched by name
    with the transaction export, and skipped if that fails."""
    h = pd.read_csv(path, sep="\t", encoding="utf-16", decimal=",", thousands=".")
    h.columns = [c.strip() for c in h.columns]
    by_name = {str(n).strip().lower(): i for i, n in names.items()}
    out = {}
    for _, row in h.iterrows():
        isin = row.get("ISIN")
        if not isinstance(isin, str) or not isin.strip():
            name = str(row.get("Navn", "")).strip().lower()
            isin = by_name.get(name) or next((i for n, i in by_name.items()
                                              if name and (n.startswith(name) or name.startswith(n))), None)
        if isin:
            out[isin] = out.get(isin, 0) + pd.to_numeric(row.get("Antal"), errors="coerce")
    return pd.Series(out, dtype=float).dropna()


def check_exports(rules, today=None):
    """Warnings and notes about the Nordnet files (list of problems)."""
    today = pd.Timestamp(today or dt.date.today())
    out = []
    try:
        tx_file, ignored = pick_transactions_file()
    except SystemExit as e:
        return [problem(ERROR, str(e))]
    for f in ignored:
        out.append(problem(INFO, f"older transaction export ignored: {f.name}"))

    # New holdings: ISINs in the transactions that aren't set up.
    from positions import read_transactions                    # here, to avoid a circular import
    t = read_transactions(tx_file)
    tx_last = t["date"].max()
    trades = t[t["Transaktionstype"].isin(["KØBT", "SOLGT"])]
    sign = trades["Transaktionstype"].map({"KØBT": 1, "SOLGT": -1})
    held = (trades["Antal"] * sign).groupby(trades["ISIN"]).sum()
    names = trades.groupby("ISIN")["Værdipapirer"].first()
    securities = rules.get("securities", {})
    s = rules.get("scanner", {})
    core = rules.get("portfolio", {}).get("core_ticker")
    for isin, shares in held.items():
        name = names.get(isin, isin)
        if isin not in securities:
            if shares > 1e-9:
                out.append(problem(WARN, f"New holding {name} ({isin}) isn't set up: add its Yahoo ticker, role "
                                   "and sector to rules.toml",
                                   fix=f'[securities] "{isin}" = {{ ticker = "...", currency = "..." }}, then '
                                       "the ticker under [scanner.holding_roles] and [scanner.holding_sectors]"))
            else:
                out.append(problem(WARN, f"{name} ({isin}, already sold) isn't in [securities], so its past "
                                   "results are left out", fix=f'add [securities] "{isin}" = {{ ticker = "...", '
                                   'currency = "..." }'))
        elif shares > 1e-9:
            ticker = securities[isin].get("ticker")
            missing = [k for k in ("holding_roles", "holding_sectors") if ticker not in s.get(k, {})]
            if ticker != core and missing:
                out.append(problem(WARN, f"New holding {name} ({ticker}) isn't fully set up: add its role and "
                                   "sector to rules.toml",
                                   fix=f"add {ticker} under " + " and ".join(f"[scanner.{k}]" for k in missing)))

    # Daily history exports: combined as before; only exact duplicates are odd.
    periods = {}
    last_deposit_change = None
    history_last = None
    for f in settings.data_files(settings.HISTORY_FILES):
        first, last, changed = _history_period(f)
        if (first, last) in periods:
            out.append(problem(WARN, f"Two Daglig udvikling exports cover exactly the same period "
                               f"({first:%Y-%m-%d} to {last:%Y-%m-%d}): {periods[(first, last)]} and {f.name}",
                               fix="delete one of them"))
        periods[(first, last)] = f.name
        history_last = last if history_last is None else max(history_last, last)
        if changed is not None:
            last_deposit_change = changed if last_deposit_change is None else max(last_deposit_change, changed)

    # Stale data.
    max_age = rules.get("alerts", {}).get("max_export_age_days", DEFAULT_MAX_EXPORT_AGE_DAYS)
    if history_last is not None and (today - history_last).days > max_age:
        out.append(problem(WARN, f"The Daglig udvikling export ends {history_last:%Y-%m-%d}, "
                           f"{(today - history_last).days} days ago (limit {max_age}).",
                           fix="export a new one: Min konto > Afkast > Daglig udvikling > Eksporter"))
    known = []
    if last_deposit_change is not None and last_deposit_change > tx_last:
        known.append((last_deposit_change, "Daglig udvikling shows a deposit or withdrawal"))
    if settings.data_files(settings.HOLDINGS_FILES):
        f = settings.newest_data_file(settings.HOLDINGS_FILES)
        when = _holdings_export_date(f)
        if when >= tx_last:
            now = _holdings_by_isin(f, names)
            rebuilt = held[held > 1e-9]
            differ = [i for i in set(now.index) | set(rebuilt.index)
                      if abs(now.get(i, 0) - rebuilt.get(i, 0)) > 1e-6]
            if differ:
                known.append((when, f"the holdings export ({f.name}) shows share counts the transactions "
                              f"don't explain ({', '.join(str(names.get(i, i)) for i in differ[:3])})"))
    for when, why in known:
        out.append(problem(WARN, f"The transaction export ends {tx_last:%Y-%m-%d}, but {why} on "
                           f"{when:%Y-%m-%d}: it's out of date.",
                           fix="export a new one: Transaktioner > Eksporter"))
    return out


def check_everything(today=None):
    """(rules problems, export problems). Export checks need usable rules."""
    rules_problems = check_rules()
    if any(p["level"] == ERROR for p in rules_problems):
        return rules_problems, []
    return rules_problems, check_exports(settings.load_rules(check=False), today)
