# ============================================================
#  portfolio.py  -  a simple Nordnet portfolio tracker
#
#  Reads the Nordnet "Aktietabel" CSV export (from the data folder,
#  see settings.py) and prints every holding plus the portfolio total.
#  It ONLY reads the file - it never changes it.
# ============================================================

# --- 1. "Imports": borrowing ready-made tools -----------------
# Python ships with lots of built-in toolboxes. We borrow one:
#   csv - knows how to split a CSV file into rows and columns
import csv

# ...and one of our own: settings.py knows where the data folder is.
from settings import DATA_FOLDER, HOLDINGS_FILES, newest_data_file


# --- 2. Small helper functions --------------------------------
# A "function" is a named, reusable mini-recipe. "def" creates one.
# Whatever goes in the brackets is the input; "return" gives back the result.

def find_latest_export(folder):
    """Find the newest Nordnet 'Aktietabel' CSV file in the data folder.
    (settings.py does the searching: name pattern, newest file wins.)"""
    return newest_data_file(HOLDINGS_FILES)


def to_number(text):
    """Turn Danish-style text like '1.234,56' into the number 1234.56."""
    text = text.strip()                 # remove spaces at the ends
    if text == "":
        return 0.0                      # empty cell -> treat as zero
    text = text.replace(".", "")        # remove thousands separators
    text = text.replace(",", ".")       # Danish decimal comma -> dot
    return float(text)                  # float = a number with decimals


def danish(number, decimals=2):
    """Format a number the Danish way: 1234.5 -> '1.234,50'."""
    # Python formats as '1,234.50'; we then swap commas and dots.
    english = f"{number:,.{decimals}f}"
    return english.replace(",", "X").replace(".", ",").replace("X", ".")


def read_holdings(csv_path):
    """Read the CSV file and return a list of holdings."""
    holdings = []   # an empty list we'll fill up, one holding at a time

    # Nordnet saves the file as UTF-16 text with TAB between columns.
    # "with open(...)" opens the file and closes it again automatically.
    # The "r" means read-only, so the file can't be changed.
    with open(csv_path, "r", encoding="utf-16", newline="") as f:
        # DictReader uses the first line (the headers) as labels, so each
        # row becomes a "dictionary": row["Antal"] gives the quantity, etc.
        reader = csv.DictReader(f, delimiter="\t")

        for row in reader:   # a "for loop": repeat for every row in the file
            # Clean up stray spaces around column names and values.
            row = {key.strip(): (value or "").strip() for key, value in row.items() if key}

            value_dkk = to_number(row["Værdi DKK"])
            gain_dkk = to_number(row["Afkast DKK"])

            # Store the bits we care about in a dictionary of our own.
            holdings.append({
                "name":      row["Navn"],
                "currency":  row["Valuta"],
                "quantity":  to_number(row["Antal"]),
                "avg_cost":  to_number(row["GAK"]),          # in the stock's own currency
                "price":     to_number(row["Seneste kurs"]), # in the stock's own currency
                "value_dkk": value_dkk,
                "gain_dkk":  gain_dkk,
                "cost_dkk":  value_dkk - gain_dkk,           # what you paid, in DKK
            })
    return holdings


def percent(gain, cost):
    """Gain as a percentage of what was paid (avoid dividing by zero)."""
    if cost == 0:
        return 0.0
    return gain / cost * 100


# --- 4. The main program --------------------------------------
def main():
    csv_path = find_latest_export(DATA_FOLDER)
    holdings = read_holdings(csv_path)

    print()
    print(f"Portfolio from: {csv_path.name}")
    print()

    # The header line. Things like {'Name':<34} mean "left-align in
    # 34 characters"; > means right-align. This lines up the columns.
    header = (f"{'Name':<34}{'Qty':>6}  {'Avg cost':>14}  {'Price':>14}"
              f"{'Value DKK':>14}{'Gain DKK':>14}{'Gain %':>9}")
    print(header)
    print("-" * len(header))   # a line of dashes as long as the header

    # Running totals - start at zero and add each holding to them.
    total_value = 0.0
    total_gain = 0.0
    total_cost = 0.0

    for h in holdings:
        gain_pct = percent(h["gain_dkk"], h["cost_dkk"])

        name = h["name"][:33]   # cut very long names so the table stays tidy
        qty = danish(h["quantity"], 0)
        avg_cost = f"{danish(h['avg_cost'])} {h['currency']}"
        price = f"{danish(h['price'])} {h['currency']}"

        # + sign in front of positive numbers, so gains are easy to spot
        gain_text = ("+" if h["gain_dkk"] >= 0 else "") + danish(h["gain_dkk"])
        pct_text = ("+" if gain_pct >= 0 else "") + danish(gain_pct) + "%"

        print(f"{name:<34}{qty:>6}  {avg_cost:>14}  {price:>14}"
              f"{danish(h['value_dkk']):>14}{gain_text:>14}{pct_text:>9}")

        total_value += h["value_dkk"]    # += means "add to what's already there"
        total_gain += h["gain_dkk"]
        total_cost += h["cost_dkk"]

    # The totals
    total_pct = percent(total_gain, total_cost)
    print("-" * len(header))
    print(f"{'TOTAL':<72}{danish(total_value):>14}"
          f"{('+' if total_gain >= 0 else '') + danish(total_gain):>14}"
          f"{('+' if total_pct >= 0 else '') + danish(total_pct) + '%':>9}")
    print()
    print(f"Invested (cost in DKK): {danish(total_cost)} DKK")
    print(f"Current value:          {danish(total_value)} DKK")
    print(f"Unrealised gain/loss:   {danish(total_gain)} DKK ({danish(total_pct)}%)")
    print()
    print("Note: average cost and price are in each stock's own currency;")
    print("DKK figures use Nordnet's exchange rates from the export.")
    print()


# This line means: "if this file is run directly, start main()".
if __name__ == "__main__":
    main()
