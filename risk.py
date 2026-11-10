# ============================================================
#  risk.py  -  how risky is my portfolio, and where does the risk sit?
#
#   1. Currency exposure: which currencies my money really sits in.
#   2. Correlation + risk share: how holdings move together, and
#      which ones cause the portfolio's ups and downs.
#   3. Stress tests: estimated loss in a few bad scenarios.
#   4. Drawdown: my account's biggest fall from a peak.
#  Settings live in the [risk] part of rules.toml.
# ============================================================

import json
import sys
import tomllib

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

from portfolio import danish
from shadow_portfolio import read_export, combine, account_returns, signed
from positions import load_account, SECURITIES, OUTPUT_FOLDER
from settings import RULES_FILE, HISTORY_FILES, data_files     # rules.toml is in the data folder

INFO, OK, WARN = "ℹ️", "✅", "⚠️"


def holdings_today(shares, prices, day):
    """DKK value of each holding I own on `day`, by Yahoo ticker."""
    values = (shares.loc[day] * prices.loc[day]).rename(lambda isin: SECURITIES[isin][0])
    return values[values > 0]


# --- 1. Currency exposure -------------------------------------

def currency_split(ticker, rules):
    """{currency: fraction} for one holding, from rules.toml.
    Falls back to the trading currency if the ticker isn't listed."""
    entry = rules["risk"]["currency"].get(ticker)
    if entry is None:
        entry = {t: c for t, c in SECURITIES.values()}[ticker]
    if isinstance(entry, str):
        return {entry: 1.0}
    total = sum(entry.values())
    return {currency: share / total for currency, share in entry.items()}


def currency_exposure(values, cash, rules):
    """% of my whole account (holdings + cash) per underlying currency."""
    exposure = {"DKK": cash}
    for ticker, value in values.items():
        for currency, fraction in currency_split(ticker, rules).items():
            exposure[currency] = exposure.get(currency, 0) + value * fraction
    exposure = pd.Series(exposure)
    return (exposure / exposure.sum() * 100).sort_values(ascending=False)


def currency_summary(exposure):
    """USD, EUR, DKK and everything else lumped together as 'other'."""
    main = {c: exposure.get(c, 0.0) for c in ["USD", "EUR", "DKK"]}
    main["other"] = exposure.sum() - sum(main.values())
    return " · ".join(f"{c} {danish(p, 0)}%" for c, p in main.items())


# --- 2. Correlation and risk share ----------------------------

def daily_returns(prices, tickers, day, lookback_days):
    """Daily % changes in DKK over the lookback period, one column per ticker."""
    p = prices.rename(columns=lambda isin: SECURITIES[isin][0])[list(tickers)]
    return returns_from_prices(p, day, lookback_days)


def returns_from_prices(p, day, lookback_days):
    """Daily % changes of a DKK price table whose columns are tickers.
    (Also used by scanner.py, for candidates I don't own.)"""
    p = p.loc[day - pd.Timedelta(days=lookback_days): day]
    p = p[p.index.dayofweek < 5]          # weekdays only - no trading at weekends
    return p.pct_change().dropna()


def risk_table(values, returns):
    """For each holding: share of money, share of risk, its own yearly
    volatility, and how much it moves with the rest of the portfolio."""
    x = values[returns.columns]               # DKK in each holding
    cov = returns.cov() * 252                 # how holdings swing together, per year
    portfolio_var = x @ cov @ x               # the portfolio's total swing (squared)

    # Risk share = how much of the total swing each holding causes.
    # It counts both its own swing AND how it moves with the others.
    risk_share = x * (cov @ x) / portfolio_var * 100

    corr_with_rest = {}
    for t in returns.columns:
        rest = returns.drop(columns=t) @ x.drop(t)     # daily DKK move of everything else
        corr_with_rest[t] = returns[t].corr(rest)

    table = pd.DataFrame({
        "money_%": x / x.sum() * 100,
        "risk_%": risk_share,
        "own_vol_%": np.sqrt(np.diag(cov)) * 100,
        "corr_with_rest": pd.Series(corr_with_rest),
    }).sort_values("risk_%", ascending=False)
    return table, np.sqrt(portfolio_var)      # second value: yearly swing in DKK


def semis_risk_share(table, rules):
    semis = [t for t in rules["groups"]["semiconductors"]["tickers"] if t in table.index]
    return table.loc[semis, "risk_%"].sum(), table.loc[semis, "money_%"].sum()


# --- 3. Stress tests ------------------------------------------

def stress_tests(values, cash, returns, rules):
    """Estimated loss in DKK and % of the whole account per scenario."""
    total = values.sum() + cash
    results = []
    for s in rules["risk"]["scenarios"]:
        shock = s["shock_pct"] / 100
        if s["kind"] == "group":
            tickers = rules["groups"][s["group"]]["tickers"]
            loss = values.reindex(tickers).fillna(0).sum() * shock
        elif s["kind"] == "market":
            market = returns[s["market"]]
            # beta: if the market moves 1%, this holding typically moves beta %.
            beta = returns.apply(lambda r: r.cov(market) / market.var())
            loss = (values[returns.columns] * beta * shock).sum()
        elif s["kind"] == "currency":
            exposed = sum(v * currency_split(t, rules).get(s["currency"], 0) for t, v in values.items())
            loss = exposed * shock
        else:
            print(f"Unknown scenario kind '{s['kind']}' in rules.toml - skipped")
            continue
        results.append((s["name"], loss, loss / total * 100))
    return results


# --- 4. Drawdown ----------------------------------------------

def drawdown():
    """Biggest fall from a peak, using the time-weighted growth index
    (so deposits and withdrawals don't count as rises or falls)."""
    files = data_files(HISTORY_FILES)
    h = combine([read_export(f) for f in files]).set_index("date")
    new_money = h["deposits"].diff().fillna(h["deposits"])
    growth = (1 + account_returns(h["value"], new_money)).cumprod()

    peak = growth.cummax()                        # highest point so far, each day
    below_peak = (growth / peak - 1) * 100        # % below that peak

    worst_day = below_peak.idxmin()
    return {
        "worst_pct": below_peak.min(),
        "worst_peak_day": growth[:worst_day].idxmax(),
        "worst_day": worst_day,
        "now_pct": below_peak.iloc[-1],
        "now_peak_day": growth.idxmax() if below_peak.iloc[-1] < 0 else h.index[-1],
        "data_until": h.index[-1],
    }


# --- Summary for weekly_check.py ------------------------------

def risk_numbers(rules, shares, cash, prices, today):
    """The risk figures as numbers (also used by weekly_check.py's actions)."""
    values = holdings_today(shares, prices, today)
    returns = daily_returns(prices, values.index, today, rules["risk"]["lookback_days"])
    table, swing = risk_table(values, returns)
    semis_risk, semis_money = semis_risk_share(table, rules)
    return {"values": values, "cash": cash[today], "returns": returns, "table": table,
            "semis_risk": semis_risk, "semis_money": semis_money}


def semis_risk_after(rules, values_after, returns):
    """Semis' share of the swings if my holdings looked like values_after."""
    held = values_after[values_after > 0]
    table, _ = risk_table(held, returns[held.index])
    return semis_risk_share(table, rules)[0]


def summary_lines(rules, shares, cash, prices, today):
    n = risk_numbers(rules, shares, cash, prices, today)
    values, cash_now, returns, table = n["values"], n["cash"], n["returns"], n["table"]
    semis_risk, semis_money = n["semis_risk"], n["semis_money"]
    limit = rules["risk"]["semis_max_risk_share"]
    dd = drawdown()

    stress = " · ".join(f"{name} {danish(loss, 0)} DKK ({danish(pct, 1)}%)"
                        for name, loss, pct in stress_tests(values, cash_now, returns, rules))
    mark = OK if semis_risk <= limit else WARN
    return [
        "## Risk",
        f"- {INFO} Currency (incl. cash): {currency_summary(currency_exposure(values, cash_now, rules))}",
        f"- {mark} Semis cause {danish(semis_risk, 0)}% of swings (max {limit}%) "
        f"with {danish(semis_money, 0)}% of the money",
        f"- {INFO} Stress: {stress}",
        f"- {INFO} Drawdown: now {danish(dd['now_pct'], 1)}% below peak ({dd['now_peak_day']:%d-%m-%Y}); "
        f"worst {danish(dd['worst_pct'], 1)}% ({dd['worst_peak_day']:%d-%m-%Y} -> {dd['worst_day']:%d-%m-%Y})",
    ]


# --- Full report ----------------------------------------------

def draw_chart(table, save_to):
    t = table.sort_values("money_%")
    y = np.arange(len(t))
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.barh(y + 0.2, t["money_%"], height=0.4, label="Share of money", color="grey")
    ax.barh(y - 0.2, t["risk_%"], height=0.4, label="Share of risk (swings)", color="tab:red")
    ax.set_yticks(y)
    ax.set_yticklabels(t.index)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: danish(v, 0) + "%"))
    ax.set_title("Each holding's share of risk vs. share of money")
    ax.legend(loc="lower right")
    ax.grid(axis="x", alpha=0.3)
    fig.tight_layout()
    fig.savefig(save_to, dpi=150)
    print(f"\nChart saved to: {save_to}")
    plt.show()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    with open(RULES_FILE, "rb") as f:
        rules = tomllib.load(f)
    today = pd.Timestamp.today().normalize()
    t, shares, cash, prices = load_account(last_day=today)

    values = holdings_today(shares, prices, today)
    cash_now = cash[today]
    total = values.sum() + cash_now
    lookback = rules["risk"]["lookback_days"]
    returns = daily_returns(prices, values.index, today, lookback)

    print(f"\n# Risk check {today:%Y-%m-%d} - account {danish(total, 0)} DKK "
          f"(holdings {danish(values.sum(), 0)} + cash {danish(cash_now, 0)})")

    print("\n## 1. Currency exposure (underlying assets, incl. cash)")
    for currency, pct in currency_exposure(values, cash_now, rules).items():
        print(f"  {currency:<5}{danish(pct, 1):>6}%")

    print(f"\n## 2. Correlation (last {lookback} days, daily returns in DKK)")
    corr = returns.corr()
    print("  " + " " * 11 + "".join(f"{c[:9]:>10}" for c in corr.columns))
    for name, row in corr.iterrows():
        print(f"  {name:<11}" + "".join(f"{danish(v):>10}" for v in row))

    table, yearly_swing = risk_table(values, returns)
    print("\n## Risk share")
    print(f"  {'':<11}{'money':>8}{'risk':>8}{'own vol/yr':>12}{'moves with rest':>17}")
    for name, r in table.iterrows():
        print(f"  {name:<11}{danish(r['money_%'], 1):>7}%{danish(r['risk_%'], 1):>7}%"
              f"{danish(r['own_vol_%'], 0):>11}%{danish(r['corr_with_rest']):>17}")
    semis_risk, semis_money = semis_risk_share(table, rules)
    limit = rules["risk"]["semis_max_risk_share"]
    mark = OK if semis_risk <= limit else WARN
    print(f"  {mark} Semis combined: {danish(semis_risk, 1)}% of the risk with "
          f"{danish(semis_money, 1)}% of the money (max {limit}%)")
    print(f"  Typical yearly swing of the account: about +/- {danish(yearly_swing, 0)} DKK "
          f"({danish(yearly_swing / total * 100, 1)}%)")

    print("\n## 3. Stress tests (instant shock, estimated)")
    for name, loss, pct in stress_tests(values, cash_now, returns, rules):
        print(f"  {INFO} {name:<20}{danish(loss, 0):>10} DKK {signed(pct, 1):>7}%")

    dd = drawdown()
    print(f"\n## 4. Drawdown (time-weighted, Nordnet data up to {dd['data_until']:%d-%m-%Y})")
    print(f"  {INFO} Worst so far: {danish(dd['worst_pct'], 1)}% "
          f"(peak {dd['worst_peak_day']:%d-%m-%Y} -> bottom {dd['worst_day']:%d-%m-%Y})")
    print(f"  {INFO} Today: {danish(dd['now_pct'], 1)}% below the peak of {dd['now_peak_day']:%d-%m-%Y}")

    OUTPUT_FOLDER.mkdir(exist_ok=True)
    # Everything above, for dashboard.py (so it never recalculates it).
    summary = {
        "date": f"{today:%Y-%m-%d}", "total": float(total),
        "holdings": float(values.sum()), "cash": float(cash_now),
        "currency": {c: float(p) for c, p in currency_exposure(values, cash_now, rules).items()},
        "per_holding": [{"ticker": tk, "money_pct": float(r["money_%"]), "risk_pct": float(r["risk_%"]),
                         "own_vol_pct": float(r["own_vol_%"]), "corr_with_rest": float(r["corr_with_rest"])}
                        for tk, r in table.iterrows()],
        "semis": {"risk_pct": float(semis_risk), "money_pct": float(semis_money), "limit": limit},
        "yearly_swing_dkk": float(yearly_swing), "yearly_swing_pct": float(yearly_swing / total * 100),
        "stress": [{"name": n, "loss_dkk": float(l), "loss_pct": float(p)}
                   for n, l, p in stress_tests(values, cash_now, returns, rules)],
        "drawdown": {k: (f"{v:%Y-%m-%d}" if isinstance(v, pd.Timestamp) else float(v)) for k, v in dd.items()},
    }
    (OUTPUT_FOLDER / "risk_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    draw_chart(table, OUTPUT_FOLDER / "risk_vs_money.png")


if __name__ == "__main__":
    main()
