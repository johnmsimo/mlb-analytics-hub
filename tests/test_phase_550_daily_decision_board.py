from pathlib import Path

import pytest

from product_hub import (
    DAILY_DECISION_BOARD_VERSION,
    product_hub_bp,
)


ROOT = Path(__file__).resolve().parents[1]


def test_daily_decision_board_contract_is_fail_closed():
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(product_hub_bp)
    with app.test_client() as client:
        payload = client.get("/api/product/journey").get_json()

    board = payload["dailyDecisionBoard"]
    assert board["version"] == DAILY_DECISION_BOARD_VERSION == "5.5"
    assert board["sourceEndpoint"] == "/api/edges/today?minEdge=0.03"
    assert board["states"] == [
        "loading",
        "computing",
        "verified_plays",
        "no_bet",
        "unavailable",
    ]
    assert board["requiredEvidenceReceiptVersion"] == "4.69"
    assert board["maximumCards"] == 8
    assert board["requiresActionable"] is True
    assert board["requiresFreshSportsbookQuote"] is True
    assert board["rawRejectedRowsIncluded"] is False
    assert board["noBetIsValidDecision"] is True
    assert board["serverMutation"] is False
    assert board["failClosed"] is True


def test_daily_board_is_the_primary_workspace_surface():
    html = (ROOT / "product_hub.html").read_text(encoding="utf-8")
    for marker in (
        'id="dailyDecisionBoard"',
        'id="decisionBoardStatus"',
        'id="decisionBoardList"',
        'id="boardWatchlistCount"',
        'id="admissionReasonList"',
        'id="decisionBoardRefresh"',
        "What matters today",
        "No bet",
    ):
        assert marker.lower() in html.lower()

    assert html.index('id="dailyDecisionBoard"') < html.index('class="journey"')


def test_daily_board_uses_only_canonical_actionable_receipted_rows():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")

    assert "function renderDailyDecisionBoard()" in source
    assert "function decisionBoardCardHtml(row)" in source
    assert "dailyDecisionRows()" in source
    assert "rows.filter(isActionable)" in source
    assert "receipt.contractVersion === RECOMMENDATION_EVIDENCE_VERSION" in source
    assert "state.edgePayload = payload" in source
    assert "candidateIntegrityAudit" in source
    assert "actionabilityAudit" in source
    assert "raw rejected" not in source.lower()
    assert "No candidate cleared identity, price, freshness, calibration, edge, and receipt gates." in source


def test_daily_board_surfaces_sanitized_analysis_without_bet_actions():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")

    assert "payload.watchlistEdges" in source
    assert "function watchlistBoardCardHtml(row)" in source
    assert "WATCHLIST · ANALYSIS ONLY" in source
    assert "Tracking and parlays stay disabled" in source
    assert "row.actionable === false" in source
    assert "row.promotionStatus === 'research_only'" in source
    assert "audit.primaryRejectionReasons || audit.rejectionReasons" in source


def test_daily_board_preserves_no_bet_and_unavailable_states():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")

    assert "rows.length ? 'verified_plays' : 'no_bet'" in source
    assert "['failed', 'unavailable'].indexOf(sourceState) >= 0 ? 'unavailable'" in source
    assert "No recommendation is shown while the durable scan is incomplete." in source
    assert "The board is failing closed." in source
    assert "Missing evidence never becomes a pick." in (
        ROOT / "product_hub.html"
    ).read_text(encoding="utf-8")


def test_daily_board_preserves_phone_width_and_touch_contract():
    css = (ROOT / "static" / "product-hub.css").read_text(encoding="utf-8")

    assert "@media(max-width:480px)" in css
    assert ".decision-board-list{grid-template-columns:1fr" in css
    assert ".decision-card-actions button,.decision-card-actions a,.decision-board-foot button,.decision-board-foot a{min-height:44px}" in css


def test_production_gate_requires_phase_55_board_contract():
    gate = (ROOT / "scripts" / "production_contract_gate.py").read_text(
        encoding="utf-8"
    )

    assert 'board.get("version") == "5.5"' in gate
    assert 'board.get("failClosed") is True' in gate
    assert 'board.get("rawRejectedRowsIncluded") is False' in gate
    assert 'board.get("maximumCards") == 8' in gate


def test_pr_baseline_accepts_previous_journey_but_post_deploy_requires_phase_55():
    from scripts.production_contract_gate import (
        ContractError,
        _validate_journey,
        _validate_journey_baseline,
    )

    previous_release = {
        "success": True,
        "version": "4.64",
        "stages": [
            {"key": "discover"},
            {"key": "validate"},
            {"key": "track"},
            {"key": "learn"},
        ],
        "alerts": {
            "failClosed": True,
            "serverPersistence": False,
            "freshness": {"maximumOddsAgeSeconds": 900},
        },
    }

    _validate_journey_baseline(previous_release)
    with pytest.raises(ContractError, match="daily decision board"):
        _validate_journey(previous_release)
