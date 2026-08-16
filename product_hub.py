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
PERSONALIZED_SIGNAL_PROVENANCE_VERSION = "4.76"
ALERT_ELIGIBILITY_PROVENANCE_VERSION = "4.77"
VERIFIED_ALERT_REVIEW_HANDOFF_VERSION = "4.78"
VERIFIED_DECISION_ORIGIN_PROVENANCE_VERSION = "4.79"
VERIFIED_DECISION_REVIEW_FRESHNESS_VERSION = "4.80"
VERIFIED_DECISION_LIVE_EXPIRY_VERSION = "4.81"
VERIFIED_DECISION_RESUME_REVALIDATION_VERSION = "4.82"
VERIFIED_DECISION_CROSS_TAB_INVALIDATION_VERSION = "4.83"
VERIFIED_DECISION_EXPLICIT_RECOVERY_VERSION = "4.84"
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
            "personalizedSignalProvenance": {
                "version": PERSONALIZED_SIGNAL_PROVENANCE_VERSION,
                "sourceContractVersion": MARKET_PREFERENCE_CHANGE_RECEIPT_VERSION,
                "sourceEndpoint": "/api/edges/today",
                "reasonOrder": ["preferred_market", "saved_player"],
                "requiresActionable": True,
                "requiresCanonicalPreferredMarket": True,
                "savedPlayerReasonUsesExplicitWatchlist": True,
                "provenanceOnly": True,
                "eligibilityChanged": False,
                "rankingChanged": False,
                "learningPerformanceUsed": False,
                "recommendation": False,
                "serverPersistence": False,
                "failClosed": True,
            },
            "alertEligibilityProvenance": {
                "version": ALERT_ELIGIBILITY_PROVENANCE_VERSION,
                "sourceContractVersion": PERSONALIZED_SIGNAL_PROVENANCE_VERSION,
                "reasonOrder": [
                    "preferred_market",
                    "threshold_match",
                    "fresh_quote",
                    "eligible_event",
                ],
                "eligibleEventKinds": [
                    "new_opportunity",
                    "edge_up",
                    "edge_down",
                    "price_move",
                ],
                "requiresActionable": True,
                "requiresActiveLedgerState": True,
                "provenanceOnly": True,
                "eligibilityChanged": False,
                "ledgerMutation": False,
                "learningPerformanceUsed": False,
                "recommendation": False,
                "serverPersistence": False,
                "failClosed": True,
            },
            "verifiedDecisionExplicitRecovery": {
                "version": VERIFIED_DECISION_EXPLICIT_RECOVERY_VERSION,
                "sourceContractVersion": VERIFIED_DECISION_CROSS_TAB_INVALIDATION_VERSION,
                "draftContractVersion": VERIFIED_DECISION_HANDOFF_VERSION,
                "controlLabel": "Review newest draft",
                "minimumTouchTargetPixels": 44,
                "replacementValidatedBeforeOffer": True,
                "storageReReadOnTap": True,
                "explicitUserActionRequired": True,
                "newDraftAutoOpened": False,
                "clientPostOnReview": False,
                "serverMutationOnReview": False,
                "manualPicksUnaffected": True,
                "requiresExplicitSave": True,
                "saveRequiresAdminAuth": True,
                "canonicalRevalidationOnSave": True,
                "failClosed": True,
            },
            "verifiedDecisionCrossTabInvalidation": {
                "version": VERIFIED_DECISION_CROSS_TAB_INVALIDATION_VERSION,
                "sourceContractVersion": VERIFIED_DECISION_RESUME_REVALIDATION_VERSION,
                "draftContractVersion": VERIFIED_DECISION_HANDOFF_VERSION,
                "storageEvent": "storage",
                "storageKey": "mlb_verified_decision_draft_v471",
                "replacementInvalidatesActiveReview": True,
                "removalInvalidatesActiveReview": True,
                "clearAllInvalidatesActiveReview": True,
                "replacementPreserved": True,
                "newDraftAutoOpened": False,
                "clientPostOnInvalidation": False,
                "serverMutationOnInvalidation": False,
                "manualPicksUnaffected": True,
                "requiresExplicitSave": True,
                "saveRequiresAdminAuth": True,
                "canonicalRevalidationOnSave": True,
                "failClosed": True,
            },
            "verifiedDecisionResumeRevalidation": {
                "version": VERIFIED_DECISION_RESUME_REVALIDATION_VERSION,
                "sourceContractVersion": VERIFIED_DECISION_LIVE_EXPIRY_VERSION,
                "draftContractVersion": VERIFIED_DECISION_HANDOFF_VERSION,
                "resumeEvents": ["visibilitychange", "pageshow"],
                "hiddenTimerPaused": True,
                "visibleTimerRestarted": True,
                "absoluteExpiryRechecked": True,
                "saveDisabledWhenExpired": True,
                "clientPostSuppressedWhenExpired": True,
                "manualPicksUnaffected": True,
                "serverMutationOnResume": False,
                "requiresExplicitSave": True,
                "saveRequiresAdminAuth": True,
                "canonicalRevalidationOnSave": True,
                "failClosed": True,
            },
            "verifiedDecisionLiveExpiry": {
                "version": VERIFIED_DECISION_LIVE_EXPIRY_VERSION,
                "sourceContractVersion": VERIFIED_DECISION_REVIEW_FRESHNESS_VERSION,
                "draftContractVersion": VERIFIED_DECISION_HANDOFF_VERSION,
                "freshnessStates": ["fresh", "expired"],
                "countdownIntervalMilliseconds": 1000,
                "visibleCountdown": True,
                "accessibleStateAnnouncement": True,
                "saveDisabledWhenExpired": True,
                "clientPostSuppressedWhenExpired": True,
                "manualPicksUnaffected": True,
                "serverMutationOnExpiry": False,
                "requiresExplicitSave": True,
                "saveRequiresAdminAuth": True,
                "canonicalRevalidationOnSave": True,
                "failClosed": True,
            },
            "verifiedDecisionReviewFreshness": {
                "version": VERIFIED_DECISION_REVIEW_FRESHNESS_VERSION,
                "sourceContractVersion": VERIFIED_DECISION_ORIGIN_PROVENANCE_VERSION,
                "draftContractVersion": VERIFIED_DECISION_HANDOFF_VERSION,
                "freshnessField": "expiresAt",
                "displayedBeforeSave": True,
                "revalidatedOnSaveAttempt": True,
                "clientPostSuppressedWhenExpired": True,
                "serverMutationOnExpiry": False,
                "recommendationChanged": False,
                "requiresExplicitSave": True,
                "saveRequiresAdminAuth": True,
                "canonicalRevalidationOnSave": True,
                "failClosed": True,
            },
            "verifiedDecisionOriginProvenance": {
                "version": VERIFIED_DECISION_ORIGIN_PROVENANCE_VERSION,
                "sourceContractVersion": VERIFIED_ALERT_REVIEW_HANDOFF_VERSION,
                "draftContractVersion": VERIFIED_DECISION_HANDOFF_VERSION,
                "allowedOrigins": ["saved_player_digest", "eligible_alert"],
                "requiresOrigin": True,
                "displayedBeforeSave": True,
                "originAffectsRecommendation": False,
                "originAffectsAuthorization": False,
                "originAffectsCanonicalRevalidation": False,
                "serverMutationOnReview": False,
                "requiresExplicitSave": True,
                "failClosed": True,
            },
            "verifiedAlertReviewHandoff": {
                "version": VERIFIED_ALERT_REVIEW_HANDOFF_VERSION,
                "sourceContractVersion": ALERT_ELIGIBILITY_PROVENANCE_VERSION,
                "draftContractVersion": VERIFIED_DECISION_HANDOFF_VERSION,
                "source": "eligible_alert",
                "destination": "/tracker",
                "storageKey": "mlb_verified_decision_draft_v471",
                "requiresAlertProvenance": True,
                "requiresActionable": True,
                "requiresFreshQuote": True,
                "requiresActiveLedgerState": True,
                "explicitUserActionRequired": True,
                "expiresWithQuote": True,
                "serverMutationOnPrepare": False,
                "ledgerMutationOnPrepare": False,
                "requiresExplicitSave": True,
                "saveRequiresAdminAuth": True,
                "canonicalRevalidationOnSave": True,
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

