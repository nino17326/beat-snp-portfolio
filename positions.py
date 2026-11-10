# ============================================================
#  positions.py  -  rebuild my account from the transaction list
#
#  PART 1 (this version):
#   1. Reads Nordnet's transaction export.
#   2. Rebuilds how many shares of each stock I held on every day,
#      and how much cash I had.
#   3. Downloads daily prices + exchange rates and values every
#      position in DKK on every day.
#   4. Compares my rebuilt total (positions + cash) with Nordnet's
#      own "Samlet værdi" to check that the rebuild is right.
#
#  Needs internet (for prices). It never changes the CSV files.
# ============================================================

import pandas as pd

from portfolio import danish
from shadow_portfolio import read_export, combine, download_close
# Paths and file patterns live in settings.py (see BEAT_SNP_DATA_DIR).
# OUTPUT_FOLDER and DATA_FOLDER are re-exported here for the other scripts.
from settings import (DATA_FOLDER, OUTPUT_FOLDER, HISTORY_FILES, TRANSACTIONS_FILES,
                      data_files, newest_data_file, load_rules)


# --- Settings -------------------------------------------------
# Which Yahoo Finance ticker belongs to which security (matched by ISIN,
# the worldwide ID number), and which currency it trades in. This is
# personal (it lists what I own), so it lives in rules.toml:
#   [securities]
#   "IE00B5BMR087" = { ticker = "SXR8.DE", currency = "EUR" }
# If you buy a new stock, add a line there.
SECURITIES = {isin: (s["ticker"], s["currency"])
              for isin, s in load_rules().get("securities", {}).items()}

# Exchange rates: how many DKK one unit of the currency costs.
FX_TICKERS = {"EUR": "EURDKK=X", "USD": "USDDKK=X", "GBP": "GBPDKK=X",
              "CHF": "CHFDKK=X", "SEK": "SEKDKK=X", "NOK": "NOKDKK=X"}


# --- Step 1: read the transactions ----------------------------

def read_transactions(path):
    """Read Nordnet's transaction export into a table.

    Trap: the file has several columns all called 'Valuta'. Each one
    belongs to the amount column just before it, so we rename them,
    e.g. the 'Valuta' after 'Beløb' becomes 'Beløb valuta'.
    """
    with open(path, encoding="utf-16") as f:
        header = f.readline().rstrip("\r\n").split("\t")

    names = []
    for name in header:
        name = name.strip()
        if name == "Valuta" and names:
            name = names[-1] + " valuta"
        names.append(name)

    t = pd.read_csv(path, sep="\t", encoding="utf-16", decimal=",",
                    header=0, names=names)

    # Use the booking day as "the date" - that's when the money and
    # shares actually show up in the account (and in Nordnet's value).
    t["date"] = pd.to_datetime(t["Bogføringsdag"])
    t["Værdipapirer"] = t["Værdipapirer"].fillna("").str.strip()

    # Nordnet's Id numbers grow over time, so sorting by Id puts the
    # transactions in the order they really happened (even within a day).
    return t.sort_values("Id").reset_index(drop=True)


# --- Step 2: shares and cash on every day ---------------------

def rebuild_shares(t, days):
    """Table with one row per day and one column per ISIN: shares held."""
    trades = t[t["Transaktionstype"].isin(["KØBT", "SOLGT"])].copy()
    # Bought = +shares, sold = -shares.
    direction = trades["Transaktionstype"].map({"KØBT": 1, "SOLGT": -1})
    trades["change"] = trades["Antal"] * direction

    # Check: our running total must match Nordnet's 'Totalt antal' column.
    trades["running"] = trades.groupby("ISIN")["change"].cumsum()
    wrong = trades[(trades["running"] - trades["Totalt antal"]).abs() > 1e-9]
    if len(wrong):
        print("WARNING: share counts don't match Nordnet's 'Totalt antal' here:")
        print(wrong[["date", "Værdipapirer", "running", "Totalt antal"]])

    # pivot_table: one row per date, one column per stock, summing the changes.
    changes = trades.pivot_table(index="date", columns="ISIN", values="change", aggfunc="sum")
    # Add the days without trades (as 0 change), then a running total.
    return changes.reindex(days, fill_value=0).fillna(0).cumsum()


def rebuild_cash(t, days):
    """Cash balance in DKK on every day.

    Every transaction's 'Beløb' (amount) is already in DKK, so the cash
    balance is simply the running total of all amounts.
    """
    running = t["Beløb"].cumsum()

    # Check: the final balance must match Nordnet's own 'Saldo'.
    # (Nordnet's export has a few rows where Saldo is briefly off and
    # then corrects itself, so we only check the end result.)
    if abs(running.iloc[-1] - t["Saldo"].iloc[-1]) > 0.01:
        print(f"WARNING: rebuilt cash {running.iloc[-1]:.2f} doesn't match "
              f"Nordnet's final 'Saldo' {t['Saldo'].iloc[-1]:.2f}")

    per_day = t.groupby("date")["Beløb"].sum()
    return per_day.reindex(days, fill_value=0).cumsum()


# --- Step 3: prices and values in DKK -------------------------

def fill_every_day(series, days):
    """Give a price to every calendar day: closed days use the last price."""
    return series.reindex(series.index.union(days)).ffill().reindex(days)


def download_prices_dkk(isins, first_day, last_day):
    """Price in DKK of one share of each stock, on every day."""
    start = first_day - pd.Timedelta(days=10)
    end = last_day + pd.Timedelta(days=1)
    days = pd.date_range(first_day, last_day, freq="D")

    fx = {"DKK": pd.Series(1.0, index=days)}    # 1 DKK = 1 DKK
    needed = {SECURITIES[isin][1] for isin in isins} - {"DKK"}   # only the currencies I hold
    for currency in sorted(needed):
        if currency not in FX_TICKERS:
            raise SystemExit(f"No exchange-rate ticker for {currency} - add it to FX_TICKERS")
        fx[currency] = fill_every_day(download_close(FX_TICKERS[currency], start, end), days)

    prices = {}
    for isin in isins:
        ticker, currency = SECURITIES[isin]
        # Real closing prices (NOT dividend-adjusted): the dividends are
        # already in the cash balance, so adjusting would count them twice.
        local = download_close(ticker, start, end, adjust_for_dividends=False)
        prices[isin] = fill_every_day(local, days) * fx[currency]
    return pd.DataFrame(prices)


# --- Step 4: compare with Nordnet -----------------------------

def compare_with_nordnet(total, files):
    nordnet = combine([read_export(f) for f in files]).set_index("date")["value"]
    check = pd.DataFrame({"nordnet": nordnet, "rebuilt": total}).dropna()
    check = check[check["nordnet"] > 0]
    check["diff_dkk"] = check["rebuilt"] - check["nordnet"]
    check["diff_pct"] = check["diff_dkk"] / check["nordnet"] * 100
    return check


def load_account(last_day=None):
    """Steps 1-3 in one go. Other scripts (position_results.py,
    weekly_check.py) use this. Returns the transactions, shares per day,
    cash per day and prices in DKK per day.

    last_day=None -> stop at the newest date in the Nordnet files.
    last_day=a date -> carry on up to that date (e.g. today).
    """
    t = read_transactions(newest_data_file(TRANSACTIONS_FILES))
    first_day = t["date"].min()
    history_files = data_files(HISTORY_FILES)
    if last_day is None:
        last_day = max(t["date"].max(),
                       max(read_export(f)["date"].max() for f in history_files))
    days = pd.date_range(first_day, last_day, freq="D")

    shares = rebuild_shares(t, days)
    cash = rebuild_cash(t, days)

    missing = [i for i in shares.columns if i not in SECURITIES]
    if missing:
        raise SystemExit(f"No Yahoo ticker for these ISINs - add them to [securities] in rules.toml: {missing}")

    prices = download_prices_dkk(shares.columns, first_day, last_day)
    return t, shares, cash, prices


def main():
    t, shares, cash, prices = load_account()
    history_files = data_files(HISTORY_FILES)

    # The ticker list, so you can confirm the matches.
    names = t.dropna(subset=["ISIN"]).groupby("ISIN")["Værdipapirer"].first()
    print("\nYahoo tickers used:")
    for isin in shares.columns:
        ticker, currency = SECURITIES[isin]
        print(f"  {names[isin][:45]:<46}{isin}  ->  {ticker:<10}({currency})")
    print()

    values = shares * prices                        # DKK value of each position, every day
    total = values.sum(axis=1) + cash               # positions + cash

    check = compare_with_nordnet(total, history_files)

    print("\nRebuilt total vs. Nordnet's 'Samlet værdi' (month ends):")
    print(f"{'Date':<12}{'Nordnet':>12}{'Rebuilt':>12}{'Diff DKK':>11}{'Diff %':>9}")
    month_ends = check.groupby(check.index.to_period("M")).tail(1)
    for date, row in month_ends.iterrows():
        print(f"{date:%Y-%m-%d}  {danish(row['nordnet'], 0):>10}{danish(row['rebuilt'], 0):>12}"
              f"{danish(row['diff_dkk'], 0):>11}{danish(row['diff_pct']):>8}%")

    big = check[check["diff_pct"].abs() > 1]
    print(f"\nDates checked: {len(check)}.  Largest difference: "
          f"{danish(check['diff_pct'].abs().max())}%.  Dates off by more than 1%: {len(big)}")
    if len(big):
        print(big.round(2).to_string())

    OUTPUT_FOLDER.mkdir(exist_ok=True)
    out = values.rename(columns=lambda i: SECURITIES[i][0]).assign(cash=cash, total=total)
    out.to_csv(OUTPUT_FOLDER / "positions_daily.csv", sep=";", decimal=",",
               encoding="utf-8-sig", index_label="date")
    print(f"\nDaily values per position saved to: {OUTPUT_FOLDER / 'positions_daily.csv'}")


if __name__ == "__main__":
    main()
