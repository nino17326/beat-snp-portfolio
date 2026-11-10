# ============================================================
#  make_demo_data.py  -  builds the synthetic files in demo_data\
#
#      py tools\make_demo_data.py
#
#  A MADE-UP portfolio (not anyone's real account): CSPX as the core
#  plus 5 stocks, about 18 months, with deposits, buys, one sale and
#  dividends. Prices, exchange rates and dividends are REAL (from Yahoo
#  Finance), so the numbers behave like a real account; the trades are
#  invented. It writes the three Nordnet exports in Nordnet's own
#  format (UTF-16, tab-separated, Danish decimal comma):
#    Daglig udvikling ... .csv, transactions-and-notes-export.csv, Aktietabel ... .csv
#  The daily account values are computed from the same transactions and
#  prices the tracker uses, so the demo reconciles just like real data.
# ============================================================

import sys
import time
from pathlib import Path

import pandas as pd
import yfinance as yf

OUT = Path(__file__).resolve().parent.parent / "demo_data"
FIRST, LAST = pd.Timestamp("2025-05-01"), pd.Timestamp("2026-11-06")
ACCOUNT = "00000000"                       # placeholder account number

# ISIN: (Yahoo ticker, currency, name as Nordnet writes it, market, dividend tax withheld)
SECURITIES = {
    "IE00B5BMR087": ("SXR8.DE", "EUR", "iShares Core S&P 500 UCITS ETF USD (Acc)", "Xetra", 0.0),
    "US5949181045": ("MSFT", "USD", "Microsoft Corp", "Nasdaq", 0.15),
    "NL0010273215": ("ASML.AS", "EUR", "ASML Holding", "Euronext Amsterdam", 0.15),
    "DE0007164600": ("SAP.DE", "EUR", "SAP SE", "Xetra", 0.26375),
    "DK0060079531": ("DSV.CO", "DKK", "DSV A/S", "Nasdaq Copenhagen A/S", 0.0),
    "US6541061031": ("NKE", "USD", "Nike Inc B", "NYSE", 0.15),
}
FX = {"EUR": "EURDKK=X", "USD": "USDDKK=X"}
CORE = "IE00B5BMR087"

# The plan: (day, what, ISIN, number of shares - or "all" for a sale)
PLAN = [
    ("2025-05-05", "buy", CORE, 6),
    ("2025-05-05", "buy", "US5949181045", 2),
    ("2025-05-06", "buy", "NL0010273215", 1),
    ("2025-06-03", "buy", "US6541061031", 10),
    ("2025-07-02", "buy", "DE0007164600", 2),
    ("2025-08-04", "buy", "DK0060079531", 2),
    ("2026-02-03", "buy", "NL0010273215", 1),
    ("2026-04-15", "sell", "US6541061031", "all"),       # the one sale
    ("2026-04-16", "buy", CORE, 2),                      # proceeds back into the core
]
STOCK_MONTHS = {"2025-06", "2025-07", "2025-08", "2026-02"}   # no core buy those months
MONTHLY_DEPOSIT = 6000.0


def deposits():
    """40.000 DKK to start, then MONTHLY_DEPOSIT on the first weekday of each month."""
    out = [(pd.Timestamp("2025-05-02"), 40000.0)]
    for month in pd.period_range("2025-06", "2026-11", freq="M"):
        day = pd.Timestamp(month.start_time)
        while day.day_of_week >= 5:
            day += pd.Timedelta(days=1)
        out.append((day, MONTHLY_DEPOSIT))
    return out


def monthly_core_buys(deps):
    """1 share of CSPX the weekday after each monthly deposit (not in stock months)."""
    plan = []
    for day, _ in deps[1:]:
        if f"{day:%Y-%m}" in STOCK_MONTHS:
            continue
        buy = day + pd.Timedelta(days=1)
        while buy.day_of_week >= 5:
            buy += pd.Timedelta(days=1)
        plan.append((f"{buy:%Y-%m-%d}", "buy", CORE, 1))
    return plan


def download(ticker):
    """Daily closes (NOT dividend-adjusted, like the tracker) on every calendar day."""
    print(f"Downloading {ticker} ...")
    h = yf.Ticker(ticker).history(start=FIRST - pd.Timedelta(days=10), end=LAST + pd.Timedelta(days=1),
                                  auto_adjust=False)
    if h.empty:
        raise SystemExit(f"No prices for {ticker} - check the internet connection")
    close = h["Close"]
    close.index = close.index.tz_localize(None).normalize()
    days = pd.date_range(FIRST, LAST, freq="D")
    time.sleep(0.5)
    return close.reindex(close.index.union(days)).ffill().reindex(days)


def dividends(ticker):
    d = yf.Ticker(ticker).dividends
    if d.empty:
        return d
    d.index = d.index.tz_localize(None).normalize()
    time.sleep(0.5)
    return d[(d.index > FIRST) & (d.index <= LAST - pd.Timedelta(days=14))]


def num(x, decimals=2):
    """Danish number without thousands separators: 1234.5 -> '1234,5'."""
    text = f"{round(x, decimals):.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text.replace(".", ",")


def write(path, header, rows):
    lines = ["\t".join(header)] + ["\t".join(str(v) for v in r) for r in rows]
    with open(path, "w", encoding="utf-16", newline="") as f:
        f.write("\r\n".join(lines) + "\r\n")
    print(f"Wrote {path.name} ({len(rows)} rows)")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    OUT.mkdir(exist_ok=True)
    fx = {"DKK": None, **{c: download(t) for c, t in FX.items()}}
    price = {isin: download(s[0]) for isin, s in SECURITIES.items()}

    def rate(currency, day):
        return 1.0 if currency == "DKK" else float(fx[currency][day])

    deps = deposits()
    plan = PLAN + monthly_core_buys(deps)

    # --- Build the events in time order ---------------------------
    events = [(d, 0, "deposit", None, amount) for d, amount in deps]
    events += [(pd.Timestamp(d), 1 if what == "sell" else 2, what, isin, amount) for d, what, isin, amount in plan]
    events.sort(key=lambda e: (e[0], e[1]))

    holdings = {isin: {"shares": 0, "cost_local": 0.0, "cost_dkk": 0.0} for isin in SECURITIES}
    cash, rows, next_id = 0.0, [], 2500000000

    def add_row(day, kind, isin, antal, kurs, fee, amount, local_value, result, total, fx_rate, text):
        nonlocal cash, next_id
        cash += amount
        next_id += 7
        s = SECURITIES.get(isin)
        local = s[1] if s else ""
        rows.append({"day": day, "Id": next_id, "kind": kind, "cells": [
            next_id, f"{day:%Y-%m-%d}", f"{day:%Y-%m-%d}", f"{day:%Y-%m-%d}", ACCOUNT, kind,
            s[2] if s else "", isin or "", antal, kurs, "", num(fee) if fee else "", "DKK" if fee else "",
            num(amount), "DKK", num(local_value) if local_value != "" else "", local if local_value != "" else "",
            num(result) if result != "" else "", local if result != "" else "", total, num(cash),
            num(fx_rate, 4) if fx_rate != "" else "", text, "", "", next_id,
            num(fee) if fee else "", "DKK" if fee else "", "", ""]})
        if cash < -0.005:
            raise SystemExit(f"Cash went negative on {day:%Y-%m-%d} - adjust the PLAN")

    for day, _, what, isin, amount in events:
        if what == "deposit":
            add_row(day, "INDSÆTTELSE", None, "", "", 0, amount, "", "", "", "", "Deposit")
            continue
        ticker, currency = SECURITIES[isin][0], SECURITIES[isin][1]
        kurs, r = float(price[isin][day]), rate(currency, day)
        h = holdings[isin]
        if what == "buy":
            antal = int(amount)
            gross = antal * kurs * r
            fee = max(25.0, round(gross * 0.0015, 2))
            h["shares"] += antal
            h["cost_local"] += antal * kurs
            h["cost_dkk"] += gross + fee
            add_row(day, "KØBT", isin, antal, num(kurs, 4), fee, -(gross + fee), antal * kurs, "",
                    h["shares"], "" if currency == "DKK" else r, "")
        else:
            antal = h["shares"]
            gross = antal * kurs * r
            fee = max(25.0, round(gross * 0.0015, 2))
            result = antal * kurs - h["cost_local"]
            h.update(shares=0, cost_local=0.0, cost_dkk=0.0)
            add_row(day, "SOLGT", isin, antal, num(kurs, 4), fee, gross - fee, "", result, 0,
                    "" if currency == "DKK" else r, "")

    # --- Dividends: real ex-dates and amounts, paid 14 days later ---
    trades = pd.DataFrame([(r["day"], r["cells"][7], r["cells"][8] if r["kind"] == "KØBT" else -r["cells"][8])
                           for r in rows if r["kind"] in ("KØBT", "SOLGT")], columns=["day", "isin", "change"])
    dividend_events = []
    for isin, (ticker, currency, name, _, tax_rate) in SECURITIES.items():
        for ex_day, per_share in dividends(ticker).items():
            held = trades[(trades["isin"] == isin) & (trades["day"] < ex_day)]["change"].sum()
            if held > 0:
                dividend_events.append((ex_day + pd.Timedelta(days=14), isin, int(held), float(per_share), tax_rate))
    for pay_day, isin, held, per_share, tax_rate in dividend_events:
        ticker, currency = SECURITIES[isin][0], SECURITIES[isin][1]
        gross = held * per_share * rate(currency, pay_day)
        text = f"Udbytte {ticker} {per_share:.2f} {currency}/aktie"
        rows.append({"day": pay_day, "Id": None, "kind": "UDBYTTE", "isin": isin, "held": held,
                     "per_share": per_share, "amount": gross, "text": text})
        if tax_rate:
            rows.append({"day": pay_day, "Id": None, "kind": "UDBYTTESKAT", "isin": isin, "held": held,
                         "per_share": 0, "amount": -gross * tax_rate, "text": text})

    # Re-number everything in time order and recompute the running cash (Saldo).
    order = {"INDSÆTTELSE": 0, "SOLGT": 1, "KØBT": 2, "UDBYTTE": 3, "UDBYTTESKAT": 4}
    rows.sort(key=lambda r: (r["day"], order[r["kind"]], r["Id"] or 0))
    cash, next_id, final = 0.0, 2500000000, []
    for r in rows:
        next_id += 7
        if r["kind"] in ("UDBYTTE", "UDBYTTESKAT"):
            r["amount"] = round(r["amount"], 2)              # Nordnet books whole øre
            cash += r["amount"]
            cells = [next_id, f"{r['day']:%Y-%m-%d}", f"{r['day'] - pd.Timedelta(days=14):%Y-%m-%d}",
                     f"{r['day']:%Y-%m-%d}", ACCOUNT, r["kind"], SECURITIES[r["isin"]][2], r["isin"], r["held"],
                     num(r["per_share"], 4), "", "", "", num(r["amount"]), "DKK", "", "", "", "", 0, num(cash),
                     "", r["text"], "", "", next_id, "", "", "", ""]
        else:
            cash += float(r["cells"][13].replace(",", "."))
            cells = list(r["cells"])
            cells[0] = cells[25] = next_id
            cells[20] = num(cash)
        final.append((r["day"], r["kind"], cells))

    header = ["Id", "Bogføringsdag", "Handelsdag", "Valørdag", "Depot", "Transaktionstype", "Værdipapirer",
              "ISIN", "Antal", "Kurs", "Rente", "Samlede afgifter", "Valuta", "Beløb", "Valuta",
              "Indkøbsværdi", "Valuta", "Resultat", "Valuta", "Totalt antal", "Saldo", "Vekslingskurs",
              "Transaktionstekst", "Makuleringsdato", "Notanummer", "Verifikationsnummer", "Kurtage", "Valuta",
              "Middelkurs", "Oprindelig rente"]
    # Nordnet lists the newest transaction first.
    write(OUT / "transactions-and-notes-export.csv", header, [c for _, _, c in reversed(final)])

    # --- Daily account value (what Nordnet's 'Daglig udvikling' shows) ---
    days = pd.date_range(FIRST, LAST, freq="D")
    shares = {isin: pd.Series(0.0, index=days) for isin in SECURITIES}
    cash_day, dep_day = pd.Series(0.0, index=days), pd.Series(0.0, index=days)
    for day, kind, cells in final:
        amount = float(str(cells[13]).replace(",", "."))
        cash_day[day:] += amount
        if kind == "INDSÆTTELSE":
            dep_day[day:] += amount
        elif kind in ("KØBT", "SOLGT"):
            shares[cells[7]][day:] += cells[8] if kind == "KØBT" else -cells[8]
    value = cash_day.copy()
    for isin, (ticker, currency, *_rest) in SECURITIES.items():
        dkk = price[isin] * (1.0 if currency == "DKK" else fx[currency])
        value += shares[isin] * dkk
    new_money = dep_day.diff().fillna(dep_day)
    gain = value - value.shift(1).fillna(0) - new_money
    base = value.shift(1).fillna(0) + new_money.clip(lower=0)
    daily_pct = (gain / base).where(base > 0, 0.0)
    total_pct = (1 + daily_pct).cumprod() - 1
    hist = [[f"{d:%Y-%m-%d}", f"{value[d]:.2f}".replace(".", ","), f"{daily_pct[d] * 100:.2f}%".replace(".", ","),
             f"{total_pct[d] * 100:.2f}%".replace(".", ","), f"{gain[d]:.2f}".replace(".", ","),
             f"{gain[:d].sum():.2f}".replace(".", ","), f"{dep_day[d]:.2f}".replace(".", ",")] for d in days]
    write(OUT / f"Daglig udvikling eksporteret, demo, {FIRST:%Y-%m-%d}-{LAST:%Y-%m-%d}.csv",
          ["Dato", "Samlet værdi (DKK)", "Dagligt afkast (%)", "Samlet afkast (%)", "Dagligt afkast (DKK)",
           "Samlet afkast (DKK)", "Nettoindbetalinger (DKK)"], hist)

    # --- Holdings on the last day (Nordnet's 'Aktietabel') ---------
    table = []
    for isin, (ticker, currency, name, market, _) in SECURITIES.items():
        h = holdings[isin]
        if h["shares"] <= 0:
            continue
        last, prev = float(price[isin][LAST]), float(price[isin][LAST - pd.Timedelta(days=1)])
        r = rate(currency, LAST)
        value_local, value_dkk = h["shares"] * last, h["shares"] * last * r
        gak = h["cost_local"] / h["shares"]
        table.append([name, currency, h["shares"], num(gak, 4), num((last / prev - 1) * 100),
                      num(last, 4), num(value_dkk * 0.8, 4), num(value_local, 4), num(value_dkk, 4),
                      num((last / gak - 1) * 100), num(value_dkk - h["cost_dkk"], 4), isin, market])
    write(OUT / f"Aktietabel, demo, {LAST.day}.{LAST.month}.{LAST.year}.csv",
          ["Navn", "Valuta", "Antal", "GAK", "I dag %", "Seneste kurs", "Belåningsværdi DKK", "Værdi",
           "Værdi DKK", "Ureal.afkast %", "Afkast DKK", "ISIN", "Market"], table)

    print(f"\nLast day {LAST:%Y-%m-%d}: value {value[LAST]:,.0f} DKK, cash {cash_day[LAST]:,.0f} DKK, "
          f"net deposits {dep_day[LAST]:,.0f} DKK, {len(dividend_events)} dividends")
    for isin, h in holdings.items():
        print(f"  {SECURITIES[isin][0]:<9} {h['shares']:>4} shares, last close {float(price[isin][LAST]):,.2f} "
              f"{SECURITIES[isin][1]}")


if __name__ == "__main__":
    main()
