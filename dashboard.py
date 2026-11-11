# ============================================================
#  dashboard.py  -  one HTML page with everything, opened in my browser
#
#  It does NOT calculate anything. It only READS what the other
#  scripts have already saved in output\ in the data folder:
#    shadow_summary.json, shadow_comparison.csv  <- shadow_portfolio.py
#    regime_history_GSPC.csv / _SOX.csv          <- regime.py
#    scanner/latest.json                         <- scanner.py
#    weekly/YYYY-MM-DD.md (newest)               <- weekly_check.py
#    risk_summary.json                           <- risk.py
#  and writes output\dashboard.html: a single file that also
#  works offline (the plotly chart library is copied into it).
#
#  py dashboard.py            build and open in the browser
#  py dashboard.py --no-open  build only
# ============================================================

import html
import json
import re
import sys
import webbrowser
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
from plotly.offline import get_plotlyjs
from plotly.subplots import make_subplots

from portfolio import danish
from positions import OUTPUT_FOLDER
from settings import DEMO                    # True = running on the synthetic demo_data
from scan_core import EXPLAIN, LOOKUP_URL as LOOKUP_PAGE, lookup_link   # shared texts + lookup links
from data_cache import price_status          # which closes the prices are, offline or not

DASHBOARD = OUTPUT_FOLDER / "dashboard.html"
STATUS = {"✅": "ok", "⚠️": "warn", "ℹ️": "info", "❌": "bad"}
COLOUR = {"me": "#1565c0", "cspx": "#ef6c00", "deposits": "#9e9e9e",
          "calm": "#2e7d32", "normal": "#1e88e5", "stressed": "#c62828",
          "money": "#9e9e9e", "risk": "#c62828"}


# --- Reading the other scripts' output ------------------------

def read_json(name):
    path = OUTPUT_FOLDER / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def read_csv(name):
    path = OUTPUT_FOLDER / name
    if not path.exists():
        return None
    return pd.read_csv(path, sep=";", decimal=",", encoding="utf-8-sig", parse_dates=["date"])


def newest_weekly_report():
    files = sorted((OUTPUT_FOLDER / "weekly").glob("????-??-??.md"))   # names sort by date
    return files[-1] if files else None


def parse_report(text):
    """Split the weekly report into its '## ' sections."""
    title, intro, sections, current = "", [], {}, None
    for line in text.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
        elif line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current is None or line.startswith("**"):
            if line.strip():
                intro.append(line)         # header lines and the "N items need attention" total
        else:
            sections[current].append(line)
    return title, intro, sections


def section(sections, starts_with):
    """(heading, lines) of the first section whose heading starts with this."""
    for name, lines in sections.items():
        if name.startswith(starts_with):
            return name, lines
    return None, []


# --- Small HTML helpers ---------------------------------------

def inline(text):
    """Escape text for HTML, keeping **bold**."""
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html.escape(text))


def items(lines):
    """Report lines -> a coloured list: ✅ green, ⚠️ yellow, ℹ️ blue."""
    out = []
    for line in lines:
        if not line.strip():
            continue
        nested = len(line) - len(line.lstrip()) >= 2
        text = line.strip()
        text = text[2:] if text.startswith("- ") else text
        status, symbol = "plain", ""
        for sym, cls in STATUS.items():
            if text.startswith(sym):
                status, symbol, text = cls, sym, text[len(sym):].strip()
                break
        if status == "plain" and not nested and not line.lstrip().startswith("- "):
            out.append(f'<p class="note">{inline(text)}</p>')
            continue
        out.append(f'<li class="item {status}{" sub" if nested else ""}">'
                   f'<span class="sym">{symbol}</span><span>{inline(text)}</span></li>')
    return '<ul class="items">' + "".join(out) + "</ul>"


def card(title, body, wide=False, subtitle=""):
    sub = f'<div class="subtitle">{html.escape(subtitle)}</div>' if subtitle else ""
    return (f'<section class="card{" wide" if wide else ""}"><h2>{html.escape(title)}</h2>'
            f'{sub}{body}</section>')


def missing(what, script):
    return f'<p class="note">No {what} yet - run <code>py {script}</code>.</p>'


def plot(fig):
    fig.update_layout(template="plotly_white", separators=",.",      # Danish: 1.234,5
                      margin=dict(l=10, r=10, t=30, b=10),
                      font=dict(family="system-ui, Segoe UI, sans-serif", size=12),
                      legend=dict(orientation="h", y=1.08, x=0))
    return pio.to_html(fig, include_plotlyjs=False, full_html=False,
                       config={"displaylogo": False, "responsive": True})


# --- Charts ---------------------------------------------------

def chart_account(df):
    fig = go.Figure()
    for col, name, colour, dash in [("value", "My account", COLOUR["me"], None),
                                    ("shadow_value", "CSPX shadow", COLOUR["cspx"], None),
                                    ("deposits", "Net deposits", COLOUR["deposits"], "dash")]:
        fig.add_trace(go.Scatter(x=df["date"], y=df[col], name=name, mode="lines",
                                 line=dict(color=colour, width=2 if dash is None else 1.5, dash=dash),
                                 hovertemplate="%{y:,.0f} DKK<extra>" + name + "</extra>"))
    fig.update_layout(height=380, hovermode="x unified", yaxis_title="DKK")
    return plot(fig)


def chart_risk(risk):
    rows = sorted(risk["per_holding"], key=lambda r: r["money_pct"])
    names = [r["ticker"] for r in rows]
    fig = go.Figure([
        go.Bar(y=names, x=[r["money_pct"] for r in rows], name="Share of money", orientation="h",
               marker_color=COLOUR["money"], hovertemplate="%{x:.1f}%<extra>money</extra>"),
        go.Bar(y=names, x=[r["risk_pct"] for r in rows], name="Share of risk (swings)", orientation="h",
               marker_color=COLOUR["risk"], hovertemplate="%{x:.1f}%<extra>risk</extra>"),
    ])
    fig.update_layout(barmode="group", height=320, xaxis_ticksuffix="%")
    return plot(fig)


def chart_regime(histories):
    fig = make_subplots(rows=len(histories), cols=1, shared_xaxes=True, vertical_spacing=0.08,
                        subplot_titles=[name for name, _ in histories])
    for i, (name, df) in enumerate(histories, start=1):
        for regime in ["calm", "normal", "stressed"]:
            fig.add_trace(go.Scatter(x=df["date"], y=df[regime] * 100, name=regime, stackgroup=f"s{i}",
                                     mode="none", fillcolor=COLOUR[regime], legendgroup=regime,
                                     showlegend=(i == 1),
                                     hovertemplate="%{y:.0f}%<extra>" + regime + "</extra>"),
                          row=i, col=1)
        fig.update_yaxes(range=[0, 100], ticksuffix="%", row=i, col=1)
    last = max(df["date"].max() for _, df in histories)
    fig.update_xaxes(range=[last - pd.DateOffset(years=3), last])     # start zoomed on 3 years
    fig.update_layout(height=430, hovermode="x unified",
                      xaxis=dict(rangeselector=dict(buttons=[
                          dict(count=1, label="1y", step="year", stepmode="backward"),
                          dict(count=3, label="3y", step="year", stepmode="backward"),
                          dict(step="all", label="all")])))
    return plot(fig)


# --- Page sections --------------------------------------------

def kpis(shadow, risk, warnings):
    tiles = []
    if risk:
        tiles.append(("Account value", f"{danish(risk['total'], 0)} DKK",
                      f"holdings {danish(risk['holdings'], 0)} + cash {danish(risk['cash'], 0)} · prices {risk['date']}",
                      "info"))
    if shadow:
        for label, mine, core in [("YTD", shadow["my_ytd"], shadow["shadow_ytd"]),
                                  ("Since start", shadow["my_since_start"], shadow["shadow_since_start"])]:
            gap = mine - core
            tiles.append((f"{label} (time-weighted)", f"{'+' if mine >= 0 else ''}{danish(mine, 1)}%",
                          f"CSPX {'+' if core >= 0 else ''}{danish(core, 1)}% · "
                          f"{'+' if gap >= 0 else ''}{danish(gap, 1)} pts", "ok" if gap >= 0 else "warn"))
        tiles.append(("vs. CSPX shadow", f"{'+' if shadow['difference_dkk'] >= 0 else ''}"
                                         f"{danish(shadow['difference_dkk'], 0)} DKK",
                      f"same deposits in CSPX · Nordnet data {shadow['date']}",
                      "ok" if shadow["difference_dkk"] >= 0 else "warn"))
    if warnings is not None:
        tiles.append(("Need attention", f"{warnings} ⚠️", "items in the latest weekly report",
                      "warn" if warnings else "ok"))
    return '<div class="kpis">' + "".join(
        f'<div class="kpi {cls}"><div class="label">{html.escape(l)}</div>'
        f'<div class="value">{html.escape(v)}</div><div class="small">{html.escape(s)}</div></div>'
        for l, v, s, cls in tiles) + "</div>"


def risk_body(risk, sections):
    if not risk:
        return missing("risk results", "risk.py")
    fx = risk["currency"]
    main = {k: fx.get(k, 0.0) for k in ["USD", "EUR", "DKK"]}
    main["other"] = sum(fx.values()) - sum(main.values())
    bars = "".join(f'<div class="bar-row"><span>{k}</span><div class="bar"><div style="width:{v:.1f}%"></div>'
                   f'</div><span>{danish(v, 0)}%</span></div>' for k, v in main.items())
    stress = "".join(f"<tr><td>{html.escape(s['name'])}</td><td class='num'>{danish(s['loss_dkk'], 0)} DKK</td>"
                     f"<td class='num'>{danish(s['loss_pct'], 1)}%</td></tr>" for s in risk["stress"])
    dd = risk["drawdown"]
    _, regime_lines = section(sections, "Market regime")
    risk_heading, risk_lines = section(sections, "Risk")
    semis = [x for x in risk_lines if "Semis" in x]
    return (f'<h3>Currency (underlying, incl. cash)</h3>{bars}'
            + items(semis)
            + f'<p class="note">Typical yearly swing: about ±{danish(risk["yearly_swing_dkk"], 0)} DKK '
              f'({danish(risk["yearly_swing_pct"], 1)}%).</p>'
            + f'<h3>Stress tests (instant shock, estimated)</h3><table>{stress}</table>'
            + f'<h3>Drawdown (time-weighted, Nordnet data to {dd["data_until"]})</h3>'
            + items([f"- ℹ️ Today {danish(dd['now_pct'], 1)}% below the peak of {dd['now_peak_day']}",
                     f"- ℹ️ Worst so far {danish(dd['worst_pct'], 1)}% "
                     f"({dd['worst_peak_day']} → {dd['worst_day']})"])
            + '<h3>Market regime</h3>' + items(regime_lines))


def scanner_body(scan):
    if not scan:
        return missing("scanner results", "scanner.py")
    parts = []
    for role, r in scan["roles"].items():
        top = "".join(f"<li class='item info'><span class='sym'>{i}.</span>"
                      f"<span>{html.escape(tk)} <strong>{html.escape(score)}</strong>"
                      f"{explain_a(lookup_link(tk, role=role))}</span></li>"
                      for i, (tk, score) in enumerate(r["top"], start=1)) or "<li>none eligible</li>"
        weak = (f"{html.escape(r['weakest'][0])} <strong>{html.escape(r['weakest'][1])}</strong>"
                if r["weakest"] else "n/a")
        unscored = ", ".join(r.get("not_scored", [])) or "none"
        parts.append(f'<div class="role"><h3>{role.capitalize()}</h3>'
                     f'<div class="small">Top 3 eligible</div><ul class="items">{top}</ul>'
                     f'<p class="note">My weakest: {weak}</p>'
                     f'<p class="note muted">Not scored (ETF / missing data): {html.escape(unscored)}</p></div>')
    verdicts = items([f"- ℹ️ {v}" for v in scan["verdicts"]])
    # A flagged swap links to the head-to-head comparison (lookup, replace mode).
    for sw in scan.get("swaps", []):
        verdicts += (f'<p class="note">{html.escape(sw["in"])} in, {html.escape(sw["out"])} out:'
                     + explain_a(lookup_link(sw["in"], mode="replace", out=sw["out"])) + "</p>")
    return (f'<div class="roles">{"".join(parts)}</div><h3>One in, one out '
            f'(swaps left this quarter: {scan["swaps_left"]})</h3>{verdicts}'
            f'<p class="note muted">{html.escape(EXPLAIN["legend"])}. Information only - never a buy instruction.</p>')


LOOKUP_URL = LOOKUP_PAGE + "?ticker="     # the lookup page (lookup.py, via 'Start lookup.bat')


def explain_a(url):
    """A small 'explain' link to the full explained fit check on the lookup page."""
    return (f" <a class='small' href='{html.escape(url)}' target='_blank' "
            f"title='{html.escape(EXPLAIN['fit_short_help'])}'>{EXPLAIN['fit_link']}</a>")


def watchlist_body(scan):
    """The watchlist: read from scanner/latest.json (made by scanner.py)."""
    names = (scan or {}).get("watchlist", [])
    if not names:
        return ('<p class="note muted">Empty. Look up a ticker with the search box and press '
                '"Add to watchlist" to follow it here (gates and fit every week, not ranked).</p>')
    rows = "".join(
        f"<tr><td><a href='{LOOKUP_URL}{html.escape(w['ticker'])}' target='_blank'>{html.escape(w['ticker'])}</a></td>"
        f"<td>{html.escape(w['role'])}</td><td>{html.escape(w['gates'])}</td><td>{html.escape(w['verdict'])}</td>"
        f"<td>{html.escape(w['fit'])} {html.escape(w['fit_reason'])}"
        f"{explain_a(w.get('explain') or lookup_link(w['ticker'], role=w['role']))}</td>"
        f"<td class='small'>{html.escape(w.get('thesis', ''))}</td></tr>" for w in names)
    return ("<table><tr><td><strong>Ticker</strong></td><td><strong>Role</strong></td><td><strong>Gates</strong></td>"
            "<td><strong>Verdict</strong></td>"
            f"<td title='{html.escape(EXPLAIN['fit_short_help'])}'><strong>Fit</strong></td>"
            "<td><strong>Thesis</strong></td></tr>"
            f"{rows}</table><p class='note muted'>Gates and fit only - watchlist names are never ranked.</p>")


BADGE = {"SELL": "sell", "TRIM": "trim", "REVIEW": "review", "ADD": "add", "ADD blocked": "hold",
         "HOLD": "hold", "BUY CSPX": "buy", "SLOT OPEN": "buy"}


def action_panel(acts):
    """The action panel: read from output/actions.json (made by weekly_check.py)."""
    if not acts:
        return card("Actions", missing("actions", "weekly_check.py"), wide=True)
    p = acts["portfolio"]
    rows = [f'<div class="act-row"><span class="badge {BADGE.get(p["action"], "hold")}">{html.escape(p["action"])}</span>'
            f'<div><strong>Portfolio · {html.escape(p["text"])}</strong><div class="small">{html.escape(p["reason"])}'
            + (f'<br>{html.escape(p["details"])}' if p.get("details") else "") + "</div></div></div>"]
    order = {"SELL": 0, "TRIM": 1, "REVIEW": 2, "ADD": 3, "ADD blocked": 4, "HOLD": 5}
    for x in sorted(acts["holdings"], key=lambda x: order.get(x["action"], 9)):
        label = x["ticker"] if x["name"] == x["ticker"] else f"{x['ticker']} · {x['name']}"
        proceeds = f'<div class="small proceeds">{html.escape("proceeds " + x["proceeds"])}</div>' if x.get("proceeds") else ""
        rows.append(f'<div class="act-row"><span class="badge {BADGE.get(x["action"], "hold")}">'
                    f'{html.escape(x["action"])}</span><div><strong>{html.escape(label)}</strong>'
                    f' - {html.escape(x["reason"])}{proceeds}</div></div>')
    note = f'<p class="note muted">{html.escape(acts["note"])}</p>'
    return card(f"Actions ({acts['date']})", "".join(rows) + note, wide=True)


# --- Build the page -------------------------------------------

def build():
    shadow = read_json("shadow_summary.json")
    risk = read_json("risk_summary.json")
    scan = read_json("scanner/latest.json")
    acts = read_json("actions.json")
    account = read_csv("shadow_comparison.csv")
    regimes = [(name, df) for name, df in [("S&P 500", read_csv("regime_history_GSPC.csv")),
                                           ("Semis (SOX)", read_csv("regime_history_SOX.csv"))]
               if df is not None]

    report_path = newest_weekly_report()
    report_text = report_path.read_text(encoding="utf-8") if report_path else ""
    title, intro, sections = parse_report(report_text)
    m = re.search(r"\*\*(\d+) item\(s\) need attention", report_text)
    warnings = int(m.group(1)) if m else (0 if "All clear" in report_text else None)

    # Where each piece of data comes from, and how old it is.
    prices = re.search(r"Prices: close (\d{4}-\d{2}-\d{2})", report_text)
    sources = [("weekly report", report_path.stem if report_path else None),
               ("prices close", prices.group(1) if prices else None),
               ("Nordnet history to", shadow["date"] if shadow else None),
               ("scanner", scan["date"] if scan else None),
               ("risk", risk["date"] if risk else None),
               ("regime history to", f"{max(df['date'].max() for _, df in regimes):%Y-%m-%d}" if regimes else None)]
    data_line = " · ".join(f"{name} <strong>{d or 'missing'}</strong>" for name, d in sources)
    if DEMO:
        data_line = '<span class="demo">DEMO DATA - synthetic portfolio</span> ' + data_line
    # Which closing prices the numbers use (and a warning if Yahoo was unreachable).
    prices_label, offline = price_status()
    if prices_label:
        data_line += (f' · <span class="offline">{html.escape(prices_label)}</span>' if offline
                      else f" · {html.escape(prices_label)}")

    holdings_body = "".join(f"<h3>{html.escape(h)}</h3>{items(lines)}" for h, lines in
                            [section(sections, "Limits"), section(sections, "Returns"),
                             section(sections, "Price levels")] if h) or missing("weekly report", "weekly_check.py")
    story_heading, story_lines = section(sections, "Story checks")
    ask_heading, ask_lines = section(sections, "ASK")
    cal_heading, cal_lines = section(sections, "Calendar")

    full_report = (f"<h3>{html.escape(title)}</h3>" + items(intro)
                   + "".join(f"<h3>{html.escape(name)}</h3>{items(lines)}" for name, lines in sections.items())
                   if report_text else missing("weekly report", "weekly_check.py"))

    body = (
        action_panel(acts)
        + kpis(shadow, risk, warnings)
        + '<div class="grid">'
        + card("My account vs. CSPX shadow", chart_account(account) if account is not None
               else missing("shadow data", "shadow_portfolio.py"), wide=True,
               subtitle="Hover for values · drag to zoom · double-click to reset · click a legend item to hide it")
        + card("Holdings: weights, returns, price levels", holdings_body)
        + card("Story checks, valuation, EPS revisions",
               items(story_lines) if story_heading else missing("weekly report", "weekly_check.py"))
        + card("Risk", risk_body(risk, sections))
        + card("Share of risk vs. share of money", chart_risk(risk) if risk else missing("risk results", "risk.py"),
               subtitle="A holding whose red bar is longer than its grey bar adds more swings than its size")
        + card("Market regime: probability per day (walk-forward)",
               chart_regime(regimes) if regimes else missing("regime history", "regime.py"), wide=True,
               subtitle="'Stressed' has predicted bumpier months, not worse returns")
        + card("ASK tax and calendar",
               (f"<h3>{html.escape(ask_heading or 'ASK')}</h3>{items(ask_lines)}"
                f"<h3>{html.escape(cal_heading or 'Calendar')}</h3>{items(cal_lines)}")
               if report_text else missing("weekly report", "weekly_check.py"))
        + card("Scanner", scanner_body(scan))
        + card("Watchlist", watchlist_body(scan), wide=True)
        + card("Full weekly report", full_report, wide=True)
        + "</div>"
    )

    page = PAGE.format(plotly=get_plotlyjs(), data_line=data_line, body=body, lookup=LOOKUP_URL,
                       built=datetime.now().strftime("%Y-%m-%d %H:%M"))
    DASHBOARD.write_text(page, encoding="utf-8")
    return DASHBOARD


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Portfolio dashboard</title>
<script>{plotly}</script>
<style>
:root {{ --bg:#f5f6f8; --card:#fff; --text:#1f2933; --muted:#6b7280; --line:#e5e7eb;
        --ok:#2e7d32; --ok-bg:#e8f5e9; --warn:#b26a00; --warn-bg:#fff8e1;
        --info:#1565c0; --info-bg:#e3f2fd; --bad:#c62828; --bad-bg:#ffebee; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text);
       font:14px/1.45 system-ui, "Segoe UI", sans-serif; }}
header {{ padding:18px 24px 6px; max-width:1360px; margin:0 auto; }}
h1 {{ margin:0 0 4px; font-size:22px; }}
.data {{ color:var(--muted); font-size:12.5px; }}
main {{ max-width:1360px; margin:0 auto; padding:10px 24px 40px; }}
.kpis {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(190px, 1fr)); gap:12px; margin:8px 0 16px; }}
.kpi {{ background:var(--card); border-radius:10px; padding:12px 14px; border-left:5px solid var(--info); }}
.kpi.ok {{ border-left-color:var(--ok); }} .kpi.warn {{ border-left-color:#f9a825; }}
.kpi .label {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.03em; }}
.kpi .value {{ font-size:22px; font-weight:650; margin:2px 0; }}
.kpi .small, .small {{ color:var(--muted); font-size:12px; }}
.grid {{ display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:16px; }}
.card {{ background:var(--card); border-radius:10px; padding:14px 16px; box-shadow:0 1px 2px rgba(0,0,0,.06); min-width:0; }}
.card.wide {{ grid-column:1 / -1; }}
.card h2 {{ margin:0 0 4px; font-size:16px; }}
.card h3 {{ margin:14px 0 6px; font-size:13px; color:var(--muted); text-transform:uppercase; letter-spacing:.03em; }}
.subtitle {{ color:var(--muted); font-size:12px; margin-bottom:6px; }}
ul.items {{ list-style:none; margin:0; padding:0; }}
.item {{ display:flex; gap:8px; align-items:flex-start; padding:5px 8px; margin:3px 0;
        border-radius:6px; border-left:4px solid var(--line); }}
.item .sym {{ flex:0 0 auto; min-width:1.4em; }}
.item.ok {{ background:var(--ok-bg); border-left-color:var(--ok); }}
.item.warn {{ background:var(--warn-bg); border-left-color:#f9a825; }}
.item.info {{ background:var(--info-bg); border-left-color:var(--info); }}
.item.bad {{ background:var(--bad-bg); border-left-color:var(--bad); }}
.item.sub {{ margin-left:26px; font-size:13px; }}
.item.plain.sub {{ background:#fafafa; }}
.note {{ margin:6px 0; }} .muted {{ color:var(--muted); }}
table {{ border-collapse:collapse; width:100%; }}
td {{ padding:4px 6px; border-bottom:1px solid var(--line); }} td.num {{ text-align:right; white-space:nowrap; }}
.bar-row {{ display:grid; grid-template-columns:52px 1fr 44px; gap:8px; align-items:center; margin:3px 0; }}
.bar {{ background:var(--line); height:10px; border-radius:5px; overflow:hidden; }}
.bar div {{ background:var(--info); height:100%; }}
.roles {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }}
code {{ background:#eef0f3; padding:1px 4px; border-radius:4px; }}
.act-row {{ display:flex; gap:12px; align-items:flex-start; padding:7px 0; border-bottom:1px solid var(--line); }}
.act-row:last-of-type {{ border-bottom:none; }}
.badge {{ flex:0 0 96px; text-align:center; font-weight:700; font-size:12px; letter-spacing:.03em;
         padding:4px 6px; border-radius:6px; color:#fff; }}
.badge.sell {{ background:#c62828; }} .badge.trim {{ background:#ef6c00; }}
.badge.review {{ background:#f9a825; color:#3e2c00; }} .badge.add {{ background:#2e7d32; }}
.badge.hold {{ background:#9e9e9e; }} .badge.buy {{ background:#1565c0; }}
.proceeds {{ margin-top:2px; }}
main > .card.wide:first-child {{ margin-bottom:12px; }}
header .top {{ display:flex; justify-content:space-between; align-items:flex-start; gap:16px; flex-wrap:wrap; }}
.search {{ display:flex; flex-wrap:wrap; gap:6px; align-items:center; max-width:360px; }}
.search input {{ flex:1 1 200px; padding:7px 10px; border:1px solid #cfd4dc; border-radius:6px; font:inherit; }}
.search button {{ padding:7px 14px; border:0; border-radius:6px; background:var(--info); color:#fff; font:inherit; cursor:pointer; }}
.search .small {{ flex-basis:100%; }}
.demo {{ background:#f9a825; color:#3e2c00; font-weight:700; padding:1px 6px; border-radius:4px; margin-right:4px; }}
.offline {{ color:#b45309; font-weight:600; }}
@media (max-width: 900px) {{ .grid, .roles {{ grid-template-columns:1fr; }} }}
</style></head>
<body>
<header><div class="top">
<div><h1>Portfolio dashboard</h1>
<div class="data">Data from: {data_line} · page built {built}</div></div>
<form class="search" onsubmit="var t=this.q.value.trim(); if (t) {{ window.open('{lookup}' + encodeURIComponent(t), '_blank'); }} return false;">
<input name="q" placeholder="Look up a ticker, e.g. SAF.PA" autocomplete="off"><button type="submit">Look up</button>
<div class="small">Double-click 'Start lookup' first</div></form>
</div></header>
<main>{body}</main>
</body></html>
"""


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    path = build()
    print(f"Dashboard saved to: {path}")
    if "--no-open" not in sys.argv:
        webbrowser.open(path.as_uri())          # opens the file in my default browser


if __name__ == "__main__":
    main()
