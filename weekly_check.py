# ============================================================
#  weekly_check.py  -  my Monday check-up
#
#  Reads my rules from rules.toml, rebuilds my holdings from the
#  Nordnet transaction file (via positions.py), downloads prices up
#  to the latest close, and checks:
#    1. position weights against my limits
#    2. each stock's 1-week and 4-week return vs. CSPX (in DKK)
#    3. price levels (add zones / ladder steps) that have been reached
#    4. my story checks: too many red, or not updated for too long
#  Prints a short report and saves it in output\weekly\ in the data
#  folder (see settings.py).
# ============================================================

import contextlib
import io
import json
import sys
import tomllib                       # built into Python 3.11+: reads .toml files
from datetime import date

import pandas as pd
import data_cache                       # Yahoo data, saved locally (see data_cache.py)

from portfolio import danish
from shadow_portfolio import download_close, signed
from shadow_portfolio import read_export, combine
from positions import load_account, SECURITIES, OUTPUT_FOLDER
from regime import current_regime, MARKETS
from risk import summary_lines as risk_summary, risk_numbers, semis_risk_after
from settings import RULES_FILE, HISTORY_FILES, data_files     # rules.toml is in the data folder
from settings import write_text_safely                         # crash-safe writes (temp file, then rename)

WEEKLY_FOLDER = OUTPUT_FOLDER / "weekly"
OK, WARN, INFO = "✅", "⚠️", "ℹ️"   # INFO = no action attached, never counted


def item(ok, text):
    """One report line with a tick or a warning sign."""
    return f"- {OK if ok else WARN} {text}"


def pct(x):
    return danish(x, 1) + "%"


# --- 1. Weights vs. limits ------------------------------------

def portfolio_weights(rules, values, cash):
    """% per ticker, of holdings (or holdings + cash if include_cash).
    Also used by scanner.py for 'what if I bought X' checks."""
    base = values.sum() + (cash if rules["portfolio"]["include_cash"] else 0)
    return values / base * 100


def single_stock_weights(rules, weight, extra_funds=()):
    """Weights of single stocks only: not the core, not funds/ETFs."""
    p = rules["portfolio"]
    skip = {p["core_ticker"], *p["funds"], *extra_funds}
    return weight[[t for t in weight.index if t not in skip]]


def group_weights(rules, weight):
    """{group: (weight %, max %)} for every [groups.X] in rules.toml."""
    return {name: (weight.reindex(g["tickers"]).fillna(0).sum(), g["max_pct"])
            for name, g in rules.get("groups", {}).items()}


def check_weights(rules, values, cash):
    p = rules["portfolio"]
    weight = portfolio_weights(rules, values, cash)   # % of holdings, per ticker
    core = p["core_ticker"]
    lines = ["## Limits"]

    w = weight.get(core, 0)
    lines.append(item(w >= p["core_min_pct"], f"Core {core}: {pct(w)} (min {p['core_min_pct']}%)"))

    sats = weight.drop(core, errors="ignore").sum()
    lines.append(item(sats <= p["satellites_max_pct"],
                      f"Satellites total: {pct(sats)} (max {p['satellites_max_pct']}%)"))

    for ticker, w in single_stock_weights(rules, weight).sort_values(ascending=False).items():
        lines.append(item(w <= p["single_stock_max_pct"],
                          f"{ticker}: {pct(w)} (max {p['single_stock_max_pct']}%)"))

    for group, (w, limit) in group_weights(rules, weight).items():
        lines.append(item(w <= limit, f"{group.capitalize()}: {pct(w)} (max {limit}%)"))
    return lines


# --- 2. Returns vs. CSPX --------------------------------------

def relative_returns(rules, prices_by_ticker, held, today):
    """1-week and 4-week returns vs. CSPX, as numbers.
    Returns (CSPX 1w %, CSPX 4w %, {ticker: {...}})."""
    a = rules["alerts"]
    core = rules["portfolio"]["core_ticker"]

    def ret(ticker, days):
        series = prices_by_ticker[ticker]
        return (series[today] / series[today - pd.Timedelta(days=days)] - 1) * 100

    core_1w, core_4w = ret(core, 7), ret(core, 28)
    rows = {}
    for ticker in held:
        if ticker == core:
            continue
        r1, r4 = ret(ticker, 7), ret(ticker, 28)
        gap_1w, gap_4w = r1 - core_1w, r4 - core_4w      # in percentage points
        rows[ticker] = {"1w": r1, "4w": r4, "gap_1w": gap_1w, "gap_4w": gap_4w,
                        "lagging": gap_1w < -a["week_lag_points"] or gap_4w < -a["four_week_lag_points"]}
    return core_1w, core_4w, rows


def check_returns(relative):
    core_1w, core_4w, rows = relative
    lines = [f"## Returns vs. CSPX (DKK)  -  CSPX: 1w {signed(core_1w, 1)}%, 4w {signed(core_4w, 1)}%"]
    for ticker, r in rows.items():
        lines.append(item(not r["lagging"], f"{ticker}: 1w {signed(r['1w'], 1)}% ({signed(r['gap_1w'], 1)} pts), "
                                            f"4w {signed(r['4w'], 1)}% ({signed(r['gap_4w'], 1)} pts)"))
    return lines


# --- 3. Price levels ------------------------------------------

def latest_close(ticker, today):
    """Latest closing price in the stock's own currency, and its date."""
    series = download_close(ticker, today - pd.Timedelta(days=10), today + pd.Timedelta(days=1),
                            adjust_for_dividends=False)
    return series.iloc[-1], series.index[-1]


def price_levels(rules, today):
    """Every price level in rules.toml with today's price, as numbers.
    A level whose label starts with 'Add zone' is an add zone."""
    currency_of = {ticker: currency for ticker, currency in SECURITIES.values()}
    out = []
    for ticker, stock in rules["stocks"].items():
        if not stock.get("levels"):
            continue
        price, _ = latest_close(ticker, today)
        for level in stock["levels"]:
            out.append({"ticker": ticker, "name": stock["name"], "label": level["label"],
                        "target": level["at_or_below"], "price": float(price),
                        "currency": currency_of.get(ticker, ""),
                        "reached": bool(price <= level["at_or_below"]),
                        "add_zone": level["label"].lower().startswith("add zone")})
    return out


def check_levels(levels):
    lines = ["## Price levels"]
    for l in levels:
        start = f"{l['name']} {danish(l['price'])} {l['currency']} - {l['label']}"
        if l["reached"]:
            lines.append(item(False, f"{start} ({danish(l['target'], 0)}) REACHED"))
        else:
            away = (l["target"] / l["price"] - 1) * 100
            lines.append(item(True, f"{start} at {danish(l['target'], 0)} not reached ({signed(away, 1)}% away)"))
    return lines


# --- 4. Story checks + valuation ------------------------------

def valuation(ticker):
    """Trailing P/E, forward P/E and PEG from Yahoo Finance, as text.

    trailing P/E = price / last 12 months' earnings per share
    forward P/E  = price / expected earnings for the next 12 months
    PEG          = P/E divided by the expected earnings growth rate
                   (around 1 = growth is "fairly" priced, higher = pricier)
    """
    info = data_cache.info(ticker)             # facts about the stock, refreshed once a day ({} if offline)

    def show(key):
        value = info.get(key)
        return danish(value, 1) if isinstance(value, (int, float)) else "n/a"

    # Yahoo calls the PEG "trailingPegRatio" (older versions: "pegRatio").
    peg_key = "trailingPegRatio" if info.get("trailingPegRatio") is not None else "pegRatio"
    return f"P/E {show('trailingPE')} · fwd P/E {show('forwardPE')} · PEG {show(peg_key)}"


def estimate_changes(ticker):
    """Analysts' consensus EPS estimate changes as numbers (None = unknown).
    '0y' = the current fiscal year, '+1y' = next fiscal year.
    Also used by scanner.py for its 'revisions' score."""
    trend, revisions = data_cache.estimates(ticker)      # refreshed once a day
    if trend is None:
        return None

    def change(period, days):
        """% change of the estimate over `days` days, or None if unknown."""
        try:
            now = trend.loc[period, "current"]
            then = trend.loc[period, f"{days}daysAgo"]
        except (KeyError, TypeError, AttributeError):
            return None
        # Yahoo writes 0 for "missing"; a % change from a loss is meaningless too.
        if pd.isna(now) or pd.isna(then) or then <= 0 or now == 0:
            return None
        return (now / then - 1) * 100

    try:
        up = int(revisions.loc["+1y", "upLast30days"])
        down = int(revisions.loc["+1y", "downLast30days"])
    except (KeyError, TypeError, ValueError, AttributeError):
        up = down = None

    def current(period):
        """Today's consensus EPS for a fiscal year (None if missing / 0)."""
        try:
            value = trend.loc[period, "current"]
        except (KeyError, TypeError, AttributeError):
            return None
        return None if pd.isna(value) or value == 0 else float(value)

    return {"this_30": change("0y", 30), "this_90": change("0y", 90),
            "next_30": change("+1y", 30), "next_90": change("+1y", 90),
            "up": up, "down": down,
            "eps_this_fy": current("0y"), "eps_next_fy": current("+1y")}


def estimate_revisions(ticker, cut_limit):
    """One report line about EPS estimate changes: ℹ️, or ⚠️ if next
    year's estimate was cut by more than cut_limit % over 90 days."""
    e = estimate_changes(ticker)
    if e is None:
        return f"    - {INFO} Estimates: n/a (Yahoo didn't answer)"

    def show(x):
        return "n/a" if x is None else signed(x, 1) + "%"

    votes = "n/a" if e["up"] is None else f"{e['up']} up / {e['down']} down"
    warn = e["next_90"] is not None and e["next_90"] < -cut_limit
    return (f"    - {WARN if warn else INFO} EPS est. 30d/90d: this FY {show(e['this_30'])}/"
            f"{show(e['this_90'])} · next FY {show(e['next_30'])}/{show(e['next_90'])}"
            f" · revisions 30d: {votes}")


def story_status(rules, today):
    """Story-check counts per stock, as numbers: {ticker: {...}}."""
    a = rules["alerts"]
    out = {}
    for ticker, stock in rules["stocks"].items():
        checks = stock.get("checks", {})
        statuses = [c["status"].strip().lower() for c in checks.values()]
        s = {"name": stock["name"], "total": len(statuses), "red": statuses.count("red"),
             "green": statuses.count("green"), "unknown": statuses.count("unknown"),
             "never": 0, "stale": 0}
        s["typos"] = s["total"] - s["red"] - s["green"] - s["unknown"]
        for c in checks.values():
            checked = c["checked"]
            # A real date in the file becomes a Python date; "never" stays text.
            if not isinstance(checked, date):
                s["never"] += 1
            elif (today.date() - checked).days > a["stale_after_days"]:
                s["stale"] += 1
        s["all_green"] = s["total"] > 0 and s["green"] == s["total"]
        out[ticker] = s
    return out


def check_stories(rules, stories):
    a = rules["alerts"]
    lines = ["## Story checks"]
    for ticker, s in stories.items():
        red, never, stale, typos = s["red"], s["never"], s["stale"], s["typos"]
        text = f"{s['name']}: {red} red, {s['green']} green, {s['unknown']} unknown"
        if never:
            text += f" - {never} never checked"
        if stale:
            text += f" - {stale} not updated in {a['stale_after_days']}+ days"
        if typos:
            text += f" - {typos} status typo(s): use green/red/unknown"
        ok = red < a["max_red_checks"] and not never and not stale and not typos
        lines.append(item(ok, text))
        lines.append(f"    - Valuation (Yahoo): {valuation(ticker)}")   # info only, no tick
        lines.append(estimate_revisions(ticker, a.get("estimate_cut_pct", 5)))
    return lines


# --- Scanner summary (from the last scanner.py run) -----------

def check_scanner(today):
    """Top 3 per role, my weakest satellite per role, and the one-in-one-out
    verdict - read from the summary scanner.py saves. All ℹ️."""
    path = OUTPUT_FOLDER / "scanner" / "latest.json"
    lines = ["## Scanner"]
    if not path.exists():
        return lines + [f"- {INFO} No scanner results yet - run: py scanner.py"]
    s = json.loads(path.read_text(encoding="utf-8"))
    age = (today - pd.Timestamp(s["date"])).days
    lines[0] += f" (run {s['date']})"
    if age > 7:
        lines.append(f"- {INFO} Results are {age} days old - run: py scanner.py")
    for role, r in s["roles"].items():
        top = ", ".join(f"{tk} {score}" for tk, score in r["top"]) or "none eligible"
        weak = f"{r['weakest'][0]} {r['weakest'][1]}" if r["weakest"] else "n/a"
        lines.append(f"- {INFO} {role.capitalize()}: top {top} · my weakest {weak}")
    for v in s["verdicts"]:
        lines.append(f"- {INFO} {v}")
    # Watchlist names: gates and fit only - never ranked or scored.
    for w in s.get("watchlist", []):
        problems = f" ({'; '.join(w['problems'])})" if w.get("problems") else ""
        lines.append(f"- {INFO} Watchlist {w['ticker']} ({w['role']}): gates {w['gates']} "
                     f"{w['verdict']}{problems} · fit {w['fit']} {w['fit_reason']}")
    return lines


# --- 5. Calendar ----------------------------------------------

def next_earnings(ticker):
    """Earnings dates Yahoo knows for a stock (funds have none)."""
    return data_cache.earnings_dates(ticker)     # once a day; [] if unknown / offline


def upcoming_events(rules, held, today):
    """Earnings of my holdings + my own macro dates in the next days_ahead
    days. Returns (days_ahead, [(date, text, ticker or None), ...])."""
    cal = rules.get("calendar", {})
    days = cal.get("days_ahead", 14)
    until = today + pd.Timedelta(days=days)

    events = []
    for ticker in held:
        for d in next_earnings(ticker):
            if today <= d <= until:        # ignore past dates Yahoo sometimes returns
                events.append((d, f"{ticker} earnings", ticker))
    for e in cal.get("events", []):
        d = pd.Timestamp(e["date"])
        if today <= d <= until:
            events.append((d, e["label"], None))
    return days, sorted(events, key=lambda e: e[0])     # earliest first


def check_calendar(upcoming):
    days, events = upcoming
    lines = [f"## Calendar (next {days} days)"]
    if not events:
        lines.append(f"- {INFO} Nothing scheduled")
    for d, text, _ in events:
        lines.append(f"- {INFO} {d:%a %d %b}: {text}")
    return lines


# --- 6. ASK tax and deposit ceiling ---------------------------

def ask_figures(rules):
    """This year's ASK gain, estimated tax, cash buffer and deposit room,
    from the Daglig udvikling files. None if there's no [ask] section."""
    if "ask" not in rules:
        return None
    a = rules["ask"]
    files = data_files(HISTORY_FILES)
    history = combine([read_export(f) for f in files]).set_index("date")

    last_date = history.index[-1]
    year = last_date.year
    now = history.iloc[-1]
    # The last row from LAST year = the value on 31 December.
    before = history[history.index < pd.Timestamp(year=year, month=1, day=1)]
    start_value = before["value"].iloc[-1] if len(before) else 0.0
    start_deposits = before["deposits"].iloc[-1] if len(before) else 0.0

    deposits_this_year = now["deposits"] - start_deposits
    gain = now["value"] - start_value - deposits_this_year
    tax = max(gain, 0) * a["tax_rate"]
    ceiling = a["ceilings"].get(str(year))
    return {"year": year, "last_date": last_date, "value": now["value"], "start_value": start_value,
            "deposits_this_year": deposits_this_year, "gain": gain, "tax": tax,
            "buffer": a["cash_buffer"] * tax, "cash_buffer": a["cash_buffer"], "ceiling": ceiling,
            "room": None if ceiling is None else ceiling - start_value - deposits_this_year}


def check_ask(cash_now, f):
    if f is None:
        return ["## ASK", item(False, "No [ask] section in rules.toml - add it back to get the tax check")]
    lines = [f"## ASK {f['year']} (Nordnet data up to {f['last_date']:%d-%m-%Y})"]
    lines.append(f"- {INFO} Gain this year {danish(f['gain'], 0)} DKK "
                 f"= value {danish(f['value'], 0)} - 31 Dec {danish(f['start_value'], 0)} "
                 f"- deposits {danish(f['deposits_this_year'], 0)}")
    lines.append(item(cash_now >= f["buffer"],
                      f"Estimated tax {danish(f['tax'], 0)} DKK; cash {danish(cash_now, 0)} DKK "
                      f"(want >= {danish(f['buffer'], 0)}, i.e. {f['cash_buffer']} x tax)"))
    if f["ceiling"] is None:
        lines.append(item(False, f"No deposit ceiling for {f['year']} in rules.toml [ask] - add it"))
    else:
        lines.append(item(f["room"] >= 0, f"Deposit room left {danish(f['room'], 0)} DKK "
                                          f"(ceiling {danish(f['ceiling'], 0)} - 31 Dec value - deposits)"))
    return lines


# --- 7. Actions: one per holding, from my own rules -----------
# Priority: SELL > TRIM > REVIEW > ADD > HOLD. Uses only rules.toml and
# numbers the checks above already produced. Rule output, not advice.

ACTIONS_NOTE = "Rule output, not advice: I decide, and I log every decision."


def best_candidate(rules, scan, role, exclude=()):
    """The best scanner candidate of a role that passed the gates and the
    fit check AND scores at least min_candidate_score (or None)."""
    if not scan or role is None:
        return None
    minimum = rules["scanner"]["min_candidate_score"]
    ok = [c for c in scan.get("roles", {}).get(role, {}).get("eligible", [])
          if c["score"] >= minimum and c["ticker"] not in exclude]
    return max(ok, key=lambda c: c["score"]) if ok else None


def sell_down_to(value, total, limit_pct):
    """DKK to sell so a position worth `value` of a `total` falls to limit_pct.
    (Selling x makes it (value - x) / (total - x) of what is left.)"""
    m = limit_pct / 100
    return max(0.0, (value - m * total) / (1 - m))


def proceeds_line(rules, values, cash, ticker, amount, scan):
    """Where the money from a SELL/TRIM would go, by my rules - one line,
    never more than one name, never a buy instruction."""
    p = rules["portfolio"]
    after = values.copy()
    after[ticker] -= amount
    core_after = portfolio_weights(rules, after[after > 0], cash + amount).get(p["core_ticker"], 0)
    if core_after < p["core_min_pct"]:
        return f"→ CSPX (refill core to {p['core_min_pct']}%)"
    role = rules["scanner"].get("holding_roles", {}).get(ticker)
    cand = best_candidate(rules, scan, role, exclude=set(values.index))
    if cand:
        return f"→ satellite slot: review on Sunday: {cand['ticker']} (score {danish(cand['score'], 2)})"
    return "→ CSPX for now (no candidate good enough; slot stays open)"


def decide_actions(rules, values, cash, relative, levels, stories, upcoming, ask, risk_nums, scan, today):
    p, a = rules["portfolio"], rules["alerts"]
    core = p["core_ticker"]
    weight = portfolio_weights(rules, values, cash)
    singles = single_stock_weights(rules, weight)
    semis_tickers = rules["groups"]["semiconductors"]["tickers"]
    semis_money, semis_money_max = group_weights(rules, weight)["semiconductors"]
    semis_risk, semis_risk_max = risk_nums["semis_risk"], rules["risk"]["semis_max_risk_share"]
    returns = risk_nums["returns"]
    satellites_pct = weight.drop(core, errors="ignore").sum()
    available = max(0.0, cash - (ask["buffer"] if ask else 0.0))      # cash beyond the ASK tax buffer
    swaps_out = {s["out"]: s["in"] for s in (scan or {}).get("swaps", [])}
    _, events = upcoming

    # If semis break a limit, trim the semis holding that causes the most swings.
    semis_held = [t for t in semis_tickers if t in values.index]
    semis_trim = None
    if semis_held and (semis_money > semis_money_max or semis_risk > semis_risk_max):
        semis_trim = max(semis_held, key=lambda t: risk_nums["table"].loc[t, "risk_%"])

    actions = []
    for tk in [t for t in values.index if t != core]:
        story = stories.get(tk)
        name = story["name"] if story else tk
        act, reason, sale = None, "", 0.0

        # SELL: the kill rule.
        if story and story["red"] >= a["max_red_checks"]:
            act, sale = "SELL", values[tk]
            reason = f"{story['red']} story checks red (kill rule: {a['max_red_checks']} or more)"

        # TRIM: strictly above a limit.
        elif tk in singles.index and singles[tk] > p["single_stock_max_pct"]:
            act = "TRIM"
            sale = sell_down_to(values[tk], values.sum(), p["single_stock_max_pct"])
            reason = f"{danish(singles[tk], 1)}% of holdings > {p['single_stock_max_pct']}% single-stock limit"
        elif tk == semis_trim:
            act = "TRIM"
            if semis_money > semis_money_max:
                semis_value = values.reindex(semis_held).sum()
                sale = min(values[tk], sell_down_to(semis_value, values.sum(), semis_money_max))
                reason = f"semis {danish(semis_money, 1)}% of money > {semis_money_max}%"
            else:
                # Risk breach only: sell in 5% steps until semis risk is within the limit.
                for step in range(1, 21):
                    sale = values[tk] * step * 0.05
                    after = values.copy()
                    after[tk] -= sale
                    if semis_risk_after(rules, after, returns) <= semis_risk_max:
                        break
                reason = f"semis cause {danish(semis_risk, 1)}% of swings > {semis_risk_max}%"
            reason += f"; {tk} adds the most semis risk"

        else:
            # REVIEW: something to look at, not a trade.
            looks = []
            for l in levels:
                if l["ticker"] == tk and l["reached"] and not l["add_zone"]:
                    looks.append(f"{l['label']} reached ({danish(l['price'])} ≤ {danish(l['target'], 0)} {l['currency']})")
            if story and (story["stale"] or story["never"]):
                looks.append(f"{story['stale'] + story['never']} story check(s) older than "
                             f"{a['stale_after_days']} days or never done")
            for d, _, t in events:
                if t == tk:
                    looks.append(f"earnings {d:%d %b}: update story checks after {d:%d %b}")
            if tk in swaps_out:
                looks.append(f"scanner: swap worth considering ({swaps_out[tk]} in) - review on Sunday, not a trade")
            if looks:
                act, reason = "REVIEW", "; ".join(looks)

        # ADD: price in an add zone, story all green, and my rules allow it.
        if act is None:
            zone = [l for l in levels if l["ticker"] == tk and l["reached"] and l["add_zone"]]
            if zone:
                blockers = []
                if not (story and story["all_green"]):
                    blockers.append("story checks not all green")
                if satellites_pct >= p["satellites_max_pct"]:
                    blockers.append(f"satellites {danish(satellites_pct, 1)}% ≥ {p['satellites_max_pct']}%")
                if available <= 0:
                    blockers.append("no cash beyond the ASK tax buffer")
                elif tk in semis_tickers:
                    after = values.copy()
                    after[tk] += available
                    risk_after = semis_risk_after(rules, after, returns)
                    if risk_after >= semis_risk_max:
                        blockers.append(f"semis risk would be {danish(risk_after, 1)}% ≥ {semis_risk_max}%")
                l = zone[0]
                if blockers:
                    act, reason = "ADD blocked", "; ".join(blockers)
                else:
                    act = "ADD"
                    reason = (f"{danish(l['price'])} {l['currency']} in add zone (≤ {danish(l['target'], 0)}); "
                              f"story all green; up to {danish(available, 0)} DKK")

        # HOLD: everything else.
        if act is None:
            act = "HOLD"
            r = relative[2].get(tk)
            if r and r["lagging"]:
                lag = f"1w {signed(r['gap_1w'], 1)} pts, 4w {signed(r['gap_4w'], 1)} pts vs CSPX"
                reason = (f"price lag, story intact ({lag})" if story and story["all_green"]
                          else f"lagging CSPX ({lag})")
            else:
                reason = "no rule triggered"

        entry = {"ticker": tk, "name": name, "action": act, "reason": reason, "proceeds": None}
        if act in ("SELL", "TRIM"):
            entry["sale_dkk"] = round(float(sale))
            entry["proceeds"] = proceeds_line(rules, values, cash, tk, sale, scan)
        actions.append(entry)

    return portfolio_action(rules, values, cash, available, ask, scan), actions


def portfolio_action(rules, values, cash, available, ask, scan):
    """One action for the portfolio as a whole (the core and new money)."""
    p = rules["portfolio"]
    core = p["core_ticker"]
    weight = portfolio_weights(rules, values, cash)
    core_pct = weight.get(core, 0)
    satellites_pct = weight.drop(core, errors="ignore").sum()
    room = f"deposit room left this year: {danish(ask['room'], 0)} DKK" if ask and ask["room"] is not None else ""
    buffer = (f"cash {danish(cash, 0)} - ASK buffer {danish(ask['buffer'], 0)} "
              f"({ask['cash_buffer']} x est. tax {danish(ask['tax'], 0)})") if ask else f"cash {danish(cash, 0)}"

    if core_pct < p["core_min_pct"]:
        # Buying x of CSPX makes core (core + x) / (total + x); solve for core_min.
        m = p["core_min_pct"] / 100
        to_60 = (m * values.sum() - values.get(core, 0)) / (1 - m)
        return {"action": "BUY CSPX", "text": f"BUY CSPX: up to {danish(available, 0)} DKK",
                "reason": f"core {danish(core_pct, 1)}% < {p['core_min_pct']}%; {buffer}; "
                          f"about {danish(to_60, 0)} DKK would bring core to {p['core_min_pct']}%",
                "details": room}
    if satellites_pct < p["satellites_max_pct"]:
        s = values.drop(core, errors="ignore").sum()
        m = p["satellites_max_pct"] / 100
        slot = max(0.0, min(available, (m * values.sum() - s) / (1 - m)))
        names = []
        for role in ["growth", "diversifier"]:
            c = best_candidate(rules, scan, role, exclude=set(values.index))
            names.append(f"{role}: {c['ticker']} ({danish(c['score'], 2)})" if c else f"{role}: none good enough")
        return {"action": "SLOT OPEN", "text": f"satellite slot open: up to {danish(slot, 0)} DKK",
                "reason": f"core {danish(core_pct, 1)}% ≥ {p['core_min_pct']}%, satellites "
                          f"{danish(satellites_pct, 1)}% < {p['satellites_max_pct']}%; {buffer}",
                "details": "review on Sunday - " + " · ".join(names) + (f" · {room}" if room else "")}
    return {"action": "HOLD", "text": "allocation within limits", "reason": f"core {danish(core_pct, 1)}%",
            "details": room}


def action_lines(portfolio, actions):
    """The actions as report lines (ℹ️: they never add to the ⚠️ count)."""
    lines = [f"## Actions ({ACTIONS_NOTE})",
             f"- {INFO} PORTFOLIO · {portfolio['text']} - {portfolio['reason']}"]
    if portfolio["details"]:
        lines.append(f"    - {portfolio['details']}")
    for x in actions:
        label = x["ticker"] if x["name"] == x["ticker"] else f"{x['ticker']} ({x['name']})"
        lines.append(f"- {INFO} {x['action']} · {label}: {x['reason']}")
        if x["proceeds"]:
            lines.append(f"    - proceeds {x['proceeds']}")
    return lines


def save_actions(portfolio, actions, today):
    """output/actions.json - the dashboard reads this file."""
    data = {"date": f"{today:%Y-%m-%d}", "note": ACTIONS_NOTE, "portfolio": portfolio, "holdings": actions}
    write_text_safely(OUTPUT_FOLDER / "actions.json", json.dumps(data, ensure_ascii=False, indent=2))


# --- The main program -----------------------------------------

def main():
    sys.stdout.reconfigure(encoding="utf-8")       # so ✅ and ⚠️ print correctly
    with open(RULES_FILE, "rb") as f:
        rules = tomllib.load(f)

    today = pd.Timestamp.today().normalize()
    print("Downloading prices ...")

    # Rebuild the account up to today. The rebuild prints a line per
    # download; we catch that text so the report stays on one screen.
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        t, shares, cash, prices = load_account(last_day=today)
        levels = price_levels(rules, today)
        _, last_close = latest_close(rules["portfolio"]["core_ticker"], today)
        regimes = {name: current_regime(ticker) for ticker, name in MARKETS.items()}
        held_now = [SECURITIES[i][0] for i in shares.columns if shares.loc[today, i] > 0]
        upcoming = upcoming_events(rules, held_now, today)
        ask = ask_figures(rules)
        risk_nums = risk_numbers(rules, shares, cash, prices, today)

    prices_by_ticker = prices.rename(columns=lambda isin: SECURITIES[isin][0])
    values = (shares.loc[today] * prices.loc[today]).rename(lambda isin: SECURITIES[isin][0])
    values = values[values > 0]                      # only what I hold now
    cash_now = cash[today]
    relative = relative_returns(rules, prices_by_ticker, values.index, today)
    stories = story_status(rules, today)
    scan_path = OUTPUT_FOLDER / "scanner" / "latest.json"
    scan = json.loads(scan_path.read_text(encoding="utf-8")) if scan_path.exists() else None

    # The action panel: one action per holding + one for the portfolio.
    portfolio, actions = decide_actions(rules, values, cash_now, relative, levels, stories,
                                        upcoming, ask, risk_nums, scan, today)
    save_actions(portfolio, actions, today)

    lines = [
        f"# Weekly check {today:%Y-%m-%d}",
        f"Prices: close {last_close:%Y-%m-%d}. Holdings {danish(values.sum(), 0)} DKK, "
        f"cash {danish(cash_now, 0)} DKK. Trades up to {t['date'].max():%Y-%m-%d} "
        f"(export a new transaction file if you've traded since).",
    ]
    if "WARNING" in captured.getvalue():
        lines.append(item(False, "Data check warning - run positions.py for details"))
    prices_label, offline = data_cache.price_status()
    if offline:
        lines.append(f"- {prices_label} - Yahoo couldn't be reached, so saved prices were used")
    lines += action_lines(portfolio, actions)
    lines += check_weights(rules, values, cash_now)
    lines += check_returns(relative)
    lines += check_levels(levels)
    lines += check_stories(rules, stories)

    # Market regime (regime.py). The out-of-sample test showed 'stressed'
    # predicts higher volatility over the next 20 days - NOT worse returns.
    text = " · ".join(f"{name} {label} ({danish(prob * 100, 0)}%)"
                      for name, (label, prob, _) in regimes.items())
    # Information only (ℹ️): no action is attached to the regime, so it
    # must never count toward the "items need attention" total.
    stressed = any(label == "stressed" for label, _, _ in regimes.values())
    lines.append("## Market regime")
    lines.append(f"- {INFO} " + text + (" - expect a bumpier month (volatility, not a return signal)"
                                        if stressed else ""))
    lines += risk_summary(rules, shares, cash, prices, today)
    lines += check_scanner(today)
    lines += check_calendar(upcoming)
    lines += check_ask(cash_now, ask)

    warnings = sum(WARN in line for line in lines)
    lines.append(f"**{warnings} item(s) need attention.**" if warnings else "**All clear.**")

    report = "\n".join(lines)
    print(report)

    WEEKLY_FOLDER.mkdir(parents=True, exist_ok=True)
    path = WEEKLY_FOLDER / f"{today:%Y-%m-%d}.md"
    write_text_safely(path, report + "\n")
    print(f"\nSaved to: {path}")


if __name__ == "__main__":
    main()
