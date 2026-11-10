# ============================================================
#  regime.py  -  market regime detector (Hidden Markov Model)
#
#  For the S&P 500 (^GSPC) and semiconductors (^SOX):
#   1. Features per day: return, and 20-day volatility.
#   2. A 3-state Hidden Markov Model, labelled calm / normal / stressed
#      by each state's typical volatility.
#   3. WALK-FORWARD: refit on the first trading day of every month,
#      using only data from BEFORE that day. Each day is labelled using
#      data up to that day's close - never anything later.
#   4. Honest test: what happened in the 20 trading days AFTER each
#      label? Compared with a simple "is volatility high?" rule.
#
#  Not a trading strategy. It only describes the market's mood.
# ============================================================

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

from portfolio import danish
from shadow_portfolio import download_close
from positions import OUTPUT_FOLDER

MARKETS = {"^GSPC": "S&P 500", "^SOX": "Semis (SOX)"}
START = "2000-01-01"
LABELS = ["calm", "normal", "stressed"]
COLOURS = {"calm": "tab:green", "normal": "tab:blue", "stressed": "tab:red"}
MIN_TRAIN_DAYS = 750        # ~3 years of data before the first test month
HORIZON = 20                # the test looks 20 trading days ahead


# --- The Hidden Markov Model ----------------------------------
# Written out in numpy (no extra package), so every step is visible.

class GaussianHMM:
    """A Hidden Markov Model with K hidden states.

    Each state has:
      - a typical "symptom" pattern: the average return & volatility
        and how they spread (a 2-D bell curve = Gaussian)
    and between states there are:
      - transition probabilities: P(tomorrow's state | today's state)
    """

    def __init__(self, n_states=3):
        self.K = n_states
        self.fitted = False

    def _start_guess(self, X):
        """First guess before learning: split the days into thirds by
        volatility (column 1) and use each third as one state."""
        cuts = np.quantile(X[:, 1], [1 / 3, 2 / 3])
        groups = np.digitize(X[:, 1], cuts)              # 0, 1 or 2 per day
        self.means = np.array([X[groups == k].mean(axis=0) for k in range(self.K)])
        self.covs = np.array([np.cov(X[groups == k].T) + 1e-4 * np.eye(2) for k in range(self.K)])
        self.trans = np.full((self.K, self.K), 0.02 / (self.K - 1)) + np.eye(self.K) * 0.98 \
            - np.eye(self.K) * 0.02 / (self.K - 1)       # 98% chance to stay put
        self.start = np.full(self.K, 1 / self.K)

    def _log_bell(self, X):
        """How well each day fits each state's bell curve (log scale).
        Returns a table: one row per day, one column per state."""
        out = np.empty((len(X), self.K))
        for k in range(self.K):
            diff = X - self.means[k]
            inv = np.linalg.inv(self.covs[k])
            _, logdet = np.linalg.slogdet(self.covs[k])
            distance = np.einsum("ij,jk,ik->i", diff, inv, diff)
            out[:, k] = -0.5 * (distance + logdet + 2 * np.log(2 * np.pi))
        return out

    def _forward(self, B):
        """The FORWARD pass: walk through the days in order, updating the
        belief about today's state using ONLY today and earlier days.
        alpha[t] = P(state on day t | data up to day t)."""
        T = len(B)
        alpha = np.empty((T, self.K))
        scale = np.empty(T)
        a = self.start * B[0]
        scale[0] = a.sum()
        alpha[0] = a / scale[0]
        for t in range(1, T):
            a = (alpha[t - 1] @ self.trans) * B[t]       # yesterday's belief -> today's
            scale[t] = a.sum()
            alpha[t] = a / scale[t]
        return alpha, scale

    def fit(self, X, max_iter=200, tol=1e-4):
        """Learn the states from data X (the 'Baum-Welch' / EM method):
        guess -> see how well the guess explains the data -> improve -> repeat.
        If the model was fitted before, it starts from last time's answer."""
        if not self.fitted:
            self._start_guess(X)
        previous = -np.inf
        for _ in range(max_iter):
            logB = self._log_bell(X)
            shift = logB.max(axis=1, keepdims=True)
            B = np.exp(logB - shift)                     # rescaled to avoid tiny numbers

            # E-step: forward pass + backward pass -> how likely each state was
            # on each day. (Using later days here is fine: this is TRAINING
            # on the past, not labelling the day we test.)
            alpha, scale = self._forward(B)
            beta = np.empty_like(alpha)
            beta[-1] = 1.0
            for t in range(len(X) - 2, -1, -1):
                beta[t] = self.trans @ (B[t + 1] * beta[t + 1]) / scale[t + 1]
            gamma = alpha * beta
            gamma /= gamma.sum(axis=1, keepdims=True)
            xi = (alpha[:-1, :, None] * self.trans[None]
                  * (B[1:] * beta[1:])[:, None, :]) / scale[1:, None, None]

            # M-step: update the states' bell curves and switching odds.
            self.start = gamma[0]
            self.trans = xi.sum(axis=0)
            self.trans /= self.trans.sum(axis=1, keepdims=True)
            weight = gamma.sum(axis=0)
            self.means = (gamma.T @ X) / weight[:, None]
            for k in range(self.K):
                diff = X - self.means[k]
                self.covs[k] = (gamma[:, k, None] * diff).T @ diff / weight[k] + 1e-4 * np.eye(2)

            loglik = np.log(scale).sum() + shift.sum()   # how well the model fits
            if loglik - previous < tol * abs(loglik):
                break                                    # no longer improving: done
            previous = loglik
        self.fitted = True

    def filtered_probabilities(self, X):
        """P(state | data up to and including each day) - no future used.
        Columns are ordered calm, normal, stressed (by state volatility)."""
        alpha, _ = self._forward(np.exp(self._log_bell(X)
                                        - self._log_bell(X).max(axis=1, keepdims=True)))
        order = np.argsort(self.means[:, 1])             # lowest volatility first
        return alpha[:, order]


# --- Data and features ----------------------------------------

def load_features(ticker):
    close = download_close(ticker, pd.Timestamp(START), pd.Timestamp.today() + pd.Timedelta(days=1))
    returns = close.pct_change() * 100                    # daily return in %
    vol = returns.rolling(20).std()                       # 20-day volatility in %
    features = pd.DataFrame({"ret": returns, "vol": vol}).dropna()
    return close, features


# --- Walk-forward ---------------------------------------------

def walk_forward(features):
    """Label every day using only the past. Returns a table with the
    probabilities of calm/normal/stressed per day, plus the simple
    'volatility third' rule for comparison."""
    X = features.values
    dates = features.index
    month_starts = list(dates.to_series().groupby(dates.to_period("M")).first())
    model = GaussianHMM()
    pieces = []

    for i, month_start in enumerate(month_starts):
        past = dates < month_start
        if past.sum() < MIN_TRAIN_DAYS:
            continue
        next_start = month_starts[i + 1] if i + 1 < len(month_starts) else dates[-1] + pd.Timedelta(days=1)

        # Scale the features with averages from the PAST only.
        mean, std = X[past].mean(axis=0), X[past].std(axis=0)
        Z = (X - mean) / std

        model.fit(Z[past], max_iter=200 if not model.fitted else 30)

        # Label this month's days, running the forward pass up to month end.
        upto = dates < next_start
        probs = model.filtered_probabilities(Z[upto])
        in_month = dates[upto] >= month_start
        piece = pd.DataFrame(probs[in_month], index=dates[upto][in_month], columns=LABELS)

        # The simple rule: is today's volatility in the low/middle/top third
        # of all PAST volatility readings?
        cuts = np.quantile(X[past, 1], [1 / 3, 2 / 3])
        piece["simple_rule"] = np.array(["low vol", "mid vol", "high vol"])[
            np.digitize(X[upto][in_month, 1], cuts)]
        pieces.append(piece)

        if month_start.month == 1:
            print(f"    walked forward to {month_start:%Y}")

    result = pd.concat(pieces)
    result["regime"] = result[LABELS].idxmax(axis=1)
    result["probability"] = result[LABELS].max(axis=1)
    return result


# --- The honest test ------------------------------------------

def what_happened_next(close):
    """For every day: return and volatility over the NEXT 20 trading days."""
    returns = close.pct_change()
    fwd_return = (close.shift(-HORIZON) / close - 1) * 100
    # Volatility of days t+1 ... t+20, as a yearly figure in %.
    fwd_vol = returns[::-1].rolling(HORIZON).std()[::-1].shift(-1) * np.sqrt(252) * 100
    return pd.DataFrame({"fwd_return": fwd_return, "fwd_vol": fwd_vol})


def test_table(labels, nxt):
    """Average outcome in the next 20 days, per group."""
    data = nxt.join(labels.rename("group"), how="inner").dropna()
    rows = {}
    for group, g in list(data.groupby("group")) + [("ALL DAYS", data)]:
        rows[group] = {
            "days": len(g),
            "share_%": len(g) / len(data) * 100,
            "next20_return_%": g["fwd_return"].mean(),
            "next20_negative_%": (g["fwd_return"] < 0).mean() * 100,
            "next20_vol_%": g["fwd_vol"].mean(),
        }
    return pd.DataFrame(rows).T


def t_statistic(labels, nxt, group):
    """Is the 20-day return after `group` really different from other days?
    Uses every 20th day only, so the 20-day windows don't overlap
    (overlapping windows would make the evidence look stronger than it is).
    Rule of thumb: |t| above 2 = probably real, below 2 = could be luck."""
    data = nxt.join(labels.rename("group"), how="inner").dropna()
    a = data[data["group"] == group]["fwd_return"].iloc[::HORIZON]
    b = data[data["group"] != group]["fwd_return"].iloc[::HORIZON]
    return (a.mean() - b.mean()) / np.sqrt(a.var() / len(a) + b.var() / len(b))


def print_table(title, table):
    print(f"\n  {title}")
    print(f"  {'':<10}{'days':>7}{'share':>8}{'next-20d return':>17}{'% negative':>12}{'next-20d vol':>14}")
    for name, r in table.iterrows():
        print(f"  {name:<10}{int(r['days']):>7}{danish(r['share_%'], 0) + '%':>8}"
              f"{danish(r['next20_return_%']) + '%':>17}{danish(r['next20_negative_%'], 0) + '%':>12}"
              f"{danish(r['next20_vol_%'], 1) + '%':>14}")


# --- Today's regime (used by weekly_check.py) -----------------

def current_regime(ticker):
    """Today's regime, the same way the walk-forward does it: model fitted
    on data before this month started, forward pass up to the latest close.
    Returns (label, probability, date)."""
    _, features = load_features(ticker)
    X, dates = features.values, features.index
    past = dates < dates[-1].replace(day=1)
    mean, std = X[past].mean(axis=0), X[past].std(axis=0)
    model = GaussianHMM()
    model.fit((X[past] - mean) / std)
    probs = model.filtered_probabilities((X - mean) / std)[-1]
    k = int(np.argmax(probs))
    return LABELS[k], probs[k], dates[-1]


# --- Chart ----------------------------------------------------

def draw_chart(close, regimes, save_to):
    fig, ax = plt.subplots(figsize=(14, 6))
    labels = regimes["regime"]
    runs = (labels != labels.shift()).cumsum()           # number each unbroken stretch
    for _, stretch in labels.groupby(runs):
        ax.axvspan(stretch.index[0], stretch.index[-1] + pd.Timedelta(days=1),
                   color=COLOURS[stretch.iloc[0]], alpha=0.25, linewidth=0)
    ax.plot(close.loc[labels.index[0]:], color="black", linewidth=0.8)
    ax.set_yscale("log")                                  # equal % moves = equal heights
    ax.set_title("S&P 500 with walk-forward HMM regimes (each day labelled using only past data)")
    ax.legend(handles=[Patch(color=COLOURS[l], alpha=0.4, label=l) for l in LABELS], loc="upper left")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(save_to, dpi=150)
    print(f"\nChart saved to: {save_to}")
    plt.show()


def main():
    OUTPUT_FOLDER.mkdir(exist_ok=True)
    all_tables = []
    sp_close = sp_regimes = None

    for ticker, name in MARKETS.items():
        print(f"\n=== {name} ({ticker}) ===")
        close, features = load_features(ticker)
        print("  Walking forward (refit every month on past data only) ...")
        regimes = walk_forward(features)
        nxt = what_happened_next(close)

        hmm = test_table(regimes["regime"], nxt)
        simple = test_table(regimes["simple_rule"], nxt)
        print(f"\n  Out-of-sample test, {regimes.index[0]:%Y-%m} to {regimes.index[-1]:%Y-%m}"
              f" (next-20d vol is yearly %)")
        print_table("HMM regime", hmm.reindex(LABELS + ["ALL DAYS"]))
        print_table("Simple rule: 20-day volatility vs. past thirds",
                    simple.reindex(["low vol", "mid vol", "high vol", "ALL DAYS"]))

        t_hmm = t_statistic(regimes["regime"], nxt, "stressed")
        t_simple = t_statistic(regimes["simple_rule"], nxt, "high vol")
        s, a = hmm.loc["stressed"], hmm.loc["ALL DAYS"]
        print(f"\n  After 'stressed': next-20d vol {danish(s['next20_vol_%'], 1)}% vs "
              f"{danish(a['next20_vol_%'], 1)}% on all days "
              f"({danish(s['next20_vol_%'] / a['next20_vol_%'], 2)}x).")
        print(f"  After 'stressed': next-20d return {danish(s['next20_return_%'])}% vs "
              f"{danish(a['next20_return_%'])}% on all days (t = {danish(t_hmm)}; "
              f"simple rule's 'high vol': t = {danish(t_simple)}).")

        latest = regimes.iloc[-1]
        print(f"  Latest ({regimes.index[-1]:%Y-%m-%d}): {latest['regime']} "
              f"({danish(latest['probability'] * 100, 0)}%)")

        for method, table in [("HMM", hmm), ("simple rule", simple)]:
            all_tables.append(table.assign(market=name, method=method))
        regimes.to_csv(OUTPUT_FOLDER / f"regime_history_{ticker.strip('^')}.csv",
                       sep=";", decimal=",", encoding="utf-8-sig", index_label="date")
        if ticker == "^GSPC":
            sp_close, sp_regimes = close, regimes

    pd.concat(all_tables).to_csv(OUTPUT_FOLDER / "regime_test.csv", sep=";", decimal=",",
                                 encoding="utf-8-sig", index_label="group")
    draw_chart(sp_close, sp_regimes, OUTPUT_FOLDER / "regime_sp500.png")


if __name__ == "__main__":
    main()
