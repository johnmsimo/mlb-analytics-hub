"""Phase 4.64 product journey, freshness, and material alert contracts."""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, jsonify, make_response


PRODUCT_HUB_VERSION = "4.64"
SAVED_PLAYER_DIGEST_VERSION = "4.70"
VERIFIED_DECISION_HANDOFF_VERSION = "4.71"
VERIFIED_DECISION_LEARNING_VERSION = "4.72"
VERIFIED_DECISION_MARKET_LEARNING_VERSION = "4.73"
VERIFIED_DECISION_MARKET_PREFERENCE_REVIEW_VERSION = "4.74"
MARKET_PREFERENCE_CHANGE_RECEIPT_VERSION = "4.75"
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
                "savedPlayerDigestVersion": SAVED_PLAYER_DIGEST_VERSION,
            },
            "savedPlayerDigest": {
                "version": SAVED_PLAYER_DIGEST_VERSION,
                "source": "/api/edges/today",
                "watchlistStorageKey": "mlb_watchlist",
                "states": [
                    "loading",
                    "verified_opportunity",
                    "no_verified_opportunity",
                    "unavailable",
                ],
                "requiresEvidenceReceiptVersion": "4.69",
                "oneTapSignalControls": True,
                "serverPersistence": False,
                "failClosed": True,
            },
            "verifiedDecisionHandoff": {
                "version": VERIFIED_DECISION_HANDOFF_VERSION,
                "source": "saved_player_verified_opportunity",
                "requiresEvidenceReceiptVersion": "4.69",
                "storageKey": "mlb_verified_decision_draft_v471",
                "destination": "/tracker",
                "states": ["prepared", "reviewing", "discarded", "expired"],
                "expiresWithQuote": True,
                "serverMutationOnPrepare": False,
                "requiresExplicitSave": True,
                "saveEndpoint": "/api/tracker/pick",
                "saveRequiresAdminAuth": True,
                "canonicalRevalidationOnSave": True,
                "failClosed": True,
            },
            "verifiedDecisionLearning": {
                "version": VERIFIED_DECISION_LEARNING_VERSION,
                "sourceEndpoint": "/api/tracker/performance?window=30",
                "trackedSource": "my_hub_verified_decision_draft",
                "states": [
                    "no_verified_decisions",
                    "awaiting_outcomes",
                    "learning",
                    "sample_ready",
                    "unavailable",
                ],
                "minimumGradedSample": 10,
                "aggregateOnly": True,
                "rowsIncluded": False,
                "metricsAreDescriptive": True,
                "failClosed": True,
            },
            "verifiedDecisionMarketLearning": {
                "version": VERIFIED_DECISION_MARKET_LEARNING_VERSION,
                "parentContractVersion": VERIFIED_DECISION_LEARNING_VERSION,
                "supportedMarkets": [
                    "batter_hits",
                    "batter_total_bases",
                    "batter_home_runs",
                    "batter_rbis",
                    "pitcher_strikeouts",
                ],
                "minimumGradedSamplePerMarket": 10,
                "aggregateOnly": True,
                "trackerRowsIncluded": False,
                "rankingEnabled": False,
                "preferenceMutation": False,
                "recommendation": False,
                "metricsAreDescriptive": True,
                "failClosed": True,
            },
            "verifiedDecisionMarketPreferenceReview": {
                "version": VERIFIED_DECISION_MARKET_PREFERENCE_REVIEW_VERSION,
                "sourceContractVersion": VERIFIED_DECISION_MARKET_LEARNING_VERSION,
                "storageKey": "mlb_market_preferences",
                "explicitUserActionRequired": True,
                "deviceLocal": True,
                "serverPersistence": False,
                "automaticPreferenceMutation": False,
                "rankingEnabled": False,
                "recommendation": False,
                "requiresRepresentedCanonicalMarket": True,
                "syncsDiscoverPreferences": True,
                "failClosed": True,
            },
            "marketPreferenceChangeReceipt": {
                "version": MARKET_PREFERENCE_CHANGE_RECEIPT_VERSION,
                "sourceContractVersion": VERIFIED_DECISION_MARKET_PREFERENCE_REVIEW_VERSION,
                "preferenceStorageKey": "mlb_market_preferences",
                "states": ["idle", "applied", "undone", "unavailable"],
                "receiptPersistence": "session_only",
                "deviceLocal": True,
                "serverPersistence": False,
                "explicitUserActionRequired": True,
                "undoRequiresExplicitAction": True,
                "automaticPreferenceMutation": False,
                "performanceDriven": False,
                "recommendation": False,
                "signalImpactSource": "current_actionable_edges",
                "signalImpactRequiresReadyState": True,
                "failClosed": True,
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

