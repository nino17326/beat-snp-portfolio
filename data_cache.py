# ============================================================
#  data_cache.py  -  the ONLY place that talks to Yahoo Finance
#
#  Every script gets prices, exchange rates, company info, analyst
#  estimates and earnings dates from here. Everything is saved in
#  <data folder>\cache\ (outside git), so a run only downloads what is
#  missing:
#    prices     - only the days after the last saved one
#    info, estimates, earnings dates - at most once a day
#  If Yahoo fails or I'm offline, the saved data is used instead and
#  the run is marked offline (the dashboard shows "⚠️ prices from ...").
#
#  Prices are CLOSING prices of complete trading days only: a day's bar
#  is used only after that exchange has closed, never an intraday price.
# ============================================================

import atexit
import json
import os
import re
import threading
import time
from datetime import datetime, time as clock
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

import settings

CACHE = settings.DATA_FOLDER / "cache"
PRICES = CACHE / "prices"
STATUS_FILE = CACHE / "status.json"
HOME_TZ = ZoneInfo("Europe/Copenhagen")

BATCH_SIZE = 5              # tickers per Yahoo request
PAUSE_SECONDS = 1.0         # pause between batches / single requests
TIMEOUT_SECONDS = 10        # per price request
INFO_TIMEOUT_SECONDS = 20   # per info / estimates / calendar request
OVERLAP_DAYS = 7            # re-download this many saved days to spot splits/re-adjustments
TOLERANCE = 1e-4            # relative difference that counts as "history changed"

# When does a day's bar become final? (exchange time zone, close + a margin)
CLOSES = {"us": ("America/New_York", clock(16, 30)),
          "london": ("Europe/London", clock(17, 0)),
          "europe": ("Europe/Berlin", clock(18, 0)),
          "fx": ("Europe/London", clock(23, 15))}

# What this process did (printed at the end, see _report).
STATS = {"downloads": 0, "download_seconds": 0.0, "from_cache": 0, "failed": set()}
_failed_now = set()         # tickers that failed in THIS run: don't hammer Yahoo again
MAX_FAILURES_IN_A_ROW = 3   # then treat Yahoo as offline for the rest of this run
_streak = {"failures": 0}


def _yahoo_down():
    return _streak["failures"] >= MAX_FAILURES_IN_A_ROW


def _ok():
    _streak["failures"] = 0


def _fail():
    _streak["failures"] += 1
    if _streak["failures"] == MAX_FAILURES_IN_A_ROW:
        print(f"  ⚠️ Yahoo failed {MAX_FAILURES_IN_A_ROW} times in a row - using saved data for the rest of this run")


# --- Small helpers --------------------------------------------

def _safe(ticker):
    return re.sub(r"[^A-Za-z0-9._-]", "_", ticker)


def _write(path, text):
    """Write a file atomically (a crash never leaves half a file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _market(ticker):
    if ticker.endswith("=X"):
        return "fx"
    if ticker.startswith("^") or "." not in ticker:
        return "us"
    return "london" if ticker.endswith(".L") else "europe"


def last_complete_day(ticker, now=None):
    """The newest date whose closing price is final for this ticker's
    exchange (a weekday; holidays are found out by asking Yahoo)."""
    tz, close_at = CLOSES[_market(ticker)]
    local = (now or datetime.now(HOME_TZ)).astimezone(ZoneInfo(tz))
    day = pd.Timestamp(local.date())
    if local.time() < close_at:
        day -= pd.Timedelta(days=1)
    while day.dayofweek >= 5:                     # Saturday/Sunday: back to Friday
        day -= pd.Timedelta(days=1)
    return day


def _call_with_timeout(fn, seconds=INFO_TIMEOUT_SECONDS):
    """Run fn() in a background thread; give up after `seconds`."""
    box = {}

    def run():
        try:
            box["value"] = fn()
        except Exception as e:                    # noqa: BLE001 - any Yahoo error = failure
            box["error"] = e

    t = threading.Thread(target=run, daemon=True)  # daemon: never blocks the script from ending
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise TimeoutError(f"no answer within {seconds} s")
    if "error" in box:
        raise box["error"]
    return box["value"]


# --- Status (read by the dashboard) ---------------------------

def _set_status(ticker, last_close, offline):
    status = _read_json(STATUS_FILE, {})
    status[ticker] = {"last_close": f"{last_close:%Y-%m-%d}" if last_close is not None else None,
                      "offline": bool(offline), "checked": datetime.now(HOME_TZ).isoformat(timespec="seconds")}
    _write(STATUS_FILE, json.dumps(status, indent=1, sort_keys=True))


def price_status(max_age_hours=24):
    """What the dashboard shows: (label, offline). Uses tickers checked in
    the last `max_age_hours`; offline if any of them couldn't be updated."""
    status = _read_json(STATUS_FILE, {})
    cutoff = datetime.now(HOME_TZ) - pd.Timedelta(hours=max_age_hours)
    recent = [s for s in status.values()
              if s.get("last_close") and datetime.fromisoformat(s["checked"]) >= cutoff]
    if not recent:
        return None, False
    offline = [s for s in recent if s["offline"]]
    if offline:
        oldest = min(s["last_close"] for s in offline)
        return f"⚠️ prices from {oldest} (offline)", True
    newest = max(s["last_close"] for s in recent)
    return f"Prices: closes of {newest} (last complete trading day, not intraday)", False


# --- Prices ---------------------------------------------------

def _price_file(ticker):
    return PRICES / f"{_safe(ticker)}.csv"


def _meta_file(ticker):
    return PRICES / f"{_safe(ticker)}.json"


def _load_prices(ticker):
    path = _price_file(ticker)
    if not path.exists():
        return pd.DataFrame(columns=["close", "adj_close"], dtype=float)
    table = pd.read_csv(path, index_col="date", parse_dates=["date"])
    table.index = pd.DatetimeIndex(table.index).astype("datetime64[ns]")   # one date type everywhere
    return table.astype(float)


def _save_prices(ticker, table):
    table = table[~table.index.duplicated(keep="last")].sort_index()
    _write(_price_file(ticker), table.to_csv(index_label="date"))     # full precision: reads back identical


def _clean(frame, ticker):
    """Yahoo's table -> date-indexed close/adj_close of COMPLETE days only."""
    if frame is None or frame.empty or "Close" not in frame:
        return pd.DataFrame(columns=["close", "adj_close"], dtype=float)
    frame = frame.dropna(subset=["Close"])
    index = frame.index.tz_localize(None) if frame.index.tz is not None else frame.index
    out = pd.DataFrame({"close": frame["Close"].values,
                        "adj_close": frame.get("Adj Close", frame["Close"]).values},
                       index=pd.DatetimeIndex(index).normalize().astype("datetime64[ns]"))
    out.index.name = "date"
    return out[out.index <= last_complete_day(ticker)]           # drop today's unfinished bar


def _history(ticker, start):
    """One ticker from Yahoo, from `start` to now (unadjusted + adjusted)."""
    t0 = time.time()
    frame = yf.Ticker(ticker).history(start=start, end=pd.Timestamp.today() + pd.Timedelta(days=1),
                                      auto_adjust=False, timeout=TIMEOUT_SECONDS)
    STATS["downloads"] += 1
    STATS["download_seconds"] += time.time() - t0
    return _clean(frame, ticker)


def _needs(ticker, start, table, meta):
    """Where a download must start (or None if the cache is complete)."""
    target = last_complete_day(ticker)
    if table.empty:
        return start
    if start < table.index[0] - pd.Timedelta(days=5) and start < pd.Timestamp(meta.get("asked_from", "2100-01-01")):
        return start                                              # need older history
    if table.index[-1] >= target or meta.get("checked_for") == f"{target:%Y-%m-%d}":
        return None                                               # up to date (or holiday: already asked)
    return table.index[-1] - pd.Timedelta(days=OVERLAP_DAYS)


def _merge(ticker, start, table, fresh, meta):
    """Add freshly downloaded days to the saved table. If the overlapping
    days differ (a split or a dividend re-adjustment), download it all again."""
    overlap = table.index.intersection(fresh.index)
    if len(overlap) and len(table):
        a, b = table.loc[overlap], fresh.loc[overlap]
        changed = ((a - b).abs() / b.abs().clip(lower=1e-12) > TOLERANCE).any().any()
        if changed:
            print(f"  {ticker}: history changed (split or dividend adjustment) - downloading it all again")
            first = min(table.index[0], start)
            fresh = _history(ticker, first)
            table = fresh.iloc[0:0]
    merged = pd.concat([table, fresh])
    _save_prices(ticker, merged)
    meta["asked_from"] = f"{min(start, pd.Timestamp(meta.get('asked_from', '2100-01-01'))):%Y-%m-%d}"
    meta["checked_for"] = f"{last_complete_day(ticker):%Y-%m-%d}"
    _write(_meta_file(ticker), json.dumps(meta))
    return merged[~merged.index.duplicated(keep="last")].sort_index()


def prefetch(tickers, start):
    """Bring several tickers up to date in small batches (one Yahoo request
    per batch, a pause between batches). Later closes() calls then come
    straight from the cache."""
    start = pd.Timestamp(start)
    todo = {}
    for ticker in dict.fromkeys(tickers):                         # unique, order kept
        table, meta = _load_prices(ticker), _read_json(_meta_file(ticker), {})
        begin = _needs(ticker, start, table, meta)
        if begin is not None and ticker not in _failed_now and not _yahoo_down():
            todo[ticker] = (begin, table, meta)
    names = list(todo)
    for i in range(0, len(names), BATCH_SIZE):
        batch = names[i:i + BATCH_SIZE]
        begin = min(todo[t][0] for t in batch)
        t0 = time.time()
        try:
            frame = yf.download(batch, start=begin, end=pd.Timestamp.today() + pd.Timedelta(days=1),
                                auto_adjust=False, group_by="ticker", progress=False, threads=True,
                                timeout=TIMEOUT_SECONDS, multi_level_index=True)
        except Exception:                                         # noqa: BLE001 - fall back to one by one
            frame = None
        STATS["downloads"] += 1
        STATS["download_seconds"] += time.time() - t0
        print(f"  Downloaded {len(batch)} tickers in one request ({time.time() - t0:.1f} s): {', '.join(batch)}")
        for ticker in batch:
            _, table, meta = todo[ticker]
            part = None
            if frame is not None and not frame.empty and ticker in frame.columns.get_level_values(0):
                part = frame[ticker]
            fresh = _clean(part, ticker)
            if fresh.empty:
                continue                                          # closes() will try it on its own
            _merge(ticker, start, table, fresh, meta)
            _set_status(ticker, _load_prices(ticker).index[-1], offline=False)
        if i + BATCH_SIZE < len(names):
            time.sleep(PAUSE_SECONDS)


def closes(ticker, start, end=None, adjusted=False):
    """Daily closing prices (complete trading days only) from `start` up to
    but not including `end`. adjusted=True = dividend-adjusted.
    Downloads only what's missing; offline -> the saved prices."""
    start = pd.Timestamp(start)
    table, meta = _load_prices(ticker), _read_json(_meta_file(ticker), {})
    begin = _needs(ticker, start, table, meta)
    if begin is None:
        STATS["from_cache"] += 1
    elif ticker in _failed_now or _yahoo_down():
        if not table.empty:                                       # already failed this run: use the cache
            _set_status(ticker, table.index[-1], offline=True)
            STATS["failed"].add(ticker)
        else:
            raise LookupError(f"No prices for {ticker}: Yahoo is unreachable and nothing is saved yet.")
    else:
        try:
            fresh = _history(ticker, begin)
            # Every request overlaps saved days, so an empty answer means
            # Yahoo failed (yfinance hides network errors), not a holiday.
            if fresh.empty:
                raise ConnectionError("no prices returned")
            print(f"  Downloaded {ticker} from {begin:%Y-%m-%d}")
            _ok()
            table = _merge(ticker, start, table, fresh, meta)
            _set_status(ticker, table.index[-1], offline=False)
            time.sleep(PAUSE_SECONDS)
        except Exception as e:                                    # noqa: BLE001 - offline / Yahoo error
            _fail()
            _failed_now.add(ticker)
            STATS["failed"].add(ticker)
            if table.empty:
                raise LookupError(f"No prices for {ticker} and nothing saved yet ({type(e).__name__}). "
                                  "Check the ticker and your internet connection.") from e
            print(f"  ⚠️ {ticker}: Yahoo failed ({type(e).__name__}) - using saved prices up to "
                  f"{table.index[-1]:%Y-%m-%d}")
            _set_status(ticker, table.index[-1], offline=True)
    column = table["adj_close" if adjusted else "close"]
    column = column.loc[start:]
    if end is not None:
        column = column[column.index < pd.Timestamp(end)]
    column.name = "Close"
    return column


# --- Company info, estimates, earnings dates (once a day) ----

def _daily(kind, ticker, fetch, encode=lambda v: v, decode=lambda v: v):
    """Cached once per calendar day; stale copy if Yahoo fails."""
    path = CACHE / kind / f"{_safe(ticker)}.json"
    saved = _read_json(path)
    today = f"{datetime.now(HOME_TZ):%Y-%m-%d}"
    if saved and saved.get("fetched") == today:
        STATS["from_cache"] += 1
        return decode(saved["data"])
    if (kind, ticker) not in _failed_now and not _yahoo_down():
        t0 = time.time()
        try:
            value = _call_with_timeout(fetch)
            _ok()
            STATS["downloads"] += 1
            STATS["download_seconds"] += time.time() - t0
            _write(path, json.dumps({"fetched": today, "data": encode(value)}, default=str))
            time.sleep(PAUSE_SECONDS / 2)
            return value
        except Exception as e:                                    # noqa: BLE001
            _fail()
            STATS["download_seconds"] += time.time() - t0
            _failed_now.add((kind, ticker))
            STATS["failed"].add(f"{ticker} ({kind})")
            print(f"  ⚠️ {ticker} {kind}: Yahoo failed ({type(e).__name__})"
                  + (f" - using the copy from {saved['fetched']}" if saved else ""))
    return decode(saved["data"]) if saved else None


def info(ticker):
    """Yahoo's company facts (dict), refreshed once a day. {} if unknown."""
    def fetch():
        facts = yf.Ticker(ticker).info
        if not facts or len(facts) < 3:                 # an empty answer is a failure, not "no facts"
            raise ConnectionError("empty answer")
        return facts

    return _daily("info", ticker, fetch) or {}


def _frame_to(f):
    return None if f is None else {"index": [str(i) for i in f.index], "columns": [str(c) for c in f.columns],
                                   "data": f.astype(object).where(f.notna(), None).values.tolist()}


def _frame_from(d):
    return None if d is None else pd.DataFrame(d["data"], index=d["index"], columns=d["columns"]).apply(
        pd.to_numeric, errors="coerce")


def estimates(ticker):
    """(eps_trend, eps_revisions) tables, refreshed once a day; (None, None) if unknown."""
    def fetch():
        t = yf.Ticker(ticker)
        return [t.eps_trend, t.eps_revisions]

    value = _daily("estimates", ticker, fetch,
                   encode=lambda v: [_frame_to(f) for f in v],
                   decode=lambda v: [_frame_from(f) for f in v])
    if value is None:
        return None, None
    trend, revisions = value
    return (_frame_from(_frame_to(trend)) if isinstance(trend, pd.DataFrame) else trend,
            _frame_from(_frame_to(revisions)) if isinstance(revisions, pd.DataFrame) else revisions)


def earnings_dates(ticker):
    """Upcoming earnings dates Yahoo knows (list of Timestamps), once a day."""
    dates = _daily("calendar", ticker,
                   lambda: [str(d) for d in (yf.Ticker(ticker).calendar or {}).get("Earnings Date") or []])
    return [pd.Timestamp(d) for d in (dates or [])]


# --- End-of-run summary ---------------------------------------

def _report():
    if STATS["downloads"] or STATS["from_cache"]:
        line = (f"[data cache] {STATS['downloads']} Yahoo requests ({STATS['download_seconds']:.1f} s), "
                f"{STATS['from_cache']} answered from the cache")
        if STATS["failed"]:
            line += f" · ⚠️ offline/failed: {', '.join(sorted(map(str, STATS['failed'])))}"
        print(line)


atexit.register(_report)
