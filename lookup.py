# ============================================================
#  lookup.py  -  check ONE ticker against my rules (Streamlit page)
#
#  Start it with "Start lookup.bat" (or: py -m streamlit run lookup.py)
#  and open http://localhost:8501/?ticker=SAF.PA - or use the search box
#  on the dashboard, which opens exactly that address.
#
#  It uses the SAME gate, fit and scoring code as the weekly scanner
#  (scan_core.py), so a ticker is judged exactly the same way here.
#  The two buttons edit rules.toml in the data folder (outside git):
#  first a dated backup copy goes to rules_backup\, then only the new
#  lines are added. Information only - never a buy instruction.
# ============================================================

import difflib
import json
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
from scan_core import (PASS, FAIL, INFO, SKIP, GATE_ORDER, RULES_FILE, load_rules, load_context,
                       portfolio_weekly, fetch, fx_to_dkk, run_gates, verdict, fit_check,
                       raw_factors, rank_with_latest, score_text)
from weekly_check import valuation, estimate_revisions

# Yahoo's sector names -> the labels I use in rules.toml.
YAHOO_SECTOR = {"Healthcare": "Health Care", "Consumer Defensive": "Consumer Staples",
                "Consumer Cyclical": "Consumer Discretionary", "Basic Materials": "Materials",
                "Financial Services": "Financials"}
ASK_CHOICES = {"not checked": "unknown", "yes": True, "no": False}


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


def analyse(ticker, form):
    """Gates, fit, score + rank, valuation and EPS revisions for one ticker,
    using the same functions as the weekly scanner."""
    rules = load_rules()
    today = pd.Timestamp.today().normalize()
    ctx = get_context(f"{today:%Y-%m-%d}")
    d = get_data(ticker)                                         # a fresh copy each time
    c = {"ticker": ticker, "name": d["name"] or ticker, "role": form["role"], "sector": form["sector"],
         "type": "etf" if d["quote_type"] == "ETF" else "stock", "ask_ok": ASK_CHOICES[form["ask"]]}

    fx = fx_to_dkk([d["currency"]], ctx["days"])
    gates = run_gates(c, d, rules, fx, ctx["holdings_weekly"], today)
    result = verdict(gates)

    weakest = (latest_scan().get("roles", {}).get(form["role"], {}).get("weakest") or [None])[0]
    prices_dkk = ctx["holdings_daily"]
    if "daily_dkk" in d:
        prices_dkk = prices_dkk.join(d["daily_dkk"].rename(ticker), how="outer").ffill()
    # Shown even if a gate fails, so I can see what it WOULD do to my portfolio.
    fit = fit_check(c, d, rules, ctx["values"], ctx["cash"], prices_dkk, today, swap_out=weakest)

    raw = raw_factors(ticker, d, d.get("daily_dkk"), ctx["holdings_daily"][ctx["core"]],
                      portfolio_weekly(ctx), today)
    score, star, rank, n = rank_with_latest(ticker, form["role"], raw, rules)
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
                f"is {bar} my {danish(minimum, 2)} bar (#{rank} of {n} {form['role']} names)")
    if gates.get("ASK", (None,))[0] == INFO and not failed:
        line = (line[0], line[1] + " · ASK not checked yet")
    return {"c": c, "d": d, "gates": gates, "result": result, "fit": fit, "weakest": weakest,
            "score": score, "star": star, "rank": rank, "n": n, "raw": raw,
            "valuation": valuation(ticker), "eps": eps_line.strip().lstrip("- ").strip(),
            "line": line}


# --- The page -------------------------------------------------

st.set_page_config(page_title="Ticker lookup", page_icon="🔎", layout="centered")
st.title("🔎 Ticker lookup")
st.caption("Checks one ticker against my rules - the same gates, fit check and scoring as the weekly "
           "scanner. Information only, never a buy instruction.")

ticker = st.text_input("Yahoo ticker", value=st.query_params.get("ticker", ""),
                       placeholder="e.g. SAF.PA, SAP.DE, EXPN.L").strip().upper()
if not ticker:
    st.stop()
if st.query_params.get("ticker") != ticker:
    st.query_params["ticker"] = ticker                           # keep the address in sync

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
st.caption(f"Yahoo sector / industry: {info['sector'] or '-'} / {info['industry'] or '-'}")

rules = load_rules()
listed = already_listed(rules, ticker)
if listed:
    st.info(f"{ticker} is already in: {', '.join(listed)}.")

# 2. The checklist.
st.subheader("Checklist")
role = st.radio("Role (required)", ["growth", "diversifier"], index=None, horizontal=True)
sectors = sorted({c["sector"] for c in rules["scanner"]["universe"]}
                 | set(rules["scanner"].get("holding_sectors", {}).values()))
guess = YAHOO_SECTOR.get(info["sector"], info["sector"] or "")
if "semiconductor" in (info["industry"] or "").lower():
    guess = "Semiconductors"
options = sectors if guess in sectors or not guess else sorted(sectors + [guess])
sector = st.selectbox("Sector (my label)", options, index=options.index(guess) if guess in options else 0)
ask = st.radio("ASK-eligible on Nordnet?", list(ASK_CHOICES), horizontal=True)
thesis = st.text_area("Thesis - why would I own it?")
kill_rule = st.text_area("Kill rule - what would make me sell?")
form = {"role": role, "sector": sector, "ask": ask, "thesis": thesis.strip(), "kill_rule": kill_rule.strip()}

if st.button("Check fit", type="primary", disabled=role is None, help="Choose a role first"):
    st.session_state["result"] = analyse(ticker, form)
    st.session_state["result_key"] = (ticker, role, sector, ask)

result = st.session_state.get("result")
if not result or st.session_state.get("result_key") != (ticker, role, sector, ask):
    st.stop()                                                    # nothing (current) to show yet

# 3. The analysis.
st.subheader("Result")
sym, text = result["line"]
(st.success if sym == PASS else st.error if sym == FAIL else st.info)(f"{sym} {text}")

st.markdown("**Gates**")
st.markdown("\n".join(f"- {result['gates'].get(g, (SKIP, 'not checked'))[0]} **{g}** - "
                      f"{result['gates'].get(g, (SKIP, 'not checked'))[1]}" for g in GATE_ORDER))

fsym, freason, fdetails = result["fit"]
st.markdown(f"**Fit check** {fsym} {freason}")
st.caption(f"If swapped for my weakest {role} satellite ({result['weakest'] or 'none'}): {fdetails}")

r = result
rank = f"#{r['rank']} of {r['n']} {role} names" if r["rank"] else "not ranked"
st.markdown(f"**Score** {score_text(r['score'], r['star'], long=True)} · {rank} "
            f"(against the last scanner run)")
st.caption("Raw factors: " + " · ".join(f"{k} {danish(v, 1) if isinstance(v, (int, float)) else 'n/a'}"
                                        for k, v in r["raw"].items() if k != "growth_capped"))
st.markdown(f"**Valuation** {r['valuation']}")
st.markdown(f"**EPS revisions** {r['eps']}")

# 4. Save it (each backs up rules.toml, then adds only its own lines).
st.subheader("Save")
today = pd.Timestamp.today().normalize()
entry = {"ticker": ticker, "name": r["c"]["name"], "type": r["c"]["type"], "sector": sector, "role": role,
         "ask_ok": ASK_CHOICES[ask], "thesis": form["thesis"], "kill_rule": form["kill_rule"], "added": today}
passes_gates = not any(sym == FAIL for sym, _ in r["gates"].values())
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
    st.caption("'Add to scanner group' unlocks when it passes the gates, ASK = yes, "
               "and the thesis and kill rule are filled in.")
