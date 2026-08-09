"""Regression coverage for the Phase 4.58 390px mobile contract."""
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
MOBILE_CSS = (ROOT / "static" / "mobile.css").read_text(encoding="utf-8")


def test_phone_contract_is_scoped_to_mobile_width():
    assert "@media (max-width: 480px)" in MOBILE_CSS
    assert "Phase 4.58: 390px iPhone contract" in MOBILE_CSS
    assert "@media (min-width: 769px)" in MOBILE_CSS


def test_dense_surfaces_have_contained_touch_scrolling():
    for selector in (".tw", ".tbl-wrap", ".table-wrap", ".scroll-x"):
        assert selector in MOBILE_CSS
    assert "overscroll-behavior-inline: contain" in MOBILE_CSS
    assert "-webkit-overflow-scrolling: touch" in MOBILE_CSS


def test_requested_surfaces_have_mobile_css_and_page_specific_contracts():
    pages = {
        "tracker.html": ("builder-metrics", ".hero"),
        "value_bets.html": (".filters select", ".tbl-wrap"),
        "consistency.html": (".toolbar .tabs", ".table-wrap"),
        "pitcher_deepdive.html": (".cards-grid", ".sel-input"),
        "settings.html": (".top-grid", "#app-root > div"),
        "tools.html": (".calc-card", ".result-grid"),
    }
    for filename, markers in pages.items():
        html = (ROOT / filename).read_text(encoding="utf-8")
        assert re.search(r'<link[^>]+href="/static/mobile\.css"', html)
        for marker in markers:
            assert marker in MOBILE_CSS


def test_primary_controls_keep_touch_target_and_fit_the_viewport():
    assert "min-height: 44px" in MOBILE_CSS
    assert "max-width: 100%" in MOBILE_CSS
    assert "input, select, textarea, button" in MOBILE_CSS
