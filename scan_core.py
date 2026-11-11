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
from urllib.parse import urlencode

import pandas as pd
import data_cache                                # Yahoo data, saved locally

from portfolio import danish
from shadow_portfolio import download_close
from positions import (load_account, fill_every_day, FX_TICKERS, SECURITIES,
                       OUTPUT_FOLDER)
# The fit check reuses the portfolio limits and risk maths - not copies.
from weekly_check import portfolio_weights, single_stock_weights, group_weights, estimate_changes
from risk import holdings_today, currency_exposure, returns_from_prices, risk_table
import settings                                  # where rules.toml is (the data folder)

# Exchange-rate tickers for every currency a candidate may trade in
# (the same list positions.py uses for holdings).
SCANNER_FX = FX_TICKERS


# --- Plain-English texts (ONE place for the lookup, scanner and dashboard) ---

EXPLAIN = {
    "legend": "✅ within your rules · ⚠️ allowed but worth noticing · ❌ breaks a rule · ℹ️ information only",
    "same_sector_swap": ("Same-sector swap: the sector exclusion doesn't apply because your exposure "
                         "doesn't increase."),
    "corr_swap": "The near-copy check leaves out {out}, because it would be sold.",
    "fit_title": "Fit check: would this follow your portfolio rules?",
    "fit_link": "explain",
    "fit_short_help": "Short fit summary. Open the lookup page for the full explained fit check.",

    # Tooltips (hover ⓘ): what the word means, with an everyday example.
    "terms": {
        "P/E": "Price / earnings: what you pay for 1 krone of yearly profit. P/E 20 = you pay 20 kr. "
               "for a company earning 1 kr. a year, like paying 20 years' rent up front.",
        "PEG": "P/E divided by the expected yearly profit growth in %. P/E 30 with 30% growth = PEG 1. "
               "Below 1 is cheap for the growth, above 2 is pricey - like judging a house's price by "
               "both its size and how fast the area is growing.",
        "EPS revisions": "How much analysts changed their profit-per-share forecast for next year. "
                         "If a forecast goes from 10 kr. to 11 kr., that's a +10% revision - like a weather "
                         "forecast for the weekend getting sunnier as the days pass.",
        "earnings growth": "How much analysts expect profit per share to grow from this year to next. "
                           "From 10 kr. to 12 kr. = +20%. Capped at ±50% so one odd year can't dominate.",
        "momentum": "How much the share rose over the past year (skipping the last month) compared with "
                    "the S&P 500 fund. +10 = it did 10 points better than the index - like a runner who "
                    "has been ahead of the pack for a year.",
        "correlation": "How much two things move together, from -1 to 1. 1 = always up and down together "
                       "(two umbrellas), 0 = unrelated, below 0 = opposite (umbrellas and sunglasses).",
        "volatility": "How much the price typically swings in a year. 20% on 10.000 kr. = it often ends "
                      "the year 2.000 kr. higher or lower - like a bumpy vs. a smooth road.",
        "percentile rank": "Where a value sits in the group, from 0 (worst) to 1 (best). 0,75 = better "
                           "than three out of four - like finishing 25th of 100 in a race.",
        "score": "The weighted average of the factor percentile ranks for the role (0 = worst, 1 = best).",
    },

    # The scoring factors: what each measures, the tooltip term, and a
    # one-line translation. {A}/{B} = names, {a}/{b} = values, {W} = winner.
    "factors": {
        "revisions": {
            "label": "EPS revisions", "term": "EPS revisions", "unit": "%", "signed": True,
            "measures": "How much analysts changed next year's profit forecast in the last 90 days.",
            "line": "Analysts changed next year's profit forecast by {a} for {A} vs. {b} for {B}",
            "win": "they're getting more optimistic about {W} faster.",
            "win_neg": "they're cutting {W}'s forecast less.",
        },
        "earnings_growth": {
            "label": "Earnings growth", "term": "earnings growth", "unit": "%", "signed": True,
            "measures": "How much analysts expect profit per share to grow from this year to next.",
            "line": "Profit per share is expected to grow {a} for {A} vs. {b} for {B}",
            "win": "{W} is expected to grow its profit faster.",
            "win_neg": "{W}'s profit is expected to shrink less.",
        },
        "momentum": {
            "label": "Momentum", "term": "momentum", "unit": " pts", "signed": True,
            "measures": "Last year's return (skipping the last month) minus the S&P 500 fund's, in DKK.",
            "line": "Over the past year {A} did {a} vs. the S&P 500 fund and {B} {b}",
            "win": "{W} has had the stronger run.",
            "win_neg": "{W} lagged the index less.",
        },
        "valuation": {
            "label": "Valuation (forward P/E)", "term": "P/E", "unit": "x", "signed": False,
            "measures": "What you pay today for one krone of next year's expected profit (lower = cheaper).",
            "line": "You pay {a} next year's profit for {A} vs. {b} for {B}",
            "win": "{W} is cheaper for each krone of expected profit.",
        },
        "low_correlation": {
            "label": "Correlation with my portfolio", "term": "correlation", "unit": "", "signed": False,
            "measures": "How closely its weekly moves follow your current portfolio (lower = spreads risk more).",
            "line": "{A}'s weekly moves have a correlation of {a} with your portfolio vs. {b} for {B}",
            "win": "{W} would spread your risk more.",
        },
        "low_volatility": {
            "label": "Volatility", "term": "volatility", "unit": "%", "signed": False,
            "measures": "How much the price swung over the last year, in DKK (lower = calmer).",
            "line": "{A} swung about {a} a year vs. {b} for {B}",
            "win": "{W} has been the calmer ride.",
        },
    },

    # The explained fit check: one "What it means" sentence per line.
    "fit": {
        "slot": {"title": "Slot",
                 "free": "Satellites are {now} of your holdings; adding {new} keeps them within the {limit} "
                         "limit, so there's room without selling anything.",
                 "swap_weakest": "Satellites are already {now} of your holdings (limit {limit}); another {new} "
                                 "won't fit, so it would replace {out} - your weakest-scoring {role} satellite "
                                 "({score} in the last scanner run).",
                 "swap_picked": "You picked {out} to replace. The new stock takes over its full {dkk} DKK, so "
                                "the satellite total stays at {now} (limit {limit}).",
                 "over": " Satellites are already above the limit; this swap doesn't change that.",
                 "none": "Satellites are {now} (limit {limit}) and there is no {role} satellite to swap out."},
        "size": {"title": "Position size",
                 "stock": "A single stock may be at most {limit} of your holdings, so one company can't sink "
                          "the portfolio.",
                 "etf": "It's a fund, so the single-stock limit doesn't apply - a fund already spreads the risk."},
        "group": {"title": "{group} cap",
                  "meaning": "{group} together may be at most {money_limit} of your money and cause at most "
                             "{risk_limit} of the portfolio's swings."},
        "sector": {"title": "Sector",
                   "new": "Adds a new sector ({sector}) to your satellites, which spreads the risk.",
                   "same": "Swaps like for like: {sector} stays at the same weight.",
                   "more": "Adds to a sector you already own ({sector}: {before} now), so it concentrates "
                           "rather than spreads.",
                   "less": "Lowers your {sector} weight ({before} now)."},
        "currency": {"title": "Currency",
                     "meaning": "{out} earns in {out_cur} and {cand} in {cand_cur}. {change} If the dollar "
                                "fell 10% against the krone, you'd lose about {before_dkk} DKK today and "
                                "{after_dkk} DKK after the swap.",
                     "meaning_add": "{cand} earns in {cand_cur} and would be bought with new money. {change} "
                                    "If the dollar fell 10% against the krone, you'd lose about {before_dkk} DKK "
                                    "today and {after_dkk} DKK after buying it.",
                     "up": "Your dollar share goes up.",
                     "down": "Your dollar share goes down.",
                     "same": "Your dollar share stays the same."},
        "risk": {"title": "Risk",
                 "meaning": "A typical bad year would hurt about {diff} DKK {direction}. {why}",
                 "why": "{cand} has swung {cand_vol} a year on its own vs. {out_vol} for {out}, and its "
                        "correlation with the rest of your portfolio is {cand_corr} vs. {out_corr}.",
                 "why_add": "{cand} has swung {cand_vol} a year on its own, and its correlation with the "
                            "rest of your portfolio is {cand_corr}.",
                 "na": "Not enough price history to estimate the swing."},
        "cost": {"title": "Trading cost",
                 "meaning": "Selling {out} and buying {cand}: about {fees} DKK in Nordnet fees and {fx} DKK in "
                            "currency exchange (assumed rates - check Nordnet's price list). Selling inside the "
                            "ASK adds no extra tax: the ASK is taxed on its yearly value change anyway.",
                 "buy_only": "Buying {cand} for {dkk} DKK: about {fees} DKK in Nordnet fees and {fx} DKK in "
                             "currency exchange (assumed rates - check Nordnet's price list). No extra tax "
                             "inside the ASK."},
    },

    "verdict": {
        "beats": "{A} {a} vs. {B} {b}: beats it by {gap} (needs a gap of at least {need}).",
        "close": "{A} {a} vs. {B} {b}: scores higher, but not by enough (gap {gap}, needs at least {need}).",
        "loses": "{A} {a} vs. {B} {b}: doesn't beat it (needs a gap of at least {need}).",
        "unscored": "No score comparison: {who} can't be scored (ETF or too few factors with data).",
        "why": "{label} (weight {weight}): {W} ranks higher ({wp} vs. {lp} percentile), worth {pts} points.",
        "story_intact": "{name}: {green} of {total} story checks green; selling a stock whose story is "
                        "intact needs a better reason than a score.",
        "story_red": "{name}: {red} of {total} story checks red - the story itself is weakening.",
        "story_mixed": "{name}: {green} of {total} story checks green, {other} not green.",
        "story_none": "{name}: no story checks written in rules.toml.",
        "thesis_none": "{name}: no thesis or kill rule written yet.",
        "thesis_partial": "{name}: {have} written, {missing} still missing.",
        "thesis_ok": "{name}: thesis and kill rule written.",
    },
}

# Assumed Nordnet DK trading costs, overridable with a [costs] section in
# rules.toml (same keys). Approximate - check Nordnet's price list.
COSTS = {"commission_pct": 0.15, "commission_min_dkk": 39,          # foreign markets
         "commission_pct_dk": 0.10, "commission_min_dkk_dk": 29,    # Copenhagen (.CO)
         "fx_pct": 0.25}                                            # currency exchange, each way


def term_tip(term):
    """The tooltip text for a term (see EXPLAIN['terms'])."""
    return EXPLAIN["terms"].get(term, "")


def lookup_link(ticker, role=None, mode=None, out=None, ask=None):
    """Address of the lookup page that opens straight on the explained result."""
    params = {"ticker": ticker, "mode": mode, "out": out, "role": role, "ask": ask, "run": 1}
    return LOOKUP_URL + "?" + urlencode({k: v for k, v in params.items() if v})

RULES_FILE = settings.RULES_FILE
PASS, FAIL, INFO, SKIP = "✅", "❌", "ℹ️", "–"
WARN = "⚠️"                  # allowed, but worth noticing
LOOKUP_URL = "http://localhost:8501/"   # the lookup page (lookup.py, via 'Start lookup.bat')
PAUSE_SECONDS = 0.5          # be polite to Yahoo: short pause between requests
CAP_MISMATCH_PCT = 25        # flag if market cap and price x shares differ by more
GATE_ORDER = ["Yahoo", "Cap", "EPS", "FCF", "Debt", "Price", "Sector", "Corr", "ASK"]


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
    info = data_cache.info(ticker)                       # refreshed once a day
    if not info:                                         # offline and nothing saved yet
        raise ValueError("no company info (Yahoo unreachable and nothing saved yet)")
    try:                                                 # 2y: momentum needs 12 months back
        closes = data_cache.closes(ticker, pd.Timestamp.today().normalize() - pd.DateOffset(years=2))
    except LookupError:
        closes = pd.Series(dtype=float)
    if closes.empty and not info.get("quoteType"):
        raise ValueError("no data on Yahoo")

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


# --- Same-sector swaps (replace mode on the lookup page) ------

def same_sector_swap(c, d, rules, out, out_data):
    """True if the candidate is in the same sector (my label) or the same
    Yahoo industry as the holding it would replace (e.g. AVGO for ASML.AS)."""
    mine = rules["scanner"].get("holding_sectors", {}).get(out)
    if mine and c.get("sector") and c["sector"].lower() == mine.lower():
        return True
    industry, out_industry = (d or {}).get("industry"), (out_data or {}).get("industry")
    return bool(industry and out_industry and industry == out_industry)


def apply_same_sector_swap(gates, out):
    """Replace mode, same-sector swap: an 'excluded sector' fail doesn't
    apply (exposure doesn't grow). Every cap is still checked by the fit."""
    if gates.get("Sector", (None,))[0] == FAIL:
        gates["Sector"] = (WARN, f"{gates['Sector'][1]} - same-sector swap for {out}, so not applied")
    return gates


def with_swap_groups(rules, ticker, out):
    """A copy of the rules where the candidate joins every [groups.X] the
    outgoing holding is in (AVGO replacing ASML.AS still counts as semis)."""
    rules = copy.deepcopy(rules)
    for g in rules.get("groups", {}).values():
        if out in g["tickers"] and ticker not in g["tickers"]:
            g["tickers"].append(ticker)
    return rules


# --- Fit check: would it fit my portfolio rules? --------------

def group_risk_limit(rules, name):
    """Max share of the swings for a group (only semiconductors has one)."""
    return rules["risk"].get("semis_max_risk_share") if name == "semiconductors" else None


def fit_numbers(c, d, rules, values, cash, prices_dkk, today, swap_out=None, replace=False,
                same_sector=False):
    """Every number behind the fit check, for the one-line summary
    (fit_check) and the explained block (fit_explained).

    values     = DKK in each holding today (by ticker)
    prices_dkk = daily DKK prices of my holdings AND this candidate
    swap_out   = which holding it would replace (default: the biggest
                 one with the same role; the scanner passes the weakest-scoring)
    replace    = True: I picked swap_out myself (lookup 'Replace a holding'):
                 it is sold in full and the candidate takes over its money.
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
    if replace and same_sector:
        rules = with_swap_groups(rules, ticker, swap_out)

    def swapped():
        """Sell `amount` DKK of the same-role satellite (all of it in replace mode), buy the candidate."""
        after = values.copy()
        sold = after[swap_out] if replace else min(amount, after[swap_out])
        after[swap_out] -= sold
        after[ticker] = sold
        return after[after > 0]

    n = {"ticker": ticker, "role": role, "type": c["type"], "sector": c["sector"], "swap_out": swap_out,
         "replace": replace, "same_sector": same_sector, "cash": cash, "values": values,
         "new_pct": s["new_position_pct"], "sat_limit": p["satellites_max_pct"],
         "single_limit": p["single_stock_max_pct"], "sector_limit": s["max_sector_pct"],
         "currency": d.get("currency"), "costs": rules.get("costs", {}), "problems": []}

    # 1. Slot: is there room under the satellite limit?
    n["satellites_now"] = portfolio_weights(rules, values, cash).drop(core, errors="ignore").sum()
    n["free_slot"] = not replace and n["satellites_now"] + s["new_position_pct"] <= p["satellites_max_pct"]
    if n["free_slot"]:
        after = values.copy()
        after[ticker] = amount
        n["slot_text"] = "free slot"
    elif swap_out is None:
        n["slot_text"] = f"no slot, and no {role} satellite to swap"
        n["after"] = None
        return n
    else:
        after = swapped()
        n["slot_text"] = f"replaces {swap_out} (picked)" if replace else f"swap only: for {swap_out}"
    n["after"] = after
    n["bought_dkk"] = after[ticker]
    n["sold_dkk"] = 0 if n["free_slot"] else values[swap_out] - after.get(swap_out, 0)

    weight = portfolio_weights(rules, after, cash)
    n["weight"] = weight[ticker]

    # 2. Size: no single stock above the limit (ETFs/funds don't count).
    funds = [ticker] if c["type"] == "etf" else []
    singles = single_stock_weights(rules, weight, extra_funds=funds)
    too_big = singles[singles > p["single_stock_max_pct"]]
    n["too_big"] = too_big
    n["problems"] += [f"{t} {danish(w, 1)}% > {p['single_stock_max_pct']}%" for t, w in too_big.items()]
    before_groups = group_weights(rules, portfolio_weights(rules, values, cash))
    n["groups"] = {}
    for name, (w, limit) in group_weights(rules, weight).items():        # e.g. semis cap
        n["groups"][name] = {"before": before_groups[name][0], "after": w, "limit": limit,
                             "risk_limit": group_risk_limit(rules, name),
                             "tickers": rules["groups"][name]["tickers"]}
        if w > limit:
            n["problems"].append(f"{name} {danish(w, 1)}% > {limit}%")

    # 3. Sector: no sector above max_sector_pct across satellites.
    satellite_weight = weight.drop(core, errors="ignore")
    by_sector = satellite_weight.groupby(lambda t: sectors.get(t, "unknown")).sum()
    n["my_sector"] = by_sector.get(c["sector"], 0)
    before_sat = portfolio_weights(rules, values, cash).drop(core, errors="ignore")
    n["my_sector_before"] = before_sat.groupby(lambda t: sectors.get(t, "unknown")).sum().get(c["sector"], 0)
    n["top_sector"] = (by_sector.idxmax(), by_sector.max())
    if by_sector.max() > s["max_sector_pct"]:
        top = by_sector.idxmax()
        n["problems"].append(f"{top} {danish(by_sector.max(), 1)}% > {s['max_sector_pct']}%")

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

    n["fx_before"], n["fx_after"] = bucket(before_fx), bucket(after_fx)
    n["fx_total_before"], n["fx_total_after"] = values.sum() + cash, after.sum() + cash
    n["fx_mapped"] = mapped
    n["fx_of"] = {t: currency_of(t, rules_plus) for t in (ticker, swap_out) if t}

    # 5. Risk: yearly swing if it replaces the same-role satellite (info only).
    n["swing"] = None
    if swap_out is not None and ticker in prices_dkk.columns:
        lookback = rules["risk"]["lookback_days"]
        swap = swapped()
        returns = returns_from_prices(prices_dkk[list(set(values.index) | set(swap.index))],
                                      today, lookback)
        table_now, swing_now = risk_table(values, returns[values.index])
        table_after, swing_after = risk_table(swap, returns[swap.index])
        n["swing"] = {"now_dkk": swing_now, "after_dkk": swing_after,
                      "now_pct": swing_now / (values.sum() + cash) * 100,
                      "after_pct": swing_after / (swap.sum() + cash) * 100,
                      "out_vol": table_now.loc[swap_out, "own_vol_%"],
                      "out_corr": table_now.loc[swap_out, "corr_with_rest"],
                      "cand_vol": table_after.loc[ticker, "own_vol_%"] if ticker in table_after.index else None,
                      "cand_corr": (table_after.loc[ticker, "corr_with_rest"]
                                    if ticker in table_after.index else None)}
        for name, g in n["groups"].items():
            g["risk_before"] = table_now.reindex(g["tickers"])["risk_%"].fillna(0).sum()
            g["risk_after"] = table_after.reindex(g["tickers"])["risk_%"].fillna(0).sum()
            # Replace mode checks every cap after the swap, the risk cap too.
            if replace and g["risk_limit"] is not None and g["risk_after"] > g["risk_limit"]:
                n["problems"].append(f"{name} {danish(g['risk_after'], 1)}% of swings > {g['risk_limit']}%")

    # Free slot: the explained block shows the risk of ADDING it (with new
    # money), which is what the slot line says. (The one-line summary above
    # keeps its "if swapped for" figure.)
    n["swing_add"] = None
    if n["free_slot"] and ticker in prices_dkk.columns:
        returns = returns_from_prices(prices_dkk[list(after.index)], today, rules["risk"]["lookback_days"])
        table_now, swing_now = risk_table(values, returns[values.index])
        table_add, swing_add = risk_table(after, returns[after.index])
        n["swing_add"] = {"now_dkk": swing_now, "after_dkk": swing_add,
                          "now_pct": swing_now / (values.sum() + cash) * 100,
                          "after_pct": swing_add / (after.sum() + cash) * 100,
                          "cand_vol": table_add.loc[ticker, "own_vol_%"],
                          "cand_corr": table_add.loc[ticker, "corr_with_rest"]}
        for g in n["groups"].values():
            g["risk_before"] = table_now.reindex(g["tickers"])["risk_%"].fillna(0).sum()
            g["risk_after"] = table_add.reindex(g["tickers"])["risk_%"].fillna(0).sum()
    return n


def currency_of(ticker, rules):
    """Short label of a holding's underlying currency ('USD', or 'EUR/GBP' for a mix)."""
    entry = rules["risk"]["currency"].get(ticker)
    if entry is None:
        entry = {t: cur for t, cur in SECURITIES.values()}.get(ticker, "?")
    return entry if isinstance(entry, str) else "/".join(sorted(entry, key=entry.get, reverse=True)[:2])


def fit_check(c, d, rules, values, cash, prices_dkk, today, swap_out=None, replace=False, same_sector=False):
    """What happens to my portfolio if I add this candidate?
    Returns (symbol, short reason, details) - the one-line summary used by
    the scanner table and the dashboard. See fit_numbers for the inputs."""
    n = fit_numbers(c, d, rules, values, cash, prices_dkk, today, swap_out, replace, same_sector)
    return fit_summary(n)


def fit_summary(n):
    """(symbol, short reason, details) from fit_numbers."""
    if n["after"] is None:
        return FAIL, n["slot_text"], ""
    b, a = n["fx_before"], n["fx_after"]
    fx_text = " ".join(f"{k} {danish(b[k], 0)}→{danish(a[k], 0)}" for k in b)
    if a["USD"] > b["USD"] + 0.05:
        fx_text += " · USD ↑"
    if n["type"] == "etf" and not n["fx_mapped"]:
        fx_text += " (ETF at trading currency)"
    risk_text = "swing n/a"
    if n["swing"]:
        w = n["swing"]
        risk_text = (f"swing {danish(w['now_pct'], 1)}%→{danish(w['after_pct'], 1)}% "
                     f"(if swapped for {n['swap_out']})")
    details = f"{n['sector']} {danish(n['my_sector'], 1)}% · {fx_text} · {risk_text}"
    if n["problems"]:
        return FAIL, "; ".join(n["problems"]), details
    return (PASS if n["free_slot"] else INFO), n["slot_text"], details


def fit_explained(n, names, score_of=None):
    """The fit check in plain English: a list of lines
    {"check", "status", "result", "meaning"} plus a one-sentence summary.
    names = {ticker: display name}; score_of = {ticker: score text} (for the slot line)."""
    t = EXPLAIN["fit"]
    pc = lambda x: danish(x, 1) + "%"                              # noqa: E731
    kr = lambda x: danish(x, 0)                                     # noqa: E731
    cand, out = n["ticker"], n["swap_out"]
    A, B = names.get(cand, cand), names.get(out, out) if out else None
    lines = []

    # Slot.
    if n["after"] is None:
        lines.append({"check": t["slot"]["title"], "status": FAIL, "result": n["slot_text"],
                      "meaning": t["slot"]["none"].format(now=pc(n["satellites_now"]), limit=f"{n['sat_limit']}%",
                                                          role=n["role"])})
        return {"lines": lines, "status": FAIL, "summary": "It doesn't fit: there is no slot and nothing to swap."}
    if n["free_slot"]:
        meaning = t["slot"]["free"].format(now=pc(n["satellites_now"]), new=pc(n["new_pct"]),
                                           limit=f"{n['sat_limit']}%")
        lines.append({"check": t["slot"]["title"], "status": PASS, "result": "free slot", "meaning": meaning})
    elif n["replace"]:
        meaning = t["slot"]["swap_picked"].format(out=B, dkk=kr(n["sold_dkk"]), now=pc(n["satellites_now"]),
                                                  limit=f"{n['sat_limit']}%")
        over = n["satellites_now"] > n["sat_limit"]
        lines.append({"check": t["slot"]["title"], "status": WARN if over else PASS,
                      "result": f"replaces {B} · satellites {pc(n['satellites_now'])} (limit {n['sat_limit']}%)",
                      "meaning": meaning + (t["slot"]["over"] if over else "")})
    else:
        meaning = t["slot"]["swap_weakest"].format(now=pc(n["satellites_now"]), limit=f"{n['sat_limit']}%",
                                                   new=pc(n["new_pct"]), out=B, role=n["role"],
                                                   score=(score_of or {}).get(out, "n/a"))
        lines.append({"check": t["slot"]["title"], "status": INFO,
                      "result": f"swap for {B} · satellites {pc(n['satellites_now'])} (limit {n['sat_limit']}%)",
                      "meaning": meaning})

    # Position size.
    if n["type"] == "etf":
        lines.append({"check": t["size"]["title"], "status": PASS,
                      "result": f"{pc(n['weight'])} = {kr(n['bought_dkk'])} DKK (fund: no limit)",
                      "meaning": t["size"]["etf"]})
    else:
        lines.append({"check": t["size"]["title"],
                      "status": FAIL if n["weight"] > n["single_limit"] else PASS,
                      "result": f"{pc(n['weight'])} = {kr(n['bought_dkk'])} DKK (limit {n['single_limit']}%)",
                      "meaning": t["size"]["stock"].format(limit=f"{n['single_limit']}%")})
        others = n["too_big"].drop(cand, errors="ignore")
        if len(others):
            lines[-1]["status"] = FAIL
            lines[-1]["result"] += " · also " + ", ".join(f"{x} {pc(w)}" for x, w in others.items())

    # Group caps (only the groups this swap touches, or that are already over).
    touched = {cand, out}
    for name, g in n["groups"].items():
        if not (touched & set(g["tickers"])) and g["after"] <= g["limit"]:
            continue
        label = name.capitalize()
        result = f"{pc(g['before'])} → {pc(g['after'])} of money (limit {g['limit']}%)"
        status = FAIL if g["after"] > g["limit"] else PASS
        if "risk_after" in g and g["risk_limit"] is not None:
            result += f" · {pc(g['risk_before'])} → {pc(g['risk_after'])} of swings (limit {g['risk_limit']}%)"
            if g["risk_after"] > g["risk_limit"] and status == PASS:
                status = FAIL if n["replace"] else WARN
        lines.append({"check": t["group"]["title"].format(group=label), "status": status, "result": result,
                      "meaning": t["group"]["meaning"].format(group=label, money_limit=f"{g['limit']}%",
                                                              risk_limit=f"{g['risk_limit']}%")})

    # Sector.
    sec, before, after = n["sector"], n["my_sector_before"], n["my_sector"]
    key = "new" if before < 0.05 else "same" if abs(after - before) < 0.05 else "more" if after > before else "less"
    top_name, top_w = n["top_sector"]
    over = top_w > n["sector_limit"]
    result = f"{sec} {pc(before)} → {pc(after)} of holdings (limit {n['sector_limit']}% per sector)"
    if over and top_name != sec:
        result += f" · {top_name} {pc(top_w)}"
    meaning = t["sector"][key].format(sector=sec, before=pc(before))
    if n["same_sector"] and n["replace"]:
        meaning += " " + EXPLAIN["same_sector_swap"]
    lines.append({"check": t["sector"]["title"], "status": FAIL if over else (WARN if key == "more" else PASS),
                  "result": result, "meaning": meaning})

    # Currency.
    b, a = n["fx_before"], n["fx_after"]
    usd_up, usd_down = a["USD"] > b["USD"] + 0.05, a["USD"] < b["USD"] - 0.05
    usd_dkk_before = b["USD"] / 100 * n["fx_total_before"]
    usd_dkk_after = a["USD"] / 100 * n["fx_total_after"]
    change = t["currency"]["up" if usd_up else "down" if usd_down else "same"]
    template = t["currency"]["meaning_add" if n["free_slot"] else "meaning"]
    meaning = template.format(out=B, out_cur=n["fx_of"].get(out, "?"), cand=A,
                              cand_cur=n["fx_of"].get(cand, n["currency"]), change=change,
                              before_dkk=kr(usd_dkk_before * 0.10), after_dkk=kr(usd_dkk_after * 0.10))
    lines.append({"check": t["currency"]["title"], "status": WARN if usd_up else PASS,
                  "result": " · ".join(f"{k} {danish(b[k], 0)}% → {danish(a[k], 0)}%" for k in b),
                  "meaning": meaning})

    # Risk.
    w = explained_swing(n)
    if w is None:
        lines.append({"check": t["risk"]["title"], "status": INFO, "result": "n/a", "meaning": t["risk"]["na"]})
    else:
        diff = w["after_dkk"] - w["now_dkk"]
        why = ""
        if n["free_slot"]:
            why = t["risk"]["why_add"].format(cand=A, cand_vol=pc(w["cand_vol"]), cand_corr=danish(w["cand_corr"]))
        elif w["cand_vol"] is not None:
            why = t["risk"]["why"].format(cand=A, out=B, cand_vol=pc(w["cand_vol"]), out_vol=pc(w["out_vol"]),
                                          cand_corr=danish(w["cand_corr"]), out_corr=danish(w["out_corr"]))
        how = ", if added with new money" if n["free_slot"] else "" if n["replace"] else f", if it replaced {B}"
        lines.append({"check": t["risk"]["title"], "status": WARN if diff > 0 else PASS,
                      "result": (f"typical yearly swing {pc(w['now_pct'])} (±{kr(w['now_dkk'])} DKK) → "
                                 f"{pc(w['after_pct'])} (±{kr(w['after_dkk'])} DKK){how}"),
                      "meaning": t["risk"]["meaning"].format(diff=kr(abs(diff)),
                                                             direction="more" if diff > 0 else "less", why=why)})

    # Trading cost (info).
    fees, fx_cost = trading_cost(n)
    template = t["cost"]["buy_only"] if n["free_slot"] else t["cost"]["meaning"]
    lines.append({"check": t["cost"]["title"], "status": INFO,
                  "result": f"about {kr(fees + fx_cost)} DKK",
                  "meaning": template.format(out=B, cand=A, fees=kr(fees), fx=kr(fx_cost),
                                             dkk=kr(n["bought_dkk"]))})

    status = FAIL if any(x["status"] == FAIL for x in lines) else PASS
    return {"lines": lines, "status": status, "summary": fit_sentence(n, lines, usd_up, usd_down)}


def trading_cost(n):
    """(Nordnet commission, currency exchange) in DKK for the swap."""
    k = {**COSTS, **n.get("costs", {})}

    def commission(ticker, dkk):
        dk = ticker.endswith(".CO")
        pct, low = (k["commission_pct_dk"], k["commission_min_dkk_dk"]) if dk else \
                   (k["commission_pct"], k["commission_min_dkk"])
        return max(low, dkk * pct / 100) if dkk > 0 else 0

    trades = [(n["ticker"], n["bought_dkk"], n["currency"])]
    if not n["free_slot"] and n["swap_out"]:
        out_trading = {t: cur for t, cur in SECURITIES.values()}.get(n["swap_out"], "DKK")
        trades.append((n["swap_out"], n["sold_dkk"], out_trading))
    fees = sum(commission(t, dkk) for t, dkk, _ in trades)
    fx = sum(dkk * k["fx_pct"] / 100 for _, dkk, cur in trades if cur and cur != "DKK")
    return fees, fx


def explained_swing(n):
    """The risk figures the explained block uses: adding it (free slot) or swapping."""
    return n["swing_add"] if n["free_slot"] else n["swing"]


def _and(items):
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def fit_sentence(n, lines, usd_up, usd_down):
    """One sentence weighing the trade-offs."""
    broken = [x["check"].lower() for x in lines if x["status"] == FAIL]
    pros, cons = [], []
    w = explained_swing(n)
    if w:
        (cons if w["after_dkk"] > w["now_dkk"] else pros).append(
            "raises risk" if w["after_dkk"] > w["now_dkk"] else "lowers risk")
    if usd_up:
        cons.append("increases dollar dependence")
    elif usd_down:
        pros.append("lowers dollar dependence")
    if any(x["check"] == EXPLAIN["fit"]["sector"]["title"] and x["status"] == WARN for x in lines):
        cons.append("concentrates a sector you already own")
    if broken:
        text = "breaks your rules (" + ", ".join(broken) + ")" + (", and it " + _and(cons) if cons else "")
        if pros:
            text += "; on the plus side it " + _and(pros)
    else:
        text = "fits your rules" + (" and " + _and(pros) if pros else "") + (", but " + _and(cons) if cons else "")
    return text[0].upper() + text[1:] + "."


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


def factor_percentiles(raw, weights, min_factors):
    """Each factor's percentile rank within the group (0 = worst, 1 = best),
    for the rows with at least min_factors factors. Returns (pct, counts)."""
    counts = raw[list(weights)].apply(pd.to_numeric, errors="coerce").notna().sum(axis=1)
    raw = raw[counts >= min_factors]
    pct = pd.DataFrame(index=raw.index)
    for factor in weights:
        col = pd.to_numeric(raw[factor], errors="coerce")
        n = col.notna().sum()
        ranks = col.rank(ascending=HIGHER_IS_BETTER[factor])   # best gets the highest rank
        pct[factor] = (ranks - 1) / (n - 1) if n > 1 else 0.5
    return pct, counts


def score_role(raw, weights, min_factors):
    """Percentile-rank each factor within the role (0 = worst, 1 = best),
    then the weighted average of the factors that have data.
    Returns (scores, star) - star = some factor missing, weights rescaled.
    Anything with fewer than min_factors factors gets NO score (NaN) and
    is left out of the ranking itself, so it can't shift others' ranks."""
    pct, counts = factor_percentiles(raw, weights, min_factors)
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


def latest_factors():
    """The raw factors and scores of the last scanner run (or None)."""
    files = sorted((OUTPUT_FOLDER / "scanner").glob("factors_????-??-??.csv"))
    if not files:
        return None
    saved = pd.read_csv(files[-1], sep=";", decimal=",", encoding="utf-8-sig", index_col="ticker")
    saved[list(HIGHER_IS_BETTER)] = saved[list(HIGHER_IS_BETTER)].apply(pd.to_numeric, errors="coerce")
    return saved


def head_to_head(ticker, raw, other, role, rules):
    """Candidate vs. one holding, measured with the same ruler: both are
    percentile-ranked in ONE pool (the last scanner run's names in this
    role + the candidate). The holding's factors come from that run.
    Returns None without a scanner run, else a dict with per-factor values,
    percentiles and the weighted points each factor gives."""
    saved = latest_factors()
    if saved is None:
        return None
    s = rules["scanner"]
    weights = s["weights"][role]
    cols = list(HIGHER_IS_BETTER)
    pool = saved[(saved["role"] == role) & (saved.index != ticker)][cols].copy()
    pool.loc[ticker] = pd.Series(raw)[cols]
    if other in saved.index and other not in pool.index:
        pool.loc[other] = saved.loc[other, cols]           # e.g. a holding I'd score in another role
    pct, _ = factor_percentiles(pool, weights, s["min_factors"])
    scores, stars = score_role(pool, weights, s["min_factors"])

    def side(t):
        return {"raw": pool.loc[t, list(weights)].to_dict() if t in pool.index else dict.fromkeys(weights),
                "pct": pct.loc[t].to_dict() if t in pct.index else dict.fromkeys(weights),
                "score": scores.get(t, float("nan")), "star": bool(stars.get(t, False))}

    out = {"weights": weights, "cand": side(ticker), "other": side(other),
           "scanner_score": saved["score"].get(other, float("nan")) if other in saved.index else float("nan"),
           "need": s["min_score_gap"]}
    out["points"] = {f: weights[f] * (out["cand"]["pct"][f] - out["other"]["pct"][f])
                     for f in weights
                     if not pd.isna(out["cand"]["pct"][f]) and not pd.isna(out["other"]["pct"][f])}
    return out


def _fmt_factor(f, v):
    meta = EXPLAIN["factors"][f]
    if v is None or pd.isna(v):
        return "n/a"
    if f == "low_correlation":
        return danish(v, 2)
    text = danish(v, 1)
    if meta["signed"] and v > 0:
        text = "+" + text
    text += meta["unit"]
    if f == "earnings_growth" and abs(v) >= EARNINGS_GROWTH_CAP:
        text += " (capped)"
    return text


def comparison_rows(h2h, names, cand, other):
    """One row per scoring factor: what it measures, both values, the
    winner and a one-line translation."""
    A, B = names.get(cand, cand), names.get(other, other)
    rows = []
    for f, weight in h2h["weights"].items():
        meta = EXPLAIN["factors"][f]
        a, b = h2h["cand"]["raw"].get(f), h2h["other"]["raw"].get(f)
        winner = None
        if not (a is None or pd.isna(a) or b is None or pd.isna(b)) and a != b:
            winner = cand if (a > b) == HIGHER_IS_BETTER[f] else other
        line = meta["line"].format(A=A, B=B, a=_fmt_factor(f, a), b=_fmt_factor(f, b))
        if winner is None:
            line += ": " + ("no data for at least one of them." if a is None or b is None or pd.isna(a)
                            or pd.isna(b) else "a tie.")
        else:
            win_value = a if winner == cand else b
            key = "win_neg" if "win_neg" in meta and win_value < 0 else "win"
            line += ": " + meta[key].format(W=names.get(winner, winner))
        rows.append({"factor": f, "label": meta["label"], "term": meta["term"], "measures": meta["measures"],
                     "weight": weight, "cand": _fmt_factor(f, a), "other": _fmt_factor(f, b),
                     "winner": winner, "line": line})
    return rows


def verdict_parts(h2h, names, cand, other, rules, story, thesis_state):
    """The three-part verdict: score, why (the 2-3 deciding factors), and
    what the numbers can't tell you (story checks and thesis).
    story = weekly_check.story_status(...) entry for the holding (or None);
    thesis_state = (has thesis, has kill rule) for the candidate."""
    v = EXPLAIN["verdict"]
    A, B = names.get(cand, cand), names.get(other, other)
    a, b = h2h["cand"]["score"], h2h["other"]["score"]
    need = h2h["need"]
    parts = {"score": "", "why": [], "story": []}
    if pd.isna(a) or pd.isna(b):
        who = " and ".join(x for x, s in ((A, a), (B, b)) if pd.isna(s))
        parts["score"] = v["unscored"].format(who=who)
    else:
        gap = a - b
        key = "beats" if gap >= need else "close" if gap > 0 else "loses"
        parts["score"] = v[key].format(A=A, B=B, a=danish(a, 2), b=danish(b, 2), gap=danish(gap, 2),
                                       need=danish(need, 2))
        scan = h2h["scanner_score"]
        if not pd.isna(scan) and abs(scan - b) >= 0.005:
            parts["score"] += f" ({B} scored {danish(scan, 2)} in the last scanner run, without {A} in the group.)"
        decisive = sorted(h2h["points"].items(), key=lambda x: abs(x[1]), reverse=True)
        decisive = [(f, p) for f, p in decisive if abs(p) >= 0.02][:3]
        for f, p in decisive:
            w_t, l_t = (cand, other) if p > 0 else (other, cand)
            side = lambda t: h2h["cand"] if t == cand else h2h["other"]          # noqa: E731
            parts["why"].append(v["why"].format(
                label=EXPLAIN["factors"][f]["label"], weight=f"{h2h['weights'][f] * 100:.0f}%",
                W=names.get(w_t, w_t), wp=danish(side(w_t)["pct"][f], 2), lp=danish(side(l_t)["pct"][f], 2),
                pts=danish(abs(p), 2)))

    # What the numbers can't tell you.
    if story is None or story["total"] == 0:
        parts["story"].append(v["story_none"].format(name=B))
    elif story["red"]:
        parts["story"].append(v["story_red"].format(name=B, red=story["red"], total=story["total"]))
    elif story["all_green"]:
        parts["story"].append(v["story_intact"].format(name=B, green=story["green"], total=story["total"]))
    else:
        parts["story"].append(v["story_mixed"].format(name=B, green=story["green"], total=story["total"],
                                                      other=story["total"] - story["green"]))
    has_thesis, has_kill = thesis_state
    if has_thesis and has_kill:
        parts["story"].append(v["thesis_ok"].format(name=A))
    elif has_thesis or has_kill:
        parts["story"].append(v["thesis_partial"].format(name=A, have="thesis" if has_thesis else "kill rule",
                                                         missing="kill rule" if has_thesis else "thesis"))
    else:
        parts["story"].append(v["thesis_none"].format(name=A))
    return parts


def score_text(score, star, long=False):
    if pd.isna(score):
        return "not scored (ETF / missing data)" if long else "not scored"
    return danish(score, 2) + ("*" if star else "")


def eligible_candidates(table, role):
    """Candidates of a role that passed the gates and the fit check, best first."""
    ok = table[(table["role"] == role) & table["verdict"].str.startswith("pass")
               & (table["Fit"] != FAIL) & table["score"].notna()]
    return ok.sort_values("score", ascending=False)
