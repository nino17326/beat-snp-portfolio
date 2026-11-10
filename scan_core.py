# ============================================================
#  scan_core.py  -  the scanner's shared logic (one source of truth)
#
#  Gates, fit check and scoring, used by:
#    scanner.py - every week, for the whole [scanner] universe
#    lookup.py  - the interactive page, for one ticker at a time
#  so a ticker is always judged by exactly the same rules.
# ============================================================

import contextlib
import copy
import io
import time
import tomllib

import pandas as pd
import yfinance as yf

from portfolio import danish
from shadow_portfolio import download_close
from positions import (load_account, fill_every_day, FX_TICKERS, SECURITIES,
                       OUTPUT_FOLDER)
# The fit check reuses the portfolio limits and risk maths - not copies.
from weekly_check import portfolio_weights, single_stock_weights, group_weights, estimate_changes
from risk import holdings_today, currency_exposure, returns_from_prices, risk_table
import settings                                  # where rules.toml is (the data folder)

RULES_FILE = settings.RULES_FILE
PASS, FAIL, INFO, SKIP = "✅", "❌", "ℹ️", "–"
PAUSE_SECONDS = 0.5          # be polite to Yahoo: short pause between requests
CAP_MISMATCH_PCT = 25        # flag if market cap and price x shares differ by more
GATE_ORDER = ["Yahoo", "Cap", "EPS", "FCF", "Debt", "Price", "Sector", "Corr", "ASK"]

# Exchange-rate tickers for every currency a candidate may trade in
# (the same list positions.py uses for holdings).
SCANNER_FX = FX_TICKERS


def load_rules():
    return settings.load_rules()


# --- My portfolio, as the gates and fit check need it ---------

def load_context(rules, today):
    """Everything about MY holdings that the gates, fit and scores need."""
    with contextlib.redirect_stdout(io.StringIO()):          # keep the rebuild quiet
        t, shares, cash, prices = load_account(last_day=today)
    held = [i for i in shares.columns if shares.loc[today, i] > 0]
    holdings_daily = prices[held].rename(columns=lambda i: SECURITIES[i][0])
    return {
        "today": today,
        "days": pd.date_range(today - pd.Timedelta(days=400), today, freq="D"),
        "transactions": t,
        "holdings_daily": holdings_daily,
        "holdings_weekly": weekly_returns(holdings_daily.loc[today - pd.Timedelta(days=365):]),
        "values": holdings_today(shares, prices, today),    # DKK per holding, by ticker
        "cash": cash[today],
        "core": rules["portfolio"]["core_ticker"],
    }


def portfolio_weekly(ctx, exclude=None):
    """Weekly DKK return of my whole portfolio (value-weighted), optionally
    without one holding - so a holding isn't correlated with itself."""
    values, holdings_weekly = ctx["values"], ctx["holdings_weekly"]
    w = values / values.sum()
    if exclude is not None:
        w = w.drop(exclude, errors="ignore")
    w = w[[x for x in w.index if x in holdings_weekly.columns]]
    return (holdings_weekly[w.index] * w).sum(axis=1) / w.sum()


# --- Fetching -------------------------------------------------

def fetch(ticker, attempts=2):
    """Everything we need about one candidate from Yahoo, or raises.
    Yahoo sometimes fails once and works the next time, so we retry."""
    for attempt in range(1, attempts + 1):
        try:
            return _fetch_once(ticker)
        except Exception:
            if attempt == attempts:
                raise                      # give up: the caller logs it and moves on
            time.sleep(PAUSE_SECONDS * 4)  # wait a bit longer before retrying


def _fetch_once(ticker):
    t = yf.Ticker(ticker)
    info = t.info or {}
    closes = t.history(period="2y", auto_adjust=False)["Close"]   # 2y: momentum needs 12 months back
    if closes.empty and not info.get("quoteType"):
        raise ValueError("no data on Yahoo")
    closes.index = closes.index.tz_localize(None).normalize()

    currency = info.get("currency")
    last_price = closes.iloc[-1] if len(closes) else info.get("regularMarketPrice")
    if currency == "GBp":                  # London quotes in pence: 14012 GBp = 140,12 GBP
        closes = closes / 100
        last_price = last_price / 100 if last_price is not None else None
        currency = "GBP"                   # (Yahoo's market cap is already in pounds)

    return {
        "name": info.get("longName") or info.get("shortName"),
        "quote_type": info.get("quoteType"),
        "exchange": info.get("fullExchangeName") or info.get("exchange"),
        "currency": currency,
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "market_cap": info.get("marketCap"),
        "shares": info.get("sharesOutstanding"),
        "price": last_price,
        "eps": info.get("trailingEps"),
        "forward_eps": info.get("forwardEps"),   # for the scores (step 2)
        "forward_pe": info.get("forwardPE"),
        "fcf": info.get("freeCashflow"),
        "debt": info.get("totalDebt"),     # for the utilities debt gate
        "cash": info.get("totalCash"),
        "ebitda": info.get("ebitda"),
        "closes": closes,
    }


def fx_to_dkk(currencies, days):
    """{currency: daily DKK rate} for every currency we need (EUR always,
    because market caps are compared in EUR)."""
    start, end = days[0] - pd.Timedelta(days=10), days[-1] + pd.Timedelta(days=1)
    rates = {"DKK": pd.Series(1.0, index=days)}
    for cur in sorted((set(currencies) | {"EUR"}) - {"DKK", None}):
        if cur not in SCANNER_FX:
            print(f"  No FX ticker for {cur} - candidates in {cur} get ℹ️ for price checks")
            continue
        rates[cur] = fill_every_day(download_close(SCANNER_FX[cur], start, end), days)
        time.sleep(PAUSE_SECONDS)
    return rates


# --- Gates ----------------------------------------------------

def weekly_returns(daily_dkk):
    """Friday-to-Friday % changes (weekly returns are less noisy than daily
    ones when markets close at different times or on different holidays)."""
    return daily_dkk.resample("W-FRI").last().pct_change().dropna(how="all")


def run_gates(c, d, rules, fx, holdings_weekly, today):
    """Return {gate: (symbol, reason)} for one candidate.
    c = the candidate's line in rules.toml, d = fetched data (or None)."""
    s = rules["scanner"]
    is_stock = c["type"] == "stock"
    g = {}

    if d is None:
        g["Yahoo"] = (FAIL, "ticker doesn't resolve on Yahoo")
        return g
    g["Yahoo"] = (PASS, d["name"] or c["ticker"])

    rate = fx.get(d["currency"])
    rate_now = rate[today] if rate is not None else None
    eur_now = fx["EUR"][today]

    # Market cap (stocks only), in EUR, plus a sanity check.
    if not is_stock:
        g["Cap"] = (SKIP, "ETF - no market cap")
    elif d["market_cap"] is None or rate_now is None:
        g["Cap"] = (INFO, "market cap missing")
    else:
        cap_eur = d["market_cap"] * rate_now / eur_now
        d["cap_eur"] = cap_eur
        ok = cap_eur >= s["min_market_cap_eur"]
        reason = f"{danish(cap_eur / 1e9, 0)} bn EUR"
        if d["shares"] and d["price"]:
            implied = d["shares"] * d["price"]
            gap = abs(implied / d["market_cap"] - 1) * 100
            if gap > CAP_MISMATCH_PCT:
                reason += f" (check: price x shares differs {danish(gap, 0)}%)"
        g["Cap"] = (PASS if ok else FAIL, reason)

    # Profitability (stocks only). Missing data = ℹ️, never ❌.
    if not is_stock:
        g["EPS"] = (SKIP, "ETF")
        g["FCF"] = (SKIP, "ETF")
        g["Debt"] = (SKIP, "ETF")
    else:
        eps = d["eps"]
        g["EPS"] = (INFO, "EPS missing") if eps is None else \
                   (PASS if eps > 0 else FAIL, f"trailing EPS {danish(eps)}")
        labels = f"{d['sector']} {c['sector']}".lower()
        financial = "financial" in labels
        utility = "utilities" in labels
        if financial or utility:
            # Banks/insurers and utilities: free cash flow says little
            # (utilities spend heavily on grids and plants, financed by debt).
            g["FCF"] = (SKIP, "not meaningful for " + ("financials" if financial else "utilities"))
        elif d["fcf"] is None:
            g["FCF"] = (INFO, "free cash flow missing")
        else:
            g["FCF"] = (PASS if d["fcf"] > 0 else FAIL, f"FCF {danish(d['fcf'] / 1e9, 1)} bn {d['currency']}")

        # Utilities instead: is the debt manageable? Net debt / EBITDA =
        # years of operating profit it would take to pay the debt back.
        if not utility:
            g["Debt"] = (SKIP, "utilities only")
        elif d["debt"] is None or d["ebitda"] is None or d["ebitda"] <= 0:
            g["Debt"] = (INFO, "debt or EBITDA missing")
        else:
            ratio = (d["debt"] - (d["cash"] or 0)) / d["ebitda"]
            limit = s["max_net_debt_ebitda_utilities"]
            g["Debt"] = (PASS if ratio <= limit else FAIL,
                         f"net debt/EBITDA {danish(ratio, 1)}x (max {limit}x)")

    # Share price in DKK.
    if d["price"] is None or rate_now is None:
        g["Price"] = (INFO, "price or FX missing")
    else:
        d["price_dkk"] = d["price"] * rate_now
        g["Price"] = (PASS if d["price_dkk"] <= s["max_share_price_dkk"] else FAIL,
                      f"{danish(d['price_dkk'], 0)} DKK per share")

    # Excluded sectors: Yahoo's sector and industry (and my own label).
    excluded = [x.lower() for x in s["excluded_sectors"]]
    labels = [x for x in (d["sector"], d["industry"]) if x]
    hits = [x for x in labels + [c["sector"]] if x.lower() in excluded]
    if hits:
        g["Sector"] = (FAIL, f"excluded: {hits[0]}")
    elif not labels:
        g["Sector"] = (INFO, "Yahoo has no sector (normal for ETFs)")
    else:
        g["Sector"] = (PASS, " / ".join(labels))

    # Near-copy check: weekly DKK returns vs. each holding.
    if rate is None or len(d["closes"]) < 60:
        g["Corr"] = (INFO, "not enough price history")
    else:
        daily_dkk = fill_every_day(d["closes"], rate.index) * rate
        d["daily_dkk"] = daily_dkk          # kept for the fit check's risk estimate
        mine = weekly_returns(daily_dkk)
        both = holdings_weekly.join(mine.rename("cand"), how="inner").dropna()
        corr = both.drop(columns="cand").corrwith(both["cand"]).sort_values(ascending=False)
        top, value = corr.index[0], corr.iloc[0]
        g["Corr"] = (FAIL if value > s["max_correlation"] else PASS,
                     f"highest {danish(value)} with {top} ({len(both)} weeks)")

    # ASK eligibility - set by hand in rules.toml.
    ask = c.get("ask_ok", "unknown")
    if ask is True:
        g["ASK"] = (PASS, "ASK-eligible")
    elif ask is False:
        g["ASK"] = (FAIL, "not allowed in ASK")
    else:
        g["ASK"] = (INFO, "check on Nordnet")
    return g


def verdict(gates):
    """'fail' if any gate failed; 'needs ASK check' if nothing failed but
    ask_ok is still unknown; otherwise 'pass'. A * means some other gate
    had no data (ℹ️) - see the notes."""
    symbols = [sym for sym, _ in gates.values()]
    if FAIL in symbols:
        return "fail"
    data_gaps = [sym for name, (sym, _) in gates.items() if sym == INFO and name != "ASK"]
    label = "needs ASK check" if gates.get("ASK", (INFO, ""))[0] == INFO else "pass"
    return label + ("*" if data_gaps else "")


# --- Fit check: would it fit my portfolio rules? --------------

def fit_check(c, d, rules, values, cash, prices_dkk, today, swap_out=None):
    """What happens to my portfolio if I add this candidate?

    values     = DKK in each holding today (by ticker)
    prices_dkk = daily DKK prices of my holdings AND this candidate
    swap_out   = which holding it would replace (default: the biggest
                 one with the same role; the scanner passes the weakest-scoring)
    Returns (symbol, short reason, details).
    """
    s, p = rules["scanner"], rules["portfolio"]
    ticker, role = c["ticker"], c["role"]
    core = p["core_ticker"]
    roles = s.get("holding_roles", {})
    sectors = {**s.get("holding_sectors", {}), ticker: c["sector"]}
    amount = s["new_position_pct"] / 100 * values.sum()   # DKK for a new satellite

    # Which current satellite would it replace? Default: the biggest one with the same role.
    if swap_out is None:
        same_role = [t for t in values.index if roles.get(t) == role]
        swap_out = max(same_role, key=lambda t: values[t]) if same_role else None

    def swapped():
        """Sell `amount` DKK of the same-role satellite, buy the candidate."""
        after = values.copy()
        sold = min(amount, after[swap_out])
        after[swap_out] -= sold
        after[ticker] = sold
        return after[after > 0]

    # 1. Slot: is there room under the satellite limit?
    satellites_now = portfolio_weights(rules, values, cash).drop(core, errors="ignore").sum()
    free_slot = satellites_now + s["new_position_pct"] <= p["satellites_max_pct"]
    if free_slot:
        after = values.copy()
        after[ticker] = amount
        slot_text = "free slot"
    elif swap_out is None:
        return FAIL, f"no slot, and no {role} satellite to swap", ""
    else:
        after = swapped()
        slot_text = f"swap only: for {swap_out}"

    weight = portfolio_weights(rules, after, cash)
    problems = []

    # 2. Size: no single stock above the limit (ETFs/funds don't count).
    funds = [ticker] if c["type"] == "etf" else []
    singles = single_stock_weights(rules, weight, extra_funds=funds)
    too_big = singles[singles > p["single_stock_max_pct"]]
    problems += [f"{t} {danish(w, 1)}% > {p['single_stock_max_pct']}%" for t, w in too_big.items()]
    for name, (w, limit) in group_weights(rules, weight).items():        # e.g. semis cap
        if w > limit:
            problems.append(f"{name} {danish(w, 1)}% > {limit}%")

    # 3. Sector: no sector above max_sector_pct across satellites.
    satellite_weight = weight.drop(core, errors="ignore")
    by_sector = satellite_weight.groupby(lambda t: sectors.get(t, "unknown")).sum()
    my_sector = by_sector.get(c["sector"], 0)
    if by_sector.max() > s["max_sector_pct"]:
        top = by_sector.idxmax()
        problems.append(f"{top} {danish(by_sector.max(), 1)}% > {s['max_sector_pct']}%")

    # 4. Currency: how would my USD/EUR/DKK/other split change? (info only)
    rules_plus = copy.deepcopy(rules)
    mapped = ticker in rules_plus["risk"]["currency"]
    rules_plus["risk"]["currency"].setdefault(ticker, d["currency"])     # unknown: trading currency
    before_fx = currency_exposure(values, cash, rules_plus)
    after_fx = currency_exposure(after, cash, rules_plus)

    def bucket(exposure):
        main = {k: exposure.get(k, 0.0) for k in ["USD", "EUR", "DKK"]}
        main["other"] = exposure.sum() - sum(main.values())
        return main

    b, a = bucket(before_fx), bucket(after_fx)
    fx_text = " ".join(f"{k} {danish(b[k], 0)}→{danish(a[k], 0)}" for k in b)
    if a["USD"] > b["USD"] + 0.05:
        fx_text += " · USD ↑"
    if c["type"] == "etf" and not mapped:
        fx_text += " (ETF at trading currency)"

    # 5. Risk: yearly swing if it replaces the same-role satellite (info only).
    risk_text = "swing n/a"
    if swap_out is not None and ticker in prices_dkk.columns:
        lookback = rules["risk"]["lookback_days"]
        swap = swapped()
        returns = returns_from_prices(prices_dkk[list(set(values.index) | set(swap.index))],
                                      today, lookback)
        _, swing_now = risk_table(values, returns[values.index])
        _, swing_after = risk_table(swap, returns[swap.index])
        risk_text = (f"swing {danish(swing_now / (values.sum() + cash) * 100, 1)}%→"
                     f"{danish(swing_after / (swap.sum() + cash) * 100, 1)}% (if swapped for {swap_out})")

    details = f"{c['sector']} {danish(my_sector, 1)}% · {fx_text} · {risk_text}"
    if problems:
        return FAIL, "; ".join(problems), details
    return (PASS if free_slot else INFO), slot_text, details


def fit_for(c, d, rules, ctx, result, swap_out=None):
    """The fit check, only for candidates that passed the gates."""
    if result.startswith("fail"):
        return SKIP, "gates failed", ""
    prices_dkk = ctx["holdings_daily"]
    if "daily_dkk" in d:
        prices_dkk = prices_dkk.join(d["daily_dkk"].rename(c["ticker"]), how="outer").ffill()
    return fit_check(c, d, rules, ctx["values"], ctx["cash"], prices_dkk, ctx["today"], swap_out=swap_out)


# --- Scores ---------------------------------------------------

# For each factor: is a HIGHER raw value better?
HIGHER_IS_BETTER = {"revisions": True, "earnings_growth": True, "momentum": True,
                    "valuation": False, "low_correlation": False, "low_volatility": False}
EARNINGS_GROWTH_CAP = 50      # % - earnings growth is capped at +/- this


def raw_factors(ticker, d, daily_dkk, cspx_dkk, portfolio_weekly, today):
    """The six raw numbers behind the score (None = no data)."""
    f = dict.fromkeys(HIGHER_IS_BETTER)
    f["growth_capped"] = False

    e = estimate_changes(ticker)                  # same numbers as the weekly check
    if e is not None:
        f["revisions"] = e["next_90"]

        # Earnings growth: analysts' next-FY EPS vs. this-FY EPS, from the SAME
        # estimate table (same currency, same accounting). Capped at +/-50%
        # so one odd number (a loss year, a one-off) can't dominate.
        this_fy, next_fy = e["eps_this_fy"], e["eps_next_fy"]
        if this_fy and this_fy > 0 and next_fy is not None:
            growth = (next_fy / this_fy - 1) * 100
            f["growth_capped"] = abs(growth) > EARNINGS_GROWTH_CAP
            f["earnings_growth"] = max(-EARNINGS_GROWTH_CAP, min(EARNINGS_GROWTH_CAP, growth))

    if d.get("forward_pe") and d["forward_pe"] > 0:
        f["valuation"] = d["forward_pe"]

    if daily_dkk is not None and daily_dkk.notna().sum() > 200:
        # Momentum: return from 12 months ago to 1 month ago (skipping the
        # last month, which tends to bounce back), minus CSPX's return.
        def ret(series):
            a = series.asof(today - pd.Timedelta(days=365))
            b = series.asof(today - pd.Timedelta(days=30))
            return (b / a - 1) * 100 if a and b and not pd.isna(a) else None
        mine, core = ret(daily_dkk), ret(cspx_dkk)
        if mine is not None and core is not None:
            f["momentum"] = mine - core

        year = daily_dkk.loc[today - pd.Timedelta(days=365):]
        daily = year[year.index.dayofweek < 5].pct_change().dropna()
        f["low_volatility"] = daily.std() * 252 ** 0.5 * 100

        weekly = weekly_returns(daily_dkk.loc[today - pd.Timedelta(days=365):])
        both = pd.concat([weekly, portfolio_weekly], axis=1, join="inner").dropna()
        if len(both) > 20:
            f["low_correlation"] = both.iloc[:, 0].corr(both.iloc[:, 1])
    return f


def score_role(raw, weights, min_factors):
    """Percentile-rank each factor within the role (0 = worst, 1 = best),
    then the weighted average of the factors that have data.
    Returns (scores, star) - star = some factor missing, weights rescaled.
    Anything with fewer than min_factors factors gets NO score (NaN) and
    is left out of the ranking itself, so it can't shift others' ranks."""
    counts = raw[list(weights)].apply(pd.to_numeric, errors="coerce").notna().sum(axis=1)
    raw = raw[counts >= min_factors]
    pct = pd.DataFrame(index=raw.index)
    for factor in weights:
        col = pd.to_numeric(raw[factor], errors="coerce")
        n = col.notna().sum()
        ranks = col.rank(ascending=HIGHER_IS_BETTER[factor])   # best gets the highest rank
        pct[factor] = (ranks - 1) / (n - 1) if n > 1 else 0.5
    w = pd.Series(weights)
    available = pct.notna().mul(w, axis=1).sum(axis=1)       # weights of factors with data
    scores = pct.fillna(0).mul(w, axis=1).sum(axis=1) / available
    unscored = counts.index[counts < min_factors]
    scores = pd.concat([scores, pd.Series(float("nan"), index=unscored)])
    stars = pd.concat([pct.isna().any(axis=1), pd.Series(False, index=unscored)])
    return scores, stars


def rank_with_latest(ticker, role, raw, rules):
    """Score ONE ticker against everything the last scanner run scored in
    the same role (its saved factors file), with the same recipe.
    Returns (score, star, rank, how many scored) - rank 1 = best."""
    files = sorted((OUTPUT_FOLDER / "scanner").glob("factors_????-??-??.csv"))
    if not files:
        return float("nan"), False, None, 0
    saved = pd.read_csv(files[-1], sep=";", decimal=",", encoding="utf-8-sig", index_col="ticker")
    pool = saved[(saved["role"] == role) & (saved.index != ticker)][list(HIGHER_IS_BETTER)]
    pool.loc[ticker] = pd.Series(raw)[list(HIGHER_IS_BETTER)]
    scores, stars = score_role(pool, rules["scanner"]["weights"][role], rules["scanner"]["min_factors"])
    scored = scores.dropna().sort_values(ascending=False)
    if ticker not in scored.index:
        return float("nan"), False, None, len(scored)
    return scores[ticker], bool(stars[ticker]), list(scored.index).index(ticker) + 1, len(scored)


def score_text(score, star, long=False):
    if pd.isna(score):
        return "not scored (ETF / missing data)" if long else "not scored"
    return danish(score, 2) + ("*" if star else "")


def eligible_candidates(table, role):
    """Candidates of a role that passed the gates and the fit check, best first."""
    ok = table[(table["role"] == role) & table["verdict"].str.startswith("pass")
               & (table["Fit"] != FAIL) & table["score"].notna()]
    return ok.sort_values("score", ascending=False)
