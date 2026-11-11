# ============================================================
#  scanner.py  -  weekly scan of my candidate universe
#
#  Reads the [scanner] section of rules.toml. For every candidate:
#   - gates (size, profits, price, sector, near-copy, ASK-eligible)
#   - a score per role (growth / diversifier), ranked with my satellites
#   - a fit check against my portfolio rules
#   - one in, one out: is a candidate clearly better than my weakest?
#  Watchlist names ([[watchlist]] in rules.toml) get gates and fit too,
#  but are NOT ranked. Information only - never a buy instruction.
#
#  The gate, fit and scoring logic lives in scan_core.py (shared with
#  the lookup page), so both judge a ticker in exactly the same way.
# ============================================================

import json
import re
import sys

import pandas as pd

import data_cache

from portfolio import danish
from positions import SECURITIES, OUTPUT_FOLDER
from settings import write_text_safely, save_csv_safely   # crash-safe writes (temp file, then rename)
from scan_core import (PASS, FAIL, INFO, SKIP, SCANNER_FX, GATE_ORDER, EARNINGS_GROWTH_CAP, EXPLAIN,
                       load_rules, load_context, portfolio_weekly, fetch, fx_to_dkk, run_gates,
                       verdict, fit_for, raw_factors, score_role, score_text, eligible_candidates,
                       lookup_link)


def ask_param(ask_ok):
    """rules.toml ask_ok -> the lookup page's ask= parameter."""
    return {True: "yes", False: "no"}.get(ask_ok)


def explain_link(ticker, role, ask_ok=None, out=None):
    """Markdown link to the full explained fit check on the lookup page."""
    url = lookup_link(ticker, role=role, ask=ask_param(ask_ok),
                      mode="replace" if out else None, out=out)
    return f"[{EXPLAIN['fit_link']}]({url})"


def swaps_done_this_quarter(t, today, core_ticker):
    """How many satellites I've sold this quarter (from the transactions)."""
    quarter_start = pd.Timestamp(year=today.year, month=3 * ((today.month - 1) // 3) + 1, day=1)
    sold = t[(t["Transaktionstype"] == "SOLGT") & (t["date"] >= quarter_start)]
    tickers = {SECURITIES[i][0] for i in sold["ISIN"] if i in SECURITIES}
    return len(tickers - {core_ticker})


def ranking_lines(table, holdings):
    """Top 3 eligible candidates and my satellites, per role."""
    lines = []
    for role in ["growth", "diversifier"]:
        top = eligible_candidates(table, role).head(3)
        lines.append(f"{role} - top 3: " + ", ".join(
            f"{r['ticker']} {score_text(r['score'], r['star'])}" for _, r in top.iterrows()))
        mine = holdings[holdings["role"] == role].sort_values("score")   # unscored go last
        scored = mine["score"].dropna()
        weakest = scored.index[0] if len(scored) else None
        lines.append(f"{role} - my satellites: " + ", ".join(
            f"{r['ticker']} {score_text(r['score'], r['star'], long=True)}"
            + (" (weakest)" if idx == weakest else "")
            for idx, r in mine.iterrows()))
    return lines


def save_latest(table, holdings, verdicts, swaps_left, today, watchlist=()):
    """A small summary file that weekly_check.py, the dashboard and the
    lookup page read (so they don't have to re-run the whole scanner)."""
    summary = {"date": f"{today:%Y-%m-%d}", "swaps_left": swaps_left, "verdicts": verdicts, "roles": {}}
    for role in ["growth", "diversifier"]:
        top = eligible_candidates(table, role).head(3)
        mine = holdings[(holdings["role"] == role) & holdings["score"].notna()].sort_values("score")
        unscored = (list(table[(table["role"] == role) & table["score"].isna()]["ticker"])
                    + [f"{tk} (holding)" for tk in holdings[(holdings["role"] == role)
                                                            & holdings["score"].isna()]["ticker"]])
        summary["roles"][role] = {
            # every candidate that passed the gates and the fit check, with its score
            # (the action panel in weekly_check.py picks from these)
            "eligible": [{"ticker": r["ticker"], "score": float(r["score"])}
                         for _, r in eligible_candidates(table, role).iterrows()],
            "top": [[r["ticker"], score_text(r["score"], r["star"])] for _, r in top.iterrows()],
            "weakest": ([mine.iloc[0]["ticker"], score_text(mine.iloc[0]["score"], mine.iloc[0]["star"])]
                        if len(mine) else None),
            "not_scored": unscored,
        }
    # Swaps flagged as "worth considering", in a form other scripts can read.
    summary["swaps"] = [{"in": m.group(1), "out": m.group(2)} for v in verdicts
                        for m in [re.search(r"swap worth considering: (\S+) in, (\S+) out", v)] if m]
    summary["watchlist"] = list(watchlist)
    path = OUTPUT_FOLDER / "scanner" / "latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_safely(path, json.dumps(summary, ensure_ascii=False, indent=2))


def one_in_one_out(table, holdings, rules, swaps_left):
    """Per role: best eligible candidate vs. my weakest holding.
    Returns a list of verdict lines (all information, never an order)."""
    s = rules["scanner"]
    lines, suggestions = [], []
    for role in ["growth", "diversifier"]:
        eligible = eligible_candidates(table, role)
        mine = holdings[(holdings["role"] == role) & holdings["score"].notna()]
        if eligible.empty or mine.empty:
            lines.append(f"{role}: not enough data to compare")
            continue
        best = eligible.loc[eligible["score"].idxmax()]
        weakest = mine.loc[mine["score"].idxmin()]
        gap = best["score"] - weakest["score"]
        text = (f"{role}: best {best['ticker']} {danish(best['score'], 2)} vs. weakest "
                f"{weakest['ticker']} {danish(weakest['score'], 2)} (gap {danish(gap, 2)}, "
                f"needs {danish(s['min_score_gap'], 2)})")
        if gap >= s["min_score_gap"]:
            suggestions.append((gap, role, text, best["ticker"], weakest["ticker"]))
        else:
            lines.append(f"{text} -> keep current holdings")

    suggestions.sort(reverse=True)                    # biggest gap first
    for i, (gap, role, text, cand, held) in enumerate(suggestions):
        if i < swaps_left:
            lines.append(f"{text} -> swap worth considering: {cand} in, {held} out")
        else:
            lines.append(f"{text} -> keep current holdings (swap limit "
                         f"{s['max_swaps_per_quarter']}/quarter reached)")
    return lines


def save_markdown(table, order, today, path, extra=()):
    """Markdown report: passes first (best score first), then 'needs ASK
    check', then fails. `extra` = more lines added at the end."""
    rank = {"pass": 0, "needs ASK check": 1, "fail": 2}
    table = table.assign(_rank=table["verdict"].str.rstrip("*").map(rank)) \
                 .sort_values(["_rank", "score"], ascending=[True, False])

    lines = [f"# Scanner {today:%Y-%m-%d}",
             "",
             "Information only - never a buy instruction. "
             f"{EXPLAIN['legend']} · – not applicable. "
             "`*` = some data missing (see notes); a starred score left out a missing factor. "
             "Fit is the short summary; 'explain' opens the full explained fit check on the lookup page "
             "(start it with 'Start lookup.bat').",
             "",
             "| Ticker | Name | Role | Sector / industry | Cap bn EUR | DKK/share | "
             + " | ".join(order) + " | Fit | Score | Verdict | Notes |",
             "|" + "---|" * (len(order) + 11)]
    for _, r in table.iterrows():
        cap = "" if pd.isna(r["cap_bn_eur"]) else danish(r["cap_bn_eur"], 0)
        price = "" if pd.isna(r["price_dkk"]) else danish(r["price_dkk"], 0)
        notes = [f"{g}: {r[g + '_reason']}" for g in order if r[g] in (FAIL, INFO) and g != "ASK"]
        if r["Fit"] != SKIP:
            notes.append(f"Fit: {r['Fit_reason']} · {r['fit_details']} · "
                         + explain_link(r["ticker"], r["role"], r["ask_ok"]))
        lines.append(f"| {r['ticker']} | {r['name']} | {r['role']} | {r['sector_industry']} | {cap} | {price} | "
                     + " | ".join(r[g] for g in order)
                     + f" | {r['Fit']} | {score_text(r['score'], r['star'])} | {r['verdict']} | {'; '.join(notes)} |")
    lines += ["", summary_line(table), "", *extra]
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_safely(path, "\n".join(lines) + "\n")


def summary_line(table):
    v = table["verdict"].str.rstrip("*")
    return (f"**{(v == 'pass').sum()} passed, {(v == 'fail').sum()} failed, "
            f"{(v == 'needs ASK check').sum()} need an ASK check.**")


# --- Main -----------------------------------------------------

def main():
    sys.stdout.reconfigure(encoding="utf-8")
    rules = load_rules()
    universe = rules["scanner"]["universe"]
    watchlist = rules.get("watchlist", [])
    today = pd.Timestamp.today().normalize()

    # My current holdings, in DKK, from the transaction rebuild.
    print("Rebuilding my holdings ...")
    ctx = load_context(rules, today)
    values, core, holdings_daily = ctx["values"], ctx["core"], ctx["holdings_daily"]

    # Fetch every candidate (and watchlist name); one failure must not stop the rest.
    data, errors = {}, []
    print(f"Fetching {len(universe)} candidates and {len(watchlist)} watchlist names (cache first) ...")
    # Prices for every candidate, my satellites and the exchange rates, in a few batched requests.
    data_cache.prefetch([c["ticker"] for c in list(universe) + list(watchlist)]
                        + list(rules["scanner"]["holding_roles"]) + list(SCANNER_FX.values()),
                        today - pd.DateOffset(years=2))
    for c in list(universe) + list(watchlist):
        try:
            data[c["ticker"]] = fetch(c["ticker"])
        except Exception as e:
            data[c["ticker"]] = None
            errors.append(f"{c['ticker']}: {type(e).__name__}: {e}")

    fx = fx_to_dkk([d["currency"] for d in data.values() if d], ctx["days"])

    # Step 1: gates for every candidate.
    order = GATE_ORDER
    gates_of = {c["ticker"]: run_gates(c, data[c["ticker"]], rules, fx, ctx["holdings_weekly"], today)
                for c in list(universe) + list(watchlist)}

    # Step 2: scores. Candidates AND my satellites go into one ranking per role.
    print("Scoring candidates and my satellites ...")
    roles_of = {c["ticker"]: c["role"] for c in universe}
    raw_rows = {}
    for c in universe:
        d = data[c["ticker"]]
        if d is not None:
            raw_rows[c["ticker"]] = raw_factors(c["ticker"], d, d.get("daily_dkk"),
                                                holdings_daily[core], portfolio_weekly(ctx), today)
    my_satellites = {tk: role for tk, role in rules["scanner"]["holding_roles"].items()
                     if tk in values.index}
    for tk, role in my_satellites.items():
        try:
            d = fetch(tk)
        except Exception as e:
            errors.append(f"{tk} (holding): {type(e).__name__}: {e}")
            continue
        roles_of[tk] = role
        raw_rows[tk] = raw_factors(tk, d, holdings_daily[tk], holdings_daily[core],
                                   portfolio_weekly(ctx, exclude=tk), today)

    raw = pd.DataFrame(raw_rows).T
    scores, stars = pd.Series(dtype=float), pd.Series(dtype=bool)
    for role, weights in rules["scanner"]["weights"].items():
        members = [x for x in raw.index if roles_of.get(x) == role]
        sc, st = score_role(raw.loc[members], weights, rules["scanner"]["min_factors"])
        scores, stars = pd.concat([scores, sc]), pd.concat([stars, st])

    # Keep the raw numbers behind every score, so they can be checked
    # (the lookup page also ranks a new ticker against this file).
    (OUTPUT_FOLDER / "scanner").mkdir(parents=True, exist_ok=True)
    raw.assign(role=pd.Series(roles_of), score=scores, missing=stars).sort_values(["role", "score"]) \
       .pipe(save_csv_safely, OUTPUT_FOLDER / "scanner" / f"factors_{today:%Y-%m-%d}.csv",
             sep=";", decimal=",", encoding="utf-8-sig", index_label="ticker")

    holdings = pd.DataFrame([{"ticker": tk, "role": role, "score": scores.get(tk, float("nan")),
                              "star": bool(stars.get(tk, False))} for tk, role in my_satellites.items()])
    weakest = {role: g.loc[g["score"].idxmin(), "ticker"]
               for role, g in holdings.dropna(subset=["score"]).groupby("role")}

    # Fit check (against my WEAKEST same-role satellite) and the result table.
    rows = []
    for c in universe:
        d = data[c["ticker"]]
        gates = gates_of[c["ticker"]]
        result = verdict(gates)
        fit, fit_reason, fit_details = fit_for(c, d, rules, ctx, result, swap_out=weakest.get(c["role"]))

        row = {"ticker": c["ticker"], "name": c["name"], "type": c["type"],
               "role": c.get("role", "?"),
               "sector_industry": " / ".join(x for x in ((d or {}).get("sector"), (d or {}).get("industry")) if x)
                                  or c["sector"],
               "cap_bn_eur": (d or {}).get("cap_eur", float("nan")) / 1e9,
               "price_dkk": (d or {}).get("price_dkk", float("nan")),
               "score": scores.get(c["ticker"], float("nan")),
               "star": bool(stars.get(c["ticker"], False)),
               "verdict": result,
               "Fit": fit, "Fit_reason": fit_reason, "fit_details": fit_details,
               "ask_ok": c.get("ask_ok", "unknown")}
        for gate in order:
            sym, reason = gates.get(gate, (SKIP, "not checked"))
            row[gate] = sym
            row[gate + "_reason"] = reason
        rows.append(row)
    table = pd.DataFrame(rows)

    # Watchlist: gates and fit only - NOT scored, NOT in the rankings.
    watch_results = []
    for c in watchlist:
        d = data[c["ticker"]]
        gates = gates_of[c["ticker"]]
        result = verdict(gates)
        fit = fit_for(c, d, rules, ctx, result, swap_out=weakest.get(c["role"]))
        problems = [f"{g} {gates[g][1]}" for g in order if g in gates and gates[g][0] == FAIL]
        watch_results.append({"ticker": c["ticker"], "name": c.get("name", c["ticker"]), "role": c["role"],
                              "gates": "".join(gates.get(g, (SKIP, ""))[0] for g in order),
                              "verdict": result, "problems": problems,
                              "fit": fit[0], "fit_reason": fit[1],
                              "explain": lookup_link(c["ticker"], role=c["role"], ask=ask_param(c.get("ask_ok"))),
                              "thesis": c.get("thesis", ""), "kill_rule": c.get("kill_rule", ""),
                              "added": str(c.get("added", ""))})

    # Step 3: one in, one out.
    swaps_left = max(0, rules["scanner"]["max_swaps_per_quarter"]
                     - swaps_done_this_quarter(ctx["transactions"], today, core))
    verdicts = one_in_one_out(table, holdings, rules, swaps_left)
    ranking = ranking_lines(table, holdings)
    capped = sorted(raw.index[raw["growth_capped"] == True])     # noqa: E712 (column holds True/False)
    if capped:
        ranking.append(f"earnings growth capped at +/-{EARNINGS_GROWTH_CAP}%: " + ", ".join(capped))

    # --- Print ---
    print(f"\n# Scanner {today:%Y-%m-%d} (information only - never a buy instruction)\n")
    # Gate columns: an emoji takes 2 places on screen, so each column is
    # the symbol plus 3 spaces, under a 5-letter header.
    headers = {"Yahoo": "Yah", "Cap": "Cap", "EPS": "EPS", "FCF": "FCF", "Debt": "Debt",
               "Price": "Prc", "Sector": "Sect", "Corr": "Corr", "ASK": "ASK", "Fit": "Fit"}
    cols = order + ["Fit"]
    print(f"{'Ticker':<10}{'Name':<15}{'Role':<5}{'Sector / industry':<25}{'Cap bn€':>8}{'DKK/sh':>8}  "
          + "".join(f"{headers[g]:<5}" for g in cols) + f"{'Score':<11}Verdict")
    for _, r in table.iterrows():
        cap = "" if pd.isna(r["cap_bn_eur"]) else danish(r["cap_bn_eur"], 0)
        price = "" if pd.isna(r["price_dkk"]) else danish(r["price_dkk"], 0)
        role = {"growth": "G", "diversifier": "D"}.get(r["role"], "?")
        print(f"{r['ticker']:<10}{r['name'][:14]:<15}{role:<5}{r['sector_industry'][:24]:<25}{cap:>8}{price:>8}  "
              + "".join(f"{r[g]}   " if r[g] != SKIP else f"{r[g]}    " for g in cols)
              + f"{score_text(r['score'], r['star']):<11}" + r["verdict"])

    print("\nFit check (candidates that passed the gates):")
    for _, r in table[table["Fit"] != SKIP].iterrows():
        print(f"  {r['ticker']:<10}{r['Fit']} {r['Fit_reason']} · {r['fit_details']}")

    print("\nReasons for ❌ and ℹ️:")
    for _, r in table.iterrows():
        notes = [f"{g} {r[g]} {r[g + '_reason']}" for g in order if r[g] in (FAIL, INFO)]
        cap_note = r["Cap_reason"] if "check:" in str(r["Cap_reason"]) else None
        if cap_note and r["Cap"] == PASS:
            notes.append(f"Cap ℹ️ {cap_note}")
        if notes:
            print(f"  {r['ticker']:<10}" + " | ".join(notes))

    if watch_results:
        print("\nWatchlist (gates and fit only, not ranked):")
        for w in watch_results:
            print(f"  {w['ticker']:<10}{w['gates']}  {w['verdict']} · fit {w['fit']} {w['fit_reason']}")

    if errors:
        print("\nFetch errors (logged, skipped):")
        for e in errors:
            print("  " + e)

    print("\nScores (0 = worst, 1 = best within the role; * = some factor missing):")
    for line in ranking:
        print("  " + line)
    print(f"\nOne in, one out (swaps left this quarter: {swaps_left}):")
    for line in verdicts:
        print(f"  {INFO} {line}")

    watch_md = (["", "## Watchlist (gates and fit only, not ranked)", ""]
                + [f"- {INFO} {w['ticker']} ({w['role']}): {w['gates']} {w['verdict']} · fit {w['fit']} "
                   f"{w['fit_reason']} · [{EXPLAIN['fit_link']}]({w['explain']})" for w in watch_results]
                if watch_results else [])
    # Swap lines get a link to the head-to-head comparison (replace mode).
    asks = {c["ticker"]: c.get("ask_ok") for c in universe}
    verdicts_md = []
    for v in verdicts:
        m = re.search(r"swap worth considering: (\S+) in, (\S+) out", v)
        role = v.split(":")[0]
        verdicts_md.append(v + (" · " + explain_link(m.group(1), role, asks.get(m.group(1)), out=m.group(2))
                                if m else ""))
    path = OUTPUT_FOLDER / "scanner" / f"{today:%Y-%m-%d}.md"
    save_markdown(table, order, today, path,
                  extra=["## Scores", ""] + [f"- {x}" for x in ranking]
                        + ["", f"## One in, one out (swaps left this quarter: {swaps_left})", ""]
                        + [f"- {INFO} {x}" for x in verdicts_md] + watch_md)
    save_latest(table, holdings, verdicts, swaps_left, today, watch_results)
    print(f"\n{summary_line(table).strip('*')}")
    print(f"Report saved to: {path}")


if __name__ == "__main__":
    main()
