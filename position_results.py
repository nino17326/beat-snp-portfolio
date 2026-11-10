# ============================================================
#  position_results.py  -  which positions made or lost money,
#  and did each one beat simply buying CSPX instead?
#
#  For every stock I've owned (also the ones I've sold):
#   - profit/loss in DKK since I first bought it
#   - profit/loss in DKK since a second start date (e.g. the day my lead
#     over CSPX peaked): [results] compare_from in rules.toml
#   - a "per-position shadow": the same buys and sells, but in CSPX
#     (the core ticker, converted to DKK). Did the stock beat it?
#
#  Uses positions.py to rebuild the account from the transactions.
#  Needs internet (for prices). It never changes the CSV files.
# ============================================================

import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

from portfolio import danish
from positions import load_account, SECURITIES, OUTPUT_FOLDER
from settings import load_rules

RULES = load_rules()
CORE_TICKER = RULES["portfolio"]["core_ticker"]                  # CSPX (e.g. SXR8.DE)
# The core's ISIN, looked up in [securities] - its prices are the benchmark.
BENCHMARK_ISIN = next((isin for isin, (tk, _) in SECURITIES.items() if tk == CORE_TICKER), None)


def compare_date(end):
    """The second start date: [results] compare_from in rules.toml,
    or 6 months before the last day if it isn't set."""
    chosen = RULES.get("results", {}).get("compare_from")
    return pd.Timestamp(chosen) if chosen else (end - pd.DateOffset(months=6)).normalize()


def position_result(isin, t, shares, prices, start=None):
    """Profit/loss for ONE position, and for its CSPX shadow.

    start=None    -> from the very first purchase
    start=a date  -> pretend I "bought" what I held at the close on that
                     date, then count everything that happened after it.
    Returns None if I didn't own or trade the stock in that period.
    """
    cspx = prices[BENCHMARK_ISIN]          # CSPX price in DKK, every day
    end = prices.index[-1]                 # the last day we have prices for

    rows = t[t["ISIN"] == isin]
    held_at_start = 0.0
    if start is not None:
        rows = rows[rows["date"] > start]
        held_at_start = shares.loc[start, isin]
    if held_at_start == 0 and not rows["Transaktionstype"].isin(["KØBT", "SOLGT"]).any():
        return None

    # Running totals, all in DKK. They start at zero...
    spent = 0.0          # money paid for buying (fees included)
    received = 0.0       # money received from selling (after fees)
    dividends = 0.0      # dividends after dividend tax
    fees = 0.0           # brokerage fees - already inside spent/received
    my_shares = held_at_start
    shadow_units = 0.0   # how many CSPX units the shadow owns
    shadow_received = 0.0

    # ...except when starting mid-way: then the position's value on the
    # start date counts as "spent", and the shadow buys CSPX for the same.
    if held_at_start > 0:
        start_value = held_at_start * prices.loc[start, isin]
        spent += start_value
        shadow_units += start_value / cspx[start]

    # Go through this stock's transactions one by one, in time order.
    for _, row in rows.iterrows():
        kind = row["Transaktionstype"]
        price_cspx = cspx[row["date"]]

        if kind == "KØBT":                               # a buy
            cost = -row["Beløb"]                         # Beløb is negative for buys
            spent += cost
            fees += row["Kurtage"]
            my_shares += row["Antal"]
            shadow_units += cost / price_cspx            # shadow buys CSPX for the same DKK

        elif kind == "SOLGT":                            # a sale
            received += row["Beløb"]
            fees += row["Kurtage"]
            fraction = row["Antal"] / my_shares          # e.g. 0.5 = sold half
            my_shares -= row["Antal"]
            units_sold = shadow_units * fraction         # shadow sells the same half
            shadow_units -= units_sold
            shadow_received += units_sold * price_cspx

        elif kind in ("UDBYTTE", "UDBYTTESKAT"):         # dividend and its tax
            dividends += row["Beløb"]

        else:
            print(f"WARNING: don't know how to handle '{kind}' for {isin} - skipped")

    value_now = my_shares * prices.loc[end, isin]
    profit = received + dividends + value_now - spent

    shadow_value_now = shadow_units * cspx[end]
    shadow_profit = shadow_received + shadow_value_now - spent

    return {
        "spent": spent, "received": received, "dividends": dividends,
        "value_now": value_now, "fees": fees,
        "profit": profit,
        "profit_pct": profit / spent * 100,
        "shadow_profit": shadow_profit,
        "beat_cspx": profit > shadow_profit,
    }


def results_table(t, shares, prices, start=None):
    """Run position_result for every stock and collect the answers in a table."""
    names = t.dropna(subset=["ISIN"]).groupby("ISIN")["Værdipapirer"].first()
    rows = {}
    for isin in shares.columns:
        result = position_result(isin, t, shares, prices, start)
        if result is not None:
            rows[names[isin][:30]] = result
    table = pd.DataFrame(rows).T                       # .T = flip: one row per stock
    return table.sort_values("profit", ascending=False)


def print_results(title, table):
    def yes_no(beat):
        return "YES" if beat else "no"

    print()
    print(title)
    header = (f"{'Position':<31}{'Spent':>10}{'Sold':>10}{'Divid.':>8}{'Value now':>11}"
              f"{'Fees':>6}{'P/L DKK':>10}{'P/L %':>9}{'CSPX P/L':>10}{'Beat?':>7}")
    print(header)
    print("-" * len(header))
    for name, r in table.iterrows():
        print(f"{name:<31}{danish(r['spent'], 0):>10}{danish(r['received'], 0):>10}"
              f"{danish(r['dividends'], 0):>8}{danish(r['value_now'], 0):>11}"
              f"{danish(r['fees'], 0):>6}{danish(r['profit'], 0):>10}"
              f"{danish(r['profit_pct'], 1) + '%':>9}{danish(r['shadow_profit'], 0):>10}"
              f"{yes_no(r['beat_cspx']):>7}")
    print("-" * len(header))
    print(f"{'TOTAL':<31}{'':>45}{danish(table['fees'].sum(), 0):>6}"
          f"{danish(table['profit'].sum(), 0):>10}{'':>9}"
          f"{danish(table['shadow_profit'].sum(), 0):>10}")


def draw_chart(total, since_peak, start, save_to):
    """One bar chart: my profit vs. the CSPX shadow, per position.
    Left half since the first purchase, right half since `start`."""
    # Both halves must list the SAME positions in the SAME order, so the
    # names on the left line up with both. Positions I didn't own after
    # the peak get empty (0) bars on the right.
    order = total.index[::-1]                          # biggest winner on top
    since_peak = since_peak.reindex(order)[["profit", "shadow_profit"]].fillna(0)

    fig, axes = plt.subplots(1, 2, figsize=(14, 6), sharey=True)
    for ax, table, title in [(axes[0], total.reindex(order), "Since first purchase"),
                             (axes[1], since_peak, f"Since {start:%d %b %Y}")]:
        y = range(len(table))
        ax.barh([i + 0.2 for i in y], table["profit"], height=0.4, label="My position")
        ax.barh([i - 0.2 for i in y], table["shadow_profit"], height=0.4,
                label="Same trades in CSPX", color="grey")
        ax.set_yticks(list(y))
        ax.set_yticklabels(table.index)
        ax.axvline(0, color="black", linewidth=0.8)
        ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _: danish(x, 0)))
        ax.set_title(title)
        ax.set_xlabel("Profit / loss (DKK)")
        ax.grid(axis="x", alpha=0.3)
    axes[0].legend(loc="lower right")
    fig.suptitle("Profit per position vs. buying CSPX instead")
    fig.tight_layout()
    fig.savefig(save_to, dpi=150)
    print(f"\nChart saved to: {save_to}")
    plt.show()


def main():
    t, shares, cash, prices = load_account()
    end = prices.index[-1]
    if BENCHMARK_ISIN not in prices.columns:
        raise SystemExit(f"The core {CORE_TICKER} was never traded, so there is no benchmark price to compare with")
    start = compare_date(end)

    total = results_table(t, shares, prices)
    since_peak = results_table(t, shares, prices, start=start)

    print_results(f"Profit/loss per position, first purchase -> {end:%d-%m-%Y}", total)
    print_results(f"Profit/loss per position, {start:%d-%m-%Y} -> {end:%d-%m-%Y} "
                  f"('Spent' includes the value held on {start:%d %b})", since_peak)

    # Sanity check: all positions together + account-level taxes should
    # equal the account's total gain (value now - net money deposited).
    cash_types = ["INDSÆTTELSE", "HÆVNING", "SKATTEINDBETALING ASK"]
    net_deposits = t[t["Transaktionstype"].isin(cash_types)]["Beløb"].sum()
    ask_tax = t[t["Transaktionstype"] == "AFKASTSKAT ASK"]["Beløb"].sum()
    account_value = (shares.loc[end] * prices.loc[end]).sum() + cash[end]
    print()
    print("Check:")
    print(f"  Sum of all positions' profit:   {danish(total['profit'].sum())} DKK")
    print(f"  + ASK tax (account level):      {danish(ask_tax)} DKK")
    print(f"  = {danish(total['profit'].sum() + ask_tax)} DKK, should equal account value "
          f"{danish(account_value)} - net deposits {danish(net_deposits)} "
          f"= {danish(account_value - net_deposits)} DKK")

    OUTPUT_FOLDER.mkdir(exist_ok=True)
    for table, name in [(total, "positions_total.csv"), (since_peak, "positions_since_peak.csv")]:
        table.to_csv(OUTPUT_FOLDER / name, sep=";", decimal=",", encoding="utf-8-sig",
                     index_label="position")
    draw_chart(total, since_peak, start, OUTPUT_FOLDER / "positions_vs_cspx.png")


if __name__ == "__main__":
    main()
