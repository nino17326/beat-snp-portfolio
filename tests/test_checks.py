# ============================================================
#  tests/test_checks.py  -  the rules.toml check (fast, no internet)
# ============================================================

from pathlib import Path

import checks

DEMO_RULES = (Path(__file__).resolve().parent.parent / "demo_data" / "rules.toml").read_text(encoding="utf-8")


def errors(text):
    return [p for p in checks.check_rules_text(text) if p["level"] == checks.ERROR]


def broken(old, new):
    assert old in DEMO_RULES, old
    return DEMO_RULES.replace(old, new, 1)


def test_demo_rules_are_valid():
    assert checks.check_rules_text(DEMO_RULES) == []


def test_true_in_quotes_is_caught_with_line_and_fix():
    found = errors(broken('role = "growth",      ask_ok = true },', 'role = "growth",      ask_ok = "true" },'))
    assert len(found) == 1
    assert found[0]["line"] and "ask_ok" in found[0]["text"] and "without quotes" in found[0]["fix"]


def test_date_in_quotes_is_caught():
    found = errors(broken("compare_from = 2026-03-02", 'compare_from = "2026-03-02"'))
    assert found and "dates have no quotes" in found[0]["fix"]


def test_missing_section_and_syntax_error():
    assert any("[ask] is missing" in p["text"] for p in errors(broken("[ask]", "[asks]")))
    syntax = errors(broken("max_correlation = 0.8 ", "max_correlation = 0.8.1 "))
    assert len(syntax) == 1 and syntax[0]["line"]


def test_duplicate_ticker_is_caught():
    dup = '    { ticker = "SIE.DE", name = "x", type = "stock", sector = "Industrials", role = "growth", ask_ok = true },\n'
    found = errors(broken('    { ticker = "REN.AS",', dup + '    { ticker = "REN.AS",'))
    assert any("listed twice" in p["text"] for p in found)
