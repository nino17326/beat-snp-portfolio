# ============================================================
#  lookup.py  -  check ONE ticker against my rules (Streamlit page)
#
#  Start it with "Start lookup.bat" (or: py -m streamlit run lookup.py)
#  and open http://localhost:8501/?ticker=SAF.PA - or use the search box
#  on the dashboard, which opens exactly that address.
#
#  Two modes:
#    New idea          - compared with my weakest satellite in the same role
#    Replace a holding - compared head-to-head with the holding I pick
#  Optional address parameters open it straight on a result, e.g.
#    ?ticker=AVGO&mode=replace&out=ASML.AS&ask=yes&run=1
#
#  It uses the SAME gate, fit and scoring code as the weekly scanner
#  (scan_core.py), so a ticker is judged exactly the same way here; all
#  the plain-English texts and tooltips come from scan_core.EXPLAIN.
#  The two buttons edit rules.toml in the data folder (outside git):
#  first a dated backup copy goes to rules_backup\, then only the new
#  lines are added. Information only - never a buy instruction.
# ============================================================

import difflib
import html
import json
import re
import shutil
import sys
import tomllib
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))   # so the project's own modules import
from portfolio import danish
from positions import SECURITIES, OUTPUT_FOLDER
from settings import BACKUP_FOLDER
from scan_core import (PASS, FAIL, INFO, SKIP, WARN, GATE_ORDER, RULES_FILE, EXPLAIN, load_rules,
                       load_context, portfolio_weekly, fetch, fx_to_dkk, run_gates, verdict,
                       fit_numbers, fit_summary, fit_explained, same_sector_swap, apply_same_sector_swap,
                       raw_factors, rank_with_latest, score_text, head_to_head, comparison_rows,
                       verdict_parts, term_tip)
from weekly_check import valuation, estimate_revisions, story_status

# Yahoo's sector names -> the labels I use in rules.toml.
YAHOO_SECTOR = {"Healthcare": "Health Care", "Consumer Defensive": "Consumer Staples",
                "Consumer Cyclical": "Consumer Discretionary", "Basic Materials": "Materials",
                "Financial Services": "Financials"}
ASK_CHOICES = {"not checked": "unknown", "yes": True, "no": False}
MODES = {"new": "New idea", "replace": "Replace a holding"}


# --- Reading and editing rules.toml (line by line, never rewritten) ---

def toml_value(v):
    """A Python value written the TOML way."""
    if v is True or v is False:
        return "true" if v else "false"
    if isinstance(v, pd.Timestamp):
        return f"{v:%Y-%m-%d}"                                   # dates have no quotes
    return json.dumps(str(v), ensure_ascii=False)               # "text" with safe escaping


def already_listed(rules, ticker):
    """Where this ticker already is: 'holding', 'scanner', 'watchlist' (or none)."""
    places = []
    held = {t for t, _ in SECURITIES.values()}
    if ticker in held:
        places.append("holding")
    if any(c["ticker"] == ticker for c in rules["scanner"]["universe"]):
        places.append("scanner")
    if any(c["ticker"] == ticker for c in rules.get("watchlist", [])):
        places.append("watchlist")
    return places


def with_watchlist_entry(text, entry):
    """rules.toml text + one new [[watchlist]] block at the very end."""
    header = "" if "[[watchlist]]" in text else (
        "\n\n# ------------------------------------------------------------\n"
        "#  WATCHLIST (added from the lookup page). Gates and fit are checked\n"
        "#  every week, but these names are NOT scored or ranked.\n"
        "# ------------------------------------------------------------")
    block = "\n\n[[watchlist]]\n" + "".join(f"{k} = {toml_value(v)}\n" for k, v in entry.items())
    return text.rstrip("\n") + header + block


def with_universe_entry(text, entry):
    """rules.toml text + one new line inside [scanner] universe = [ ... ].
    Stocks go just above the '# --- ETFs ---' line, ETFs just above ']'."""
    lines = text.split("\n")
    start = next(i for i, l in enumerate(lines) if l.startswith("universe = ["))
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == "]")
    etfs = next((i for i in range(start, end) if lines[i].strip() == "# --- ETFs ---"), end)
    at = etfs if entry["type"] == "stock" else end
    new = "    { " + ", ".join(f"{k} = {toml_value(v)}" for k, v in entry.items()) + " },"
    return "\n".join(lines[:at] + [new] + lines[at:])


def backup_and_save(old_text, new_text, ticker, where):
    """Check the edit, save a dated backup of rules.toml, then save the new text.
    rules.toml lives outside git, so the backup is the safety net."""
    rules = tomllib.loads(new_text)                              # raises if I broke the file
    listed = rules.get("watchlist", []) if where == "watchlist" else rules["scanner"]["universe"]
    assert any(c["ticker"] == ticker for c in listed), "entry not found after editing"
    # Only ADDED lines are allowed: no existing line may change or disappear
    # (a trailing blank line at the very end doesn't count).
    removed = [l for l in difflib.ndiff(old_text.split("\n"), new_text.split("\n"))
               if l.startswith("- ") and l[2:].strip()]
    assert not removed, f"edit would change existing lines: {removed[:3]}"
    BACKUP_FOLDER.mkdir(exist_ok=True)
    backup = BACKUP_FOLDER / f"rules_{datetime.now():%Y-%m-%d_%H%M%S}.toml"
    shutil.copy2(RULES_FILE, backup)                             # copy BEFORE writing
    RULES_FILE.write_text(new_text, encoding="utf-8")
    return backup


# --- Data (cached so the page stays quick) --------------------

@st.cache_data(ttl=900, show_spinner="Asking Yahoo ...")
def get_data(ticker):
    return fetch(ticker)


@st.cache_resource(ttl=3600, show_spinner="Rebuilding my holdings ...")
def get_context(day):
    return load_context(load_rules(), pd.Timestamp(day))


def latest_scan():
    path = OUTPUT_FOLDER / "scanner" / "latest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def short_name(name, ticker):
    """'NVIDIA Corporation' -> 'NVIDIA' (a readable name for sentences)."""
    if not name:
        return ticker
    cut = re.sub(r"[,]?\s+(Inc\.?|Incorporated|Corporation|Corp\.?|Company|Co\.?|Holdings?|Group|plc|PLC|"
                 r"N\.V\.|NV|S\.A\.|SA|SE|AG|A/S|AB|ASA|Ltd\.?|Limited|Aktiengesellschaft)\b\.?", "", name)
    return cut.strip(" ,") or name


def holding_name(rules, ticker):
    stock = rules.get("stocks", {}).get(ticker, {})
    return stock.get("name") or ticker


def today_day():
    return pd.Timestamp.today().normalize()


def analyse(ticker, form, mode, out):
    """Gates, fit, score + rank, valuation and EPS revisions for one ticker,
    using the same functions as the weekly scanner. mode = 'new' or 'replace'
    (then `out` is the holding I picked)."""
    rules = load_rules()
    today = today_day()
    ctx = get_context(f"{today:%Y-%m-%d}")
    d = dict(get_data(ticker))                                   # a fresh copy each time
    c = {"ticker": ticker, "name": d["name"] or ticker, "role": form["role"], "sector": form["sector"],
         "type": "etf" if d["quote_type"] == "ETF" else "stock", "ask_ok": ASK_CHOICES[form["ask"]]}
    replace = mode == "replace"

    scan = latest_scan()
    weakest_entry = scan.get("roles", {}).get(form["role"], {}).get("weakest") or [None, None]
    weakest = weakest_entry[0]
    other = out if replace else weakest

    # Replace mode: the holding I'd sell doesn't count for the near-copy check
    # or the portfolio correlation, and a same-sector swap skips the exclusion.
    same = False
    holdings_weekly, port_weekly = ctx["holdings_weekly"], portfolio_weekly(ctx)
    if replace:
        same = same_sector_swap(c, d, rules, out, get_data(out))
        holdings_weekly = holdings_weekly.drop(columns=out, errors="ignore")
        port_weekly = portfolio_weekly(ctx, exclude=out)

    fx = fx_to_dkk([d["currency"]], ctx["days"])
    gates = run_gates(c, d, rules, fx, holdings_weekly, today)
    sector_exempt = False
    if replace and same and gates.get("Sector", (None,))[0] == FAIL:
        apply_same_sector_swap(gates, out)
        sector_exempt = True
    result = verdict(gates)

    prices_dkk = ctx["holdings_daily"]
    if "daily_dkk" in d:
        prices_dkk = prices_dkk.join(d["daily_dkk"].rename(ticker), how="outer").ffill()
    # Shown even if a gate fails, so I can see what it WOULD do to my portfolio.
    n = fit_numbers(c, d, rules, ctx["values"], ctx["cash"], prices_dkk, today,
                    swap_out=other, replace=replace, same_sector=same)
    fit = fit_summary(n)

    raw = raw_factors(ticker, d, d.get("daily_dkk"), ctx["holdings_daily"][ctx["core"]], port_weekly, today)
    score, star, rank, count = rank_with_latest(ticker, form["role"], raw, rules)
    eps_line = estimate_revisions(ticker, rules["alerts"].get("estimate_cut_pct", 5))

    # One-line verdict.
    minimum = rules["scanner"]["min_candidate_score"]
    failed = [f"{g}: {gates[g][1]}" for g in GATE_ORDER if g in gates and gates[g][0] == FAIL]
    if failed:
        line = (FAIL, "Fails the gates - " + "; ".join(failed))
    elif fit[0] == FAIL:
        line = (FAIL, "Passes the gates but breaks my portfolio rules - " + fit[1])
    elif pd.isna(score):
        line = (INFO, "Passes the gates and fit, but can't be scored (too few factors) - watchlist at most")
    else:
        bar = "above" if score >= minimum else "below"
        line = (PASS if score >= minimum else INFO,
                f"Passes the gates and fit ({fit[1]}); score {score_text(score, star)} "
                f"is {bar} my {danish(minimum, 2)} bar (#{rank} of {count} {form['role']} names)")
    if gates.get("ASK", (None,))[0] == INFO and not failed:
        line = (line[0], line[1] + " · ASK not checked yet")

    # Plain-English parts: names, side-by-side comparison, explained fit, 3-part verdict.
    names = {ticker: short_name(d["name"], ticker)}
    if other:
        names[other] = holding_name(rules, other)
        if names[other] == other:
            names[other] = short_name(get_data(other)["name"], other)
    explained = fit_explained(n, names, score_of={weakest: weakest_entry[1]} if weakest else None)
    h2h = head_to_head(ticker, raw, other, form["role"], rules) if other else None
    watch = next((w for w in rules.get("watchlist", []) if w["ticker"] == ticker), {})
    thesis_state = (bool(form["thesis"] or watch.get("thesis")), bool(form["kill_rule"] or watch.get("kill_rule")))
    story = story_status(rules, today).get(other) if other else None
    return {"c": c, "d": d, "gates": gates, "result": result, "fit": fit, "n": n, "weakest": weakest,
            "other": other, "mode": mode, "same_sector": same, "sector_exempt": sector_exempt,
            "score": score, "star": star, "rank": rank, "n_ranked": count, "raw": raw,
            "valuation": valuation(ticker), "eps": eps_line.strip().lstrip("- ").strip(), "line": line,
            "names": names, "explained": explained, "h2h": h2h,
            "rows": comparison_rows(h2h, names, ticker, other) if h2h else [],
            "verdict": verdict_parts(h2h, names, ticker, other, rules, story, thesis_state) if h2h else None}


# --- Small display helpers ------------------------------------

def tip(term):
    """A hover ⓘ with the plain-English explanation of a term."""
    return (f'<span title="{html.escape(term_tip(term))}" style="cursor:help;opacity:.7">ⓘ</span>'
            if term_tip(term) else "")


def comparison_card(r):
    """Side-by-side table: one row per scoring factor, plus a translation line."""
    cand, other = r["c"]["ticker"], r["other"]
    A, B = html.escape(r["names"][cand]), html.escape(r["names"][other])
    e = html.escape
    rows = []
    for x in r["rows"]:
        mark = lambda t: " ✅" if x["winner"] == t else ""                       # noqa: E731
        rows.append(
            f"<tr><td><strong>{e(x['label'])}</strong> {tip(x['term'])} "
            f"<span style='opacity:.6'>· weight {x['weight'] * 100:.0f}%</span>"
            f"<br><small>{e(x['measures'])}</small></td>"
            f"<td style='white-space:nowrap'>{e(x['cand'])}{mark(cand)}</td>"
            f"<td style='white-space:nowrap'>{e(x['other'])}{mark(other)}</td></tr>"
            f"<tr><td colspan='3' style='border-top:none;padding-top:0'><small>↳ {e(x['line'])}</small></td></tr>")
    h = r["h2h"]
    a, b = h["cand"]["score"], h["other"]["score"]
    sa, sb = score_text(a, h["cand"]["star"]), score_text(b, h["other"]["star"])
    win_a = not pd.isna(a) and not pd.isna(b) and a > b
    win_b = not pd.isna(a) and not pd.isna(b) and b > a
    rows.append(f"<tr><td><strong>Score</strong> {tip('score')} <span style='opacity:.6'>· percentile ranks "
                f"{tip('percentile rank')}, weighted</span></td>"
                f"<td><strong>{e(sa)}{' ✅' if win_a else ''}</strong></td>"
                f"<td><strong>{e(sb)}{' ✅' if win_b else ''}</strong></td></tr>")
    return (f"<table style='width:100%'><tr><th>Factor</th><th>{A}</th><th>{B}</th></tr>"
            + "".join(rows) + "</table>")


# --- The page -------------------------------------------------

st.set_page_config(page_title="Ticker lookup", page_icon="🔎", layout="centered")
st.title("🔎 Ticker lookup")
st.caption("Checks one ticker against my rules - the same gates, fit check and scoring as the weekly "
           "scanner. Information only, never a buy instruction.")
st.markdown(f"**Legend:** {EXPLAIN['legend']}")

q = st.query_params
rules = load_rules()
mode_label = st.radio("Mode", list(MODES.values()), horizontal=True,
                      index=1 if q.get("mode") == "replace" else 0)
mode = next(k for k, v in MODES.items() if v == mode_label)

out = None
if mode == "replace":
    ctx_now = get_context(f"{today_day():%Y-%m-%d}")
    held = [t for t in rules["scanner"].get("holding_roles", {}) if t in ctx_now["values"].index]
    if not held:
        st.warning("No satellites found in rules.toml [scanner.holding_roles] that I currently hold.")
        st.stop()
    roles_of = rules["scanner"]["holding_roles"]
    out = st.selectbox("Holding to replace", held,
                       index=held.index(q["out"]) if q.get("out") in held else 0,
                       format_func=lambda t: f"{t} · {holding_name(rules, t)} ({roles_of[t]})")

ticker = st.text_input("Yahoo ticker", value=q.get("ticker", ""),
                       placeholder="e.g. SAF.PA, SAP.DE, EXPN.L").strip().upper()
if not ticker:
    st.stop()
if q.get("ticker") != ticker:
    q["ticker"] = ticker                                         # keep the address in sync
if ticker == out:
    st.warning("Pick a different ticker - that's the holding being replaced.")
    st.stop()

try:
    info = get_data(ticker)
except Exception as e:
    st.error(f"Yahoo doesn't know '{ticker}' ({type(e).__name__}). Check the ticker and suffix (.PA, .DE, .CO ...).")
    st.stop()

# 1. Confirm it's the right security before analysing.
st.subheader(info["name"] or ticker)
c1, c2, c3 = st.columns(3)
c1.metric("Exchange", info["exchange"] or "?")
c2.metric("Currency", info["currency"] or "?")
c3.metric("Type", "ETF" if info["quote_type"] == "ETF" else "Stock")
st.caption(f"Yahoo sector / industry: {info['sector'] or '-'} / {info['industry'] or '-'}"
           + (f" · last complete close {info['closes'].index[-1]:%Y-%m-%d} (not intraday)"
              if len(info["closes"]) else ""))

listed = already_listed(rules, ticker)
if listed:
    st.info(f"{ticker} is already in: {', '.join(listed)}.")

# 2. The checklist.
st.subheader("Checklist")
role_options = ["growth", "diversifier"]
default_role = rules["scanner"]["holding_roles"].get(out) if mode == "replace" else q.get("role")
role = st.radio("Role (required)" + (" - defaults to the holding's role" if mode == "replace" else ""),
                role_options, horizontal=True, key=f"role_{mode}_{out}",
                index=role_options.index(default_role) if default_role in role_options else None)
sectors = sorted({c["sector"] for c in rules["scanner"]["universe"]}
                 | set(rules["scanner"].get("holding_sectors", {}).values()))
guess = YAHOO_SECTOR.get(info["sector"], info["sector"] or "")
if "semiconductor" in (info["industry"] or "").lower():
    guess = "Semiconductors"
options = sectors if guess in sectors or not guess else sorted(sectors + [guess])
sector = st.selectbox("Sector (my label)", options, index=options.index(guess) if guess in options else 0)
# ASK: from the address, else what rules.toml already says about this ticker.
known = next((c.get("ask_ok") for c in rules["scanner"]["universe"] + rules.get("watchlist", [])
              if c["ticker"] == ticker), None)
ask_default = {"yes": 1, "no": 2}.get(q.get("ask", ""), {True: 1, False: 2}.get(known, 0))
ask = st.radio("ASK-eligible on Nordnet?", list(ASK_CHOICES), horizontal=True, index=ask_default)
thesis = st.text_area("Thesis - why would I own it?")
kill_rule = st.text_area("Kill rule - what would make me sell?")
form = {"role": role, "sector": sector, "ask": ask, "thesis": thesis.strip(), "kill_rule": kill_rule.strip()}
key = (ticker, mode, out, role, sector, ask, form["thesis"] != "", form["kill_rule"] != "")

auto = q.get("run") == "1" and role is not None
if st.button("Check fit", type="primary", disabled=role is None, help="Choose a role first") or \
        (auto and st.session_state.get("result_key") != key):
    st.session_state["result"] = analyse(ticker, form, mode, out)
    st.session_state["result_key"] = key
    if "run" in q:
        del q["run"]                                             # auto-run only once

result = st.session_state.get("result")
if not result or st.session_state.get("result_key") != key:
    st.stop()                                                    # nothing (current) to show yet
r = result
cand, other = ticker, r["other"]
A = r["names"][cand]
B = r["names"].get(other, other)

# 3. The analysis.
st.subheader("Result")
sym, text = r["line"]
(st.success if sym == PASS else st.error if sym == FAIL else st.info)(f"{sym} {text}")
if r["sector_exempt"]:
    st.warning(f"{WARN} {EXPLAIN['same_sector_swap']}")

st.markdown("**Gates**")
st.markdown("\n".join(f"- {r['gates'].get(g, (SKIP, 'not checked'))[0]} **{g}** - "
                      f"{r['gates'].get(g, (SKIP, 'not checked'))[1]}" for g in GATE_ORDER))
if mode == "replace":
    st.caption(EXPLAIN["corr_swap"].format(out=B))

# Side-by-side comparison.
if other is None:
    st.info(f"No {role} satellite to compare with (no scanner run yet, or none in this role).")
elif r["h2h"] is None:
    st.info("No scanner run yet (output/scanner/factors_*.csv), so there's nothing to compare against. "
            "Run scanner.py first.")
else:
    vs = "the holding you picked" if mode == "replace" else f"your weakest {role} satellite"
    st.subheader(f"{A} vs. {B}")
    st.caption(f"Compared with {vs}. Both are ranked against the same group: everything the last scanner "
               f"run scored as {role}.")
    st.markdown(comparison_card(r), unsafe_allow_html=True)

# Fit check, explained.
ex = r["explained"]
st.subheader(EXPLAIN["fit_title"])
for x in ex["lines"]:
    st.markdown(f"{x['status']} **{x['check']}**: {x['result']}")
    st.caption(f"What it means: {x['meaning']}")
st.markdown(f"**Summary:** {ex['summary']}")

# Verdict in three parts.
v = r["verdict"]
if v:
    st.subheader("Verdict")
    st.markdown(f"**Score.** {v['score']}")
    if v["why"]:
        st.markdown("**Why.**\n" + "\n".join(f"- {w}" for w in v["why"]))
    st.markdown("**What the numbers can't tell you.**\n" + "\n".join(f"- {s}" for s in v["story"]))

with st.expander("Details: score, valuation, EPS revisions"):
    rank_text = f"#{r['rank']} of {r['n_ranked']} {role} names" if r["rank"] else "not ranked"
    st.markdown(f"**Score** {score_text(r['score'], r['star'], long=True)} {tip('percentile rank')} · "
                f"{rank_text} (against the last scanner run)", unsafe_allow_html=True)
    st.caption("Raw factors: " + " · ".join(f"{k} {danish(val, 1) if isinstance(val, (int, float)) else 'n/a'}"
                                            for k, val in r["raw"].items() if k != "growth_capped"))
    st.markdown(f"**Valuation** {r['valuation']} {tip('P/E')} {tip('PEG')}", unsafe_allow_html=True)
    st.markdown(f"**EPS revisions** {tip('EPS revisions')} {r['eps']}", unsafe_allow_html=True)
    st.caption(f"Short fit line (as in the scanner and dashboard): {r['fit'][0]} {r['fit'][1]} · {r['fit'][2]}")

# 4. Save it (each backs up rules.toml, then adds only its own lines).
st.subheader("Save")
today = today_day()
entry = {"ticker": ticker, "name": r["c"]["name"], "type": r["c"]["type"], "sector": sector, "role": role,
         "ask_ok": ASK_CHOICES[ask], "thesis": form["thesis"], "kill_rule": form["kill_rule"], "added": today}
# The same-sector exemption only applies to this swap, not to the weekly scanner.
passes_gates = not any(sym == FAIL for sym, _ in r["gates"].values()) and not r["sector_exempt"]
scanner_ready = passes_gates and ask == "yes" and form["thesis"] and form["kill_rule"]

b1, b2 = st.columns(2)
if b1.button("Add to watchlist"):
    rules = load_rules()
    if "watchlist" in already_listed(rules, ticker):
        st.warning(f"{ticker} is already on the watchlist - not added twice.")
    else:
        old = RULES_FILE.read_text(encoding="utf-8")
        backup = backup_and_save(old, with_watchlist_entry(old, entry), ticker, "watchlist")
        st.success(f"Added {ticker} to the watchlist. Previous rules.toml saved as rules_backup\\{backup.name}")
if b2.button("Add to scanner group", disabled=not scanner_ready,
             help="Needs: passes the gates, ASK = yes, thesis and kill rule filled in"):
    rules = load_rules()
    if "scanner" in already_listed(rules, ticker):
        st.warning(f"{ticker} is already in the scanner group - not added twice.")
    else:
        old = RULES_FILE.read_text(encoding="utf-8")
        backup = backup_and_save(old, with_universe_entry(old, entry), ticker, "scanner")
        st.success(f"Added {ticker} to the scanner group. Previous rules.toml saved as rules_backup\\{backup.name}")
if not scanner_ready:
    st.caption("'Add to scanner group' unlocks when it passes the gates (without a same-sector exemption), "
               "ASK = yes, and the thesis and kill rule are filled in.")
