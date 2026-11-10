# ============================================================
#  shadow_portfolio.py  -  compare my account with a "shadow"
#
#  1. Reads the Nordnet "Daglig udvikling" exports and joins them
#     into one clean history (weekly rows first, daily rows after).
#  2. Downloads prices for the S&P 500 ETF SXR8 (in EUR) and the
#     EUR/DKK exchange rate, and converts the ETF price to DKK.
#  3. Builds a "shadow portfolio": every deposit/withdrawal I made
#     is invested in / taken out of SXR8 on the same date.
#  4. Works out the time-weighted return ("% pace") for both,
#     since the start and year-to-date.
#  5. Prints a month-by-month table and draws a chart.
#
#  Needs internet (for the prices). It never changes the CSV files.
# ============================================================

import json
from pathlib import Path

import pandas as pd                       # tables of data ("Excel in Python")
import matplotlib.pyplot as plt           # drawing charts
from matplotlib.ticker import FuncFormatter
import yfinance as yf                     # downloading prices from Yahoo Finance

# Re-use the Danish number formatter we already wrote in portfolio.py.
# Python can import from our own files too, as long as they are in
# the same folder as this script.
from portfolio import danish


# --- Settings -------------------------------------------------
# Where the Nordnet files are and where results go: see settings.py.
from settings import DATA_FOLDER, OUTPUT_FOLDER, HISTORY_FILES, data_files

BENCHMARK_TICKER = "SXR8.DE"   # iShares Core S&P 500 UCITS ETF on Xetra (priced in EUR)
FX_TICKER = "EURDKK=X"         # how many DKK one EUR costs


# --- Step 1: read and combine the Nordnet history files -------

def read_export(path):
    """Read one 'Daglig udvikling' file into a table with 3 columns:
    date, value (account value in DKK) and deposits (net deposits in DKK)."""
    table = pd.read_csv(
        path,
        sep="\t",            # columns are separated by TAB
        encoding="utf-16",   # Nordnet saves the file as UTF-16
        decimal=",",         # Danish decimal comma: 89654,98
    )
    table.columns = [name.strip() for name in table.columns]   # tidy the headers

    # Keep only the 3 columns we need, and give them short English names.
    table = table.rename(columns={
        "Dato": "date",
        "Samlet værdi (DKK)": "value",
        "Nettoindbetalinger (DKK)": "deposits",
    })[["date", "value", "deposits"]]

    table["date"] = pd.to_datetime(table["date"])   # text "2025-11-06" -> a real date
    return table


def combine(histories):
    """Join several history tables into one.

    Rule: older (weekly) rows are used until a newer file starts;
    from then on the newer (daily) file's rows are used. That way
    no date appears twice.

    Trap: each Nordnet export restarts 'deposits' at 0 on its first
    day. So we work out the difference on dates that appear in both
    files and add it to the newer file's deposits.
    """
    # Sort the files so the one that starts earliest comes first.
    histories = sorted(histories, key=lambda t: t["date"].min())
    combined = histories[0]

    for newer in histories[1:]:
        # Dates that appear in BOTH tables:
        overlap = combined.merge(newer, on="date", suffixes=("_old", "_new"))
        if overlap.empty:
            raise SystemExit("The history files don't share any dates, so they can't be joined.")

        offsets = overlap["deposits_old"] - overlap["deposits_new"]
        offset = offsets.iloc[0]
        if (offsets - offset).abs().max() > 0.01:
            print("WARNING: the files disagree about deposits on some shared dates. "
                  "Check the exports - the result may be off.")

        newer = newer.copy()                  # don't change the original table
        newer["deposits"] = newer["deposits"] + offset

        # Old rows BEFORE the newer file starts + all rows from the newer file.
        start = newer["date"].min()
        combined = pd.concat([combined[combined["date"] < start], newer])

    return combined.sort_values("date").reset_index(drop=True)


# --- Step 2: download benchmark prices in DKK -----------------

def download_close(ticker, start, end, adjust_for_dividends=True):
    """Download daily closing prices for one ticker from Yahoo Finance.

    adjust_for_dividends=True lowers old prices to account for dividends
    paid since (fine for a fund that pays none, like SXR8). Use False when
    the dividends are already counted separately as cash.
    """
    print(f"Downloading {ticker} ...")
    prices = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=adjust_for_dividends)
    if prices.empty:
        raise SystemExit(f"No prices came back for {ticker}. Check your internet connection.")
    close = prices["Close"]
    # Yahoo attaches a time zone to each date; we only want the plain date.
    close.index = close.index.tz_localize(None).normalize()
    return close


def benchmark_price_dkk(first_date, last_date):
    """SXR8 price in DKK for EVERY calendar day between the two dates.

    Markets are closed on weekends and holidays, but Nordnet's file has
    a row for every day. On closed days we use the last known price
    ("forward fill") - exactly what your account value does too.
    """
    # Start a little early so there is always a price on or before day one.
    start = first_date - pd.Timedelta(days=10)
    end = last_date + pd.Timedelta(days=1)    # Yahoo's end date is not included

    etf_eur = download_close(BENCHMARK_TICKER, start, end)
    eur_dkk = download_close(FX_TICKER, start, end)

    every_day = pd.date_range(start, last_date, freq="D")

    def fill_every_day(series):
        return series.reindex(series.index.union(every_day)).ffill().reindex(every_day)

    return fill_every_day(etf_eur) * fill_every_day(eur_dkk)   # EUR price x DKK per EUR


# --- Step 3: the shadow portfolio -----------------------------

def build_shadow(history, price_dkk):
    """Add shadow-portfolio columns to the history table."""
    h = history.copy()
    h["benchmark_price_dkk"] = price_dkk.reindex(h["date"]).values

    if h["benchmark_price_dkk"].isna().any():
        raise SystemExit("Missing benchmark prices for some dates - can't build the shadow.")

    # How much money went in (or out, if negative) on each row?
    # .diff() = this row's deposits minus the previous row's.
    h["new_money"] = h["deposits"].diff().fillna(h["deposits"])

    # Buy (or sell) that amount of the ETF at that day's price.
    # Fractions of a share are allowed - it keeps the comparison exact.
    h["units_bought"] = h["new_money"] / h["benchmark_price_dkk"]
    h["shadow_units"] = h["units_bought"].cumsum()        # running total of units held
    h["shadow_value"] = h["shadow_units"] * h["benchmark_price_dkk"]

    # Real minus shadow. Positive = I'm beating the benchmark.
    h["difference_dkk"] = h["value"] - h["shadow_value"]
    has_shadow = h["shadow_value"] > 0                   # avoid dividing by zero
    h["difference_pct"] = (h["difference_dkk"] / h["shadow_value"] * 100).where(has_shadow)
    return h


# --- Step 4: time-weighted return ("% pace") ------------------

def account_returns(value, new_money):
    """My account's % return per row, ignoring deposits/withdrawals.
    (Also used by risk.py for the drawdown.)

    Same method as Nordnet: a DEPOSIT counts as being there from the
    START of the period, a WITHDRAWAL as leaving at the END of it.
    """
    previous_value = value.shift(1).fillna(0)
    # .clip(lower=0) keeps deposits and turns withdrawals into 0.
    start_value = previous_value + new_money.clip(lower=0)
    # The gain itself must ignore ALL money moved in or out - a
    # withdrawal is not a loss and a deposit is not a profit.
    gain = value - previous_value - new_money
    # .where(...) keeps the % only where we didn't start from 0 kr.
    return (gain / start_value).where(start_value > 0, 0.0)


def add_growth_indexes(h):
    """Add a 'growth index' for my account and for the shadow.

    A growth index answers: "what would 1 krone, invested at the very
    start, be worth now?" 1,25 means +25%. Deposits and withdrawals
    don't change it - that's what makes it time-weighted.
    """
    h = h.copy()

    # --- My account: one % per row, then chain them together.
    h["my_return"] = account_returns(h["value"], h["new_money"])
    # cumprod = multiply all (1 + return) together, row by row.
    h["my_growth"] = (1 + h["my_return"]).cumprod()

    # --- Shadow: it only ever holds the ETF, so its growth index is
    # simply today's ETF price divided by the price on the first deposit.
    first_buy = h.index[h["new_money"] > 0][0]       # row of the first deposit
    first_price = h.loc[first_buy, "benchmark_price_dkk"]
    h["shadow_growth"] = (h["benchmark_price_dkk"] / first_price).where(h.index >= first_buy, 1.0)
    return h


def pace(h, column):
    """Return (since start %, year-to-date %) from a growth-index column."""
    last = h.iloc[-1]
    since_start = (last[column] - 1) * 100

    # YTD: compare with the last row from before 1 January this year.
    jan_1 = pd.Timestamp(year=last["date"].year, month=1, day=1)
    before_this_year = h[h["date"] < jan_1]
    base = before_this_year[column].iloc[-1] if len(before_this_year) else 1.0
    ytd = (last[column] / base - 1) * 100
    return since_start, ytd


def print_pace(h):
    my_start, my_ytd = pace(h, "my_growth")
    sh_start, sh_ytd = pace(h, "shadow_growth")
    year = h.iloc[-1]["date"].year

    print("Pace - time-weighted return (ignores when money went in):")
    print(f"  {'':<22}{'Since start':>13}{'YTD ' + str(year):>13}")
    print(f"  {'My account':<22}{signed(my_start) + '%':>13}{signed(my_ytd) + '%':>13}")
    print(f"  {'Shadow (' + BENCHMARK_TICKER + ')':<22}{signed(sh_start) + '%':>13}{signed(sh_ytd) + '%':>13}")
    # Percentage POINTS: the simple gap between two percentages.
    print(f"  {'Difference (points)':<22}{signed(my_start - sh_start):>13}{signed(my_ytd - sh_ytd):>13}")
    print()


# --- Step 5: show the results ---------------------------------

def signed(number, decimals=2):
    """Danish number with a + in front of positives."""
    return ("+" if number >= 0 else "") + danish(number, decimals)


def print_table(h):
    """Print one row per month (the last day of each month we have)."""
    month_ends = h.groupby(h["date"].dt.to_period("M")).tail(1)
    month_ends = month_ends[month_ends["shadow_value"] > 0]

    header = (f"{'Date':<12}{'Deposits':>12}{'My value':>13}{'Shadow':>13}"
              f"{'Diff DKK':>13}{'Diff %':>9}")
    print()
    print(header)
    print("-" * len(header))
    for _, row in month_ends.iterrows():           # one loop pass per month
        print(f"{row['date']:%Y-%m-%d}  "
              f"{danish(row['deposits'], 0):>12}"
              f"{danish(row['value'], 0):>13}"
              f"{danish(row['shadow_value'], 0):>13}"
              f"{signed(row['difference_dkk'], 0):>13}"
              f"{signed(row['difference_pct']) + '%':>9}")
    print("-" * len(header))


def draw_chart(h, save_to):
    fig, ax = plt.subplots(figsize=(11, 6))
    ax.plot(h["date"], h["value"], label="My account", linewidth=2)
    ax.plot(h["date"], h["shadow_value"], label=f"Shadow ({BENCHMARK_TICKER})", linewidth=2)
    ax.plot(h["date"], h["deposits"], label="Net deposits", linestyle="--", color="grey")

    ax.set_title("My account vs. shadow portfolio")
    ax.set_ylabel("DKK")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: danish(x, 0)))
    ax.grid(alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(save_to, dpi=150)
    print(f"Chart saved to: {save_to}")
    plt.show()     # opens a window with the chart; close it to finish


# --- The main program -----------------------------------------

def main():
    files = data_files(HISTORY_FILES)
    if not files:
        raise SystemExit(f"No '{HISTORY_FILES}' files found in {DATA_FOLDER}")
    print("Reading:")
    for f in files:
        print("  ", f.name)

    history = combine([read_export(f) for f in files])
    price_dkk = benchmark_price_dkk(history["date"].min(), history["date"].max())
    h = build_shadow(history, price_dkk)
    h = add_growth_indexes(h)

    print_table(h)

    last = h.iloc[-1]          # the newest row
    print()
    print(f"As of {last['date']:%d-%m-%Y}:")
    print(f"  Net deposits:       {danish(last['deposits'])} DKK")
    print(f"  My account:         {danish(last['value'])} DKK")
    print(f"  Shadow portfolio:   {danish(last['shadow_value'])} DKK")
    print(f"  Difference:         {signed(last['difference_dkk'])} DKK "
          f"({signed(last['difference_pct'])}%)")
    print()
    print_pace(h)

    # Save everything so you can open it in Excel.
    OUTPUT_FOLDER.mkdir(exist_ok=True)
    csv_path = OUTPUT_FOLDER / "shadow_comparison.csv"
    # sep=";" and decimal="," make Danish Excel open it correctly.
    h.to_csv(csv_path, sep=";", decimal=",", index=False, encoding="utf-8-sig")
    print(f"Full table saved to: {csv_path}")

    # The headline numbers, for dashboard.py (so it never recalculates them).
    my_start, my_ytd = pace(h, "my_growth")
    sh_start, sh_ytd = pace(h, "shadow_growth")
    summary = {"date": f"{last['date']:%Y-%m-%d}", "deposits": float(last["deposits"]),
               "value": float(last["value"]), "shadow_value": float(last["shadow_value"]),
               "difference_dkk": float(last["difference_dkk"]),
               "difference_pct": float(last["difference_pct"]),
               "my_since_start": float(my_start), "my_ytd": float(my_ytd),
               "shadow_since_start": float(sh_start), "shadow_ytd": float(sh_ytd)}
    (OUTPUT_FOLDER / "shadow_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    draw_chart(h, OUTPUT_FOLDER / "shadow_vs_real.png")


if __name__ == "__main__":
    main()
