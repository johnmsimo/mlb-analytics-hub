"""Phase 4.64 product journey, freshness, and material alert contracts."""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, jsonify, make_response


PRODUCT_HUB_VERSION = "4.64"
_ROOT = Path(__file__).resolve().parent
_HUB_PATH = _ROOT / "product_hub.html"

product_hub_bp = Blueprint("product_hub", __name__)

PRODUCT_STAGES = (
    {
        "key": "discover",
        "label": "Discover",
        "description": "Find today's fully validated, priced opportunities.",
        "primaryHref": "/props",
        "secondaryHref": "/value-bets",
    },
    {
        "key": "validate",
        "label": "Validate",
        "description": "Inspect calibration, context, price, freshness, and evidence.",
        "primaryHref": "/edge-lab",
        "secondaryHref": "/cheatsheets",
    },
    {
        "key": "track",
        "label": "Track",
        "description": "Save decisions and measure results against the market.",
        "primaryHref": "/tracker",
        "secondaryHref": "/value-bets",
    },
    {
        "key": "learn",
        "label": "Learn",
        "description": "Use graded outcomes, calibration, and consistency to improve decisions.",
        "primaryHref": "/consistency",
        "secondaryHref": "/tracker",
    },
)


@product_hub_bp.get("/workspace")
def product_workspace():
    response = make_response(_HUB_PATH.read_text(encoding="utf-8"))
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    response.headers["Cache-Control"] = "no-store"
    return response


@product_hub_bp.get("/api/product/journey")
def product_journey():
    return jsonify(
        {
            "success": True,
            "version": PRODUCT_HUB_VERSION,
            "stages": PRODUCT_STAGES,
            "personalization": {
                "watchlistStorageKey": "mlb_watchlist",
                "marketStorageKey": "mlb_market_preferences",
                "alertThresholdStorageKey": "mlb_alert_edge_threshold",
                "alertLedgerStorageKey": "mlb_alert_ledger",
                "alertCandidateStateStorageKey": "mlb_alert_candidate_state",
                "alertDelivery": "in_app",
                "persistence": "device_private",
            },
            "alerts": {
                "lifecycle": ["new", "seen", "dismissed", "superseded"],
                "dedupeIdentity": ["canonicalCandidateId", "canonicalFingerprint"],
                "groupIdentity": "canonicalCandidateId",
                "freshness": {
                    "timestampField": "oddsUpdatedAt",
                    "ageField": "oddsAgeSeconds",
                    "maximumOddsAgeSeconds": 900,
                },
                "materialChange": {
                    "minimumEdgeDeltaPct": 1.0,
                    "minimumAmericanPriceDelta": 10,
                    "supersedePreviousSnapshot": True,
                },
                "requiresPreferredMarket": True,
                "requiresThresholdMatch": True,
                "serverPersistence": False,
                "failClosed": True,
            },
            "actionability": {
                "source": "/api/edges/today",
                "required": [
                    "actionable",
                    "player identity",
                    "market price",
                    "book",
                    "canonical market",
                    "canonical candidate identity",
                    "canonical fingerprint",
                    "positive edge",
                ],
                "failClosed": True,
            },
        }
    )

