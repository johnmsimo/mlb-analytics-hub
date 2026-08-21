import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts.production_contract_gate import (
    ADMIN_READ_PATHS,
    PHASE_511_PAGE_CONTRACTS,
    PHASE_56_PAGE_CONTRACTS,
    PUBLIC_PAGE_CONTRACTS,
    ContractError,
    HttpResponse,
    PageContract,
    run_gate,
    validate_actionable_edges,
    validate_page,
    _validate_accuracy_control_plane,
    _validate_accuracy_intelligence,
    _validate_public_verification,
)


ROOT = Path(__file__).resolve().parents[1]


def response(status=200, body=b"", content_type="application/json", elapsed=0.05):
    return HttpResponse(
        status=status,
        headers={"content-type": content_type},
        body=body,
        elapsed_seconds=elapsed,
    )


def json_response(payload, status=200, elapsed=0.05):
    return response(
        status=status,
        body=json.dumps(payload).encode("utf-8"),
        elapsed=elapsed,
    )



def valid_shopping():
    return {
        "version": "5.9",
        "sourceDecisionVersion": "5.3.0",
        "state": "ready",
        "reviewRequired": True,
        "changesRecommendation": False,
        "providerHealth": {
            "provider": "The Odds API",
            "state": "ready",
            "configured": True,
            "capturedAt": "2026-08-15T12:00:00+00:00",
            "eventCount": 15,
            "fetchedEventCount": 15,
            "degradedEventCount": 0,
            "message": "Fresh multi-book prices are available.",
        },
        "consensus": {
            "requiredBooks": 2,
            "acceptedBookCount": 2,
            "rejectedQuoteCount": 0,
            "fairProbability": 0.55,
            "spread": 0.01,
            "maximumSpread": 0.08,
        },
        "priceShopping": {
            "bestAvailableBook": "Book B",
            "bestAvailablePrice": -105,
            "capturedAt": "2026-08-15T12:00:00+00:00",
            "quotes": [
                {
                    "book": "Book B",
                    "source": "the-odds-api",
                    "capturedAt": "2026-08-15T12:00:00+00:00",
                    "ageSeconds": 10,
                    "line": 0.5,
                    "overPrice": -105,
                    "underPrice": -115,
                    "selectedPrice": -105,
                    "fairProbability": 0.53,
                },
                {
                    "book": "Book A",
                    "source": "the-odds-api",
                    "capturedAt": "2026-08-15T12:00:00+00:00",
                    "ageSeconds": 10,
                    "line": 0.5,
                    "overPrice": -110,
                    "underPrice": -110,
                    "selectedPrice": -110,
                    "fairProbability": 0.50,
                },
            ],
        },
        "decision": {
            "status": "qualified",
            "qualifiedForReview": True,
            "approved": False,
            "reasons": [],
            "modelEdge": 0.11,
            "expectedValue": 0.20,
            "thresholds": {"minimumEdge": 0.025, "minimumExpectedValue": 0.03},
            "checkedAt": "2026-08-15T12:00:00+00:00",
            "fingerprint": "decision-1",
        },
    }


def valid_edge():
    observed_at = "2026-08-15T12:00:00+00:00"
    return {
        "actionable": True,
        "actionabilityStage": "Actionable",
        "player": "Contract Hitter",
        "playerId": 101,
        "canonicalCandidateId": "candidate-101-hits",
        "canonicalFingerprint": "snapshot-1",
        "canonicalMarketKey": "batter_hits",
        "canonicalSide": "Over",
        "line": 0.5,
        "canonicalPrice": -110,
        "canonicalBook": "Book A",
        "canonicalEdge": 0.05,
        "oddsUpdatedAt": observed_at,
        "multiBookShoppingVersion": "5.9",
        "multiBookShopping": valid_shopping(),
        "evidenceReceipt": {
            "contractVersion": "4.69",
            "candidateId": "candidate-101-hits",
            "fingerprint": "snapshot-1",
            "selection": {
                "marketKey": "batter_hits",
                "side": "Over",
                "line": 0.5,
            },
            "price": {
                "american": -110,
                "book": "Book A",
                "observedAt": observed_at,
                "ageSeconds": 10,
                "maximumAgeSeconds": 900,
                "fresh": True,
            },
            "model": {
                "probability": 0.60,
                "version": "contract-model-1",
            },
            "market": {
                "impliedProbability": 0.52381,
                "fairProbability": 0.55,
                "edge": 0.05,
            },
            "validation": {
                "actionable": True,
                "actionabilityStage": "Actionable",
                "candidateIntegrityVersion": "4.37",
                "marketValidationVersion": "4.38",
                "calibrationStatus": "passed",
                "marketGateStatus": "promoted",
            },
            "explanation": "Model probability exceeds the de-vigged fair market.",
        },
    }


class FakeProduction:
    def __init__(self, *, exposed_admin_path=None):
        self.calls = []
        self.exposed_admin_path = exposed_admin_path
        self.page_markers = {
            contract.path: contract.marker
            for contract in (
                PUBLIC_PAGE_CONTRACTS
                + PHASE_56_PAGE_CONTRACTS
                + PHASE_511_PAGE_CONTRACTS
            )
        }

    def __call__(self, base_url, path, timeout):
        self.calls.append((base_url, path, timeout))
        clean_path = urlsplit(path).path
        if clean_path == "/health":
            return json_response({"status": "ok", "version": "tested-sha"})
        if clean_path == "/ready":
            return json_response(
                {
                    "status": "ready",
                    "jobs": {"connected": True, "workerReady": True},
                }
            )
        if path in self.page_markers:
            marker = self.page_markers[path]
            html = (
                '<!doctype html><html><head><meta name="viewport" '
                'content="width=device-width"><script src="/static/app.js"></script>'
                f"<title>{marker}</title></head><body>{marker}"
                + ("x" * 600)
                + "</body></html>"
            )
            return response(body=html.encode("utf-8"), content_type="text/html")
        if clean_path == "/static/app.js":
            return response(body=b"window.contractGate = true;", content_type="text/javascript")
        if clean_path in ADMIN_READ_PATHS:
            status = 200 if clean_path == self.exposed_admin_path else 401
            return json_response({"success": status == 200}, status=status)
        if clean_path == "/api/product/journey":
            return json_response(
                {
                    "success": True,
                    "version": "4.64",
                    "stages": [
                        {"key": "discover"},
                        {"key": "validate"},
                        {"key": "track"},
                        {"key": "learn"},
                    ],
                    "dailyDecisionBoard": {
                        "version": "5.5",
                        "maximumCards": 8,
                        "rawRejectedRowsIncluded": False,
                        "noBetIsValidDecision": True,
                        "failClosed": True,
                    },
                    "productionMultiBookShopping": {
                        "version": "5.9",
                        "sourceDecisionEngineVersion": "5.3.0",
                        "minimumFreshBooks": 2,
                        "maximumQuoteAgeSeconds": 300,
                        "visibleOnCards": [
                            "daily_decision_board",
                            "personalized_signal",
                            "saved_player_opportunity",
                            "eligible_alert",
                        ],
                        "rawRejectedQuotesIncluded": False,
                        "bankrollIncluded": False,
                        "stakeDollarsIncluded": False,
                        "changesRecommendation": False,
                        "serverMutation": False,
                        "failClosed": True,
                    },
                    "guidedParlays": {
                        "version": "5.10",
                        "sourceEndpoint": "/api/parlay/auto",
                        "surface": "/edge-lab#parlays",
                        "states": [
                            "ready",
                            "no_verified_combinations",
                            "computing",
                            "failed",
                            "unavailable",
                        ],
                        "minimumVerifiedLegs": 2,
                        "maximumGuidedLegs": 4,
                        "requiresEvidenceReceiptVersion": "4.69",
                        "requiresMultiBookShoppingVersion": "5.9",
                        "requiresReadyMultiBookConsensus": True,
                        "correlationWarningsRequired": True,
                        "unresolvedSameGameCorrelationTrackable": False,
                        "combinedRiskExplanationRequired": True,
                        "referencePriceIsBookOffer": False,
                        "reviewRequired": True,
                        "approved": False,
                        "readOnly": True,
                        "serverMutation": False,
                        "failClosed": True,
                    },
                    "monetizationGrowth": {
                        "version": "5.11",
                        "sourceEndpoint": "/api/monetization/status",
                        "surface": "/pricing",
                        "rolloutState": "identity_required",
                        "freeUsageEnforcementMode": "shadow",
                        "premiumEntitlementSource": "server_verified_subscription",
                        "clientStorageCanGrantPremium": False,
                        "anonymousSessionCanGrantPremium": False,
                        "checkoutAvailable": False,
                        "requiresVerifiedCustomerIdentity": True,
                        "requiresWebhookReconciliation": True,
                        "onboardingStorageKey": "mlb_growth_onboarding_v511",
                        "referralStorageKey": "mlb_growth_referral_v511",
                        "conversionLedgerStorageKey": "mlb_growth_events_v511",
                        "growthPersistence": "device_private",
                        "serverAnalyticsCollection": False,
                        "rawPersonalDataIncluded": False,
                        "serverMutation": False,
                        "failClosed": True,
                    },
                    "accuracyControlPlane": {
                        "version": "6.0",
                        "sourceEndpoint": "/api/accuracy/control-plane?window=90",
                        "surface": "/verification#accuracyControlPlane",
                        "benchmarkType": "side_correct_two_way_power_devig_close",
                        "minimumPairedSample": 500,
                        "minimumClvSample": 500,
                        "beatCloseTarget": 0.524,
                        "requiresImmutablePredictionReceipt": True,
                        "requiresClosingBenchmarkReceipt": True,
                        "requiresExactLine": True,
                        "requiresAcceptedClosingIntegrity": True,
                        "requiresBrierConfidence": True,
                        "requiresBeatCloseConfidence": True,
                        "industryClaimDefaultsToFalse": True,
                        "privateTrackerFieldsIncluded": False,
                        "automaticModelChange": False,
                        "automaticThresholdChange": False,
                        "serverMutation": False,
                        "failClosed": True,
                    },
                    "accuracyIntelligenceProgram": {
                        "version": "6.5",
                        "sourceEndpoint": "/api/accuracy/intelligence?window=120",
                        "surface": "/verification#intelligenceProgram",
                        "phaseVersions": {
                            "errorAtlas": "6.1",
                            "championChallenger": "6.2",
                            "driftControl": "6.3",
                            "simulationCalibration": "6.4",
                            "policyLab": "6.5",
                        },
                        "requiresPredictionReceiptVersion": "5.4.0",
                        "requiresClosingBenchmarkReceiptVersion": "6.0",
                        "requiresIntelligenceEvidenceReceiptVersion": "6.5.0",
                        "minimumContextSample": 30,
                        "minimumChallengerTotalSample": 300,
                        "minimumSimulationSample": 100,
                        "minimumCorrelationPairs": 50,
                        "driftMayDowngradeOrSuppress": True,
                        "unverifiedCorrelationTrackable": False,
                        "rawRowsIncluded": False,
                        "automaticModelPromotion": False,
                        "automaticRetraining": False,
                        "automaticProbabilityChange": False,
                        "automaticThresholdChange": False,
                        "automaticStakingChange": False,
                        "humanReviewRequired": True,
                        "serverMutation": False,
                        "failClosed": True,
                    },
                    "alerts": {
                        "failClosed": True,
                        "serverPersistence": False,
                        "freshness": {"maximumOddsAgeSeconds": 900},
                    },
                }
            )
        if clean_path == "/api/games/today":
            return json_response({"success": True, "games": [], "count": 0})
        if clean_path == "/api/parlay/auto":
            return json_response(
                {
                    "success": True,
                    "version": "5.10",
                    "date": "2026-08-20",
                    "state": "no_verified_combinations",
                    "candidateCount": 0,
                    "verifiedCandidateCount": 0,
                    "withheldCandidateCount": 0,
                    "withheldReasonCounts": {},
                    "cached": True,
                    "computing": False,
                    "message": None,
                    "generatedAt": "2026-08-20T18:00:00+00:00",
                    "minimumVerifiedLegs": 2,
                    "maximumGuidedLegs": 4,
                    "requiresEvidenceReceiptVersion": "4.69",
                    "requiresMultiBookShoppingVersion": "5.9",
                    "reviewRequired": True,
                    "approved": False,
                    "readOnly": True,
                    "failClosed": True,
                    "parlays": [],
                }
            )
        if clean_path == "/api/edges/today":
            edges = [valid_edge()]
            probe_date = parse_qs(urlsplit(path).query).get("date", ["today"])[0]
            return json_response(
                {
                    "success": True,
                    "computing": False,
                    "computationState": "ready",
                    "scanJob": None,
                    "multiBookShoppingVersion": "5.9",
                    "oddsProviderHealth": valid_shopping()["providerHealth"],
                    "completionReceipt": {
                        "contractVersion": "4.68",
                        "source": "durable-worker",
                        "date": probe_date,
                        "completedAt": "2026-08-15T12:00:00+00:00",
                        "release": "tested-sha",
                    },
                    "edges": edges,
                    "count": len(edges),
                }
            )
        if clean_path == "/api/calibration/markets":
            return json_response({"success": True, "markets": []})
        if clean_path == "/api/tracker/performance":
            return json_response({"success": True})
        if clean_path == "/api/verification/ledger":
            return json_response(
                {
                    "success": True,
                    "version": "5.6",
                    "readOnly": True,
                    "failClosed": True,
                    "lossesOmitted": False,
                    "privateTrackerFieldsIncluded": False,
                    "metrics": {
                        "releasedCount": 1,
                        "gradedCount": 1,
                        "wins": 0,
                        "losses": 1,
                        "pending": 0,
                        "clvGradedCount": 0,
                        "roiEligibleCount": 1,
                    },
                    "ledger": [
                        {
                            "publicId": "receipt-1",
                            "receiptFingerprint": "receipt-1-full",
                            "receiptVersion": "5.6",
                            "receiptVerified": True,
                            "predictionFingerprint": "prediction-1-full",
                            "predictionReceiptVersion": "5.4.0",
                            "releasedAt": "2026-08-19T16:00:00+00:00",
                            "gradedAt": "2026-08-20T02:00:00+00:00",
                            "gamePk": 777,
                            "player": "Contract Hitter",
                            "marketKey": "batter_hits",
                            "side": "Over",
                            "line": 0.5,
                            "probability": 0.6,
                            "sportsbook": "Book A",
                            "openingPrice": -110,
                            "closingPrice": None,
                            "clvEdge": None,
                            "result": "loss",
                        }
                    ],
                    "withheld": {
                        "count": 0,
                        "reasonCounts": {},
                        "rawRowsIncluded": False,
                    },
                }
            )
        if clean_path == "/api/accuracy/control-plane":
            return json_response(
                {
                    "success": True,
                    "version": "6.0",
                    "state": "insufficient_sample",
                    "readOnly": True,
                    "failClosed": True,
                    "industryClaimMade": False,
                    "window": {"days": 90, "from": "2026-05-24", "through": "2026-08-21"},
                    "generatedAt": "2026-08-21T12:00:00+00:00",
                    "overall": {
                        "state": "insufficient_sample",
                        "claimEligible": False,
                        "pairedSampleSize": 0,
                        "modelBrier": None,
                        "closingMarketBrier": None,
                        "pairedBrierDelta": None,
                        "pairedBrierDeltaInterval": {"lower": None, "upper": None, "confidenceLevel": 0.95},
                        "relativeBrierSkillVsClose": None,
                        "brierEvidence": "insufficient_sample",
                        "modelEce": None,
                        "closingMarketEce": None,
                        "clvGradedCount": 0,
                        "beatCloseCount": 0,
                        "beatCloseRate": None,
                        "beatCloseInterval": {"lower": None, "upper": None, "confidenceLevel": 0.95},
                        "minimumClaimSample": 500,
                        "beatCloseTarget": 0.524,
                    },
                    "byMarket": {},
                    "coverage": {
                        "pairedEligibleCount": 0,
                        "rejectedCount": 0,
                        "rejectedReasonCounts": {},
                        "rawRowsIncluded": False,
                    },
                    "benchmark": {
                        "version": "6.0",
                        "type": "side_correct_two_way_power_devig_close",
                        "requiresExactLine": True,
                        "requiresAcceptedClosingIntegrity": True,
                        "outcomesFrozenAfterPrediction": True,
                    },
                    "claimPolicy": {
                        "minimumPairedSample": 500,
                        "minimumClvSample": 500,
                        "beatCloseTarget": 0.524,
                        "requiresModelBrierConfidenceUpperBelowZero": True,
                        "requiresBeatCloseWilsonLowerAboveHalf": True,
                    },
                    "privateTrackerFieldsIncluded": False,
                    "automaticModelChange": False,
                    "automaticThresholdChange": False,
                    "serverMutation": False,
                }
            )
        if clean_path == "/api/accuracy/intelligence":
            return json_response(
                {
                    "success": True,
                    "version": "6.5",
                    "state": "insufficient_sample",
                    "generatedAt": "2026-08-21T12:00:00+00:00",
                    "window": {"days": 120, "from": "2026-04-24", "through": "2026-08-21"},
                    "coverage": {
                        "verifiedObservationCount": 0,
                        "rejectedObservationCount": 0,
                        "rejectedReasonCounts": {},
                        "rawRowsIncluded": False,
                    },
                    "phases": {
                        "errorAtlas": {
                            "version": "6.1", "state": "insufficient_sample",
                            "minimumVisibleSample": 30, "cohorts": [],
                            "rawRowsIncluded": False,
                        },
                        "championChallenger": {
                            "version": "6.2", "state": "insufficient_sample",
                            "challengers": [], "automaticPromotion": False,
                            "humanReviewRequired": True,
                        },
                        "driftControl": {
                            "version": "6.3", "state": "insufficient_sample",
                            "markets": {}, "mayRetrainModel": False,
                            "mayPromoteModel": False,
                        },
                        "simulationCalibration": {
                            "version": "6.4", "state": "insufficient_sample",
                            "simulation": {"sampleSize": 0}, "correlationPairs": [],
                            "unverifiedCorrelationTrackable": False,
                            "rawRowsIncluded": False,
                        },
                        "policyLab": {
                            "version": "6.5", "state": "insufficient_sample",
                            "proposals": [], "automaticThresholdChange": False,
                            "automaticStakingChange": False,
                        },
                    },
                    "safety": {
                        "readOnly": True,
                        "failClosed": True,
                        "privateTrackerFieldsIncluded": False,
                        "automaticModelPromotion": False,
                        "automaticRetraining": False,
                        "automaticProbabilityChange": False,
                        "automaticThresholdChange": False,
                        "automaticStakingChange": False,
                        "humanReviewRequired": True,
                    },
                    "serverMutation": False,
                }
            )
        if clean_path == "/api/monetization/status":
            return json_response(
                {
                    "success": True,
                    "version": "5.11",
                    "rolloutState": "identity_required",
                    "plans": [
                        {
                            "key": "free",
                            "label": "Free",
                            "availability": "available",
                            "price": None,
                            "features": ["Daily Decision Board"],
                        },
                        {
                            "key": "premium",
                            "label": "Premium",
                            "availability": "preview",
                            "price": None,
                            "features": ["Server entitlement"],
                        },
                    ],
                    "freeUsage": {
                        "measurement": "daily_decision_board_view",
                        "configuredLimit": None,
                        "enforcementMode": "shadow",
                        "hardLimitEnabled": False,
                        "counterPersistence": "device_private",
                        "reason": "Verified identity required.",
                    },
                    "premiumEntitlement": {
                        "state": "unavailable",
                        "source": "server_verified_subscription",
                        "clientStorageCanGrant": False,
                        "anonymousSessionCanGrant": False,
                        "failClosed": True,
                    },
                    "billing": {
                        "state": "identity_required",
                        "checkoutAvailable": False,
                        "provider": None,
                        "priceDecisionRecorded": False,
                        "webhookReconciliationRequired": True,
                        "blockers": ["Identity", "Adapter", "Webhooks"],
                    },
                    "onboarding": {
                        "state": "available",
                        "persistence": "device_private",
                        "storageKey": "mlb_growth_onboarding_v511",
                        "steps": [
                            "review_daily_board",
                            "save_player",
                            "inspect_evidence",
                            "open_tracker",
                        ],
                    },
                    "referrals": {
                        "state": "device_attribution",
                        "queryParameter": "ref",
                        "acceptedPattern": "^[A-Za-z0-9_-]{3,32}$",
                        "persistence": "device_private",
                        "storageKey": "mlb_growth_referral_v511",
                        "rawPersonalDataIncluded": False,
                    },
                    "conversionAnalytics": {
                        "state": "device_receipts",
                        "persistence": "device_private",
                        "storageKey": "mlb_growth_events_v511",
                        "maximumReceipts": 100,
                        "serverCollection": False,
                        "rawPersonalDataIncluded": False,
                        "events": [
                            "pricing_viewed",
                            "premium_interest",
                            "onboarding_step_completed",
                            "referral_landed",
                        ],
                    },
                    "readOnly": True,
                    "serverMutation": False,
                    "rawPersonalDataIncluded": False,
                    "failClosed": True,
                }
            )
        raise AssertionError(f"unexpected path {path}")


def test_page_contract_requires_complete_mobile_html():
    contract = PageContract("/example", "Expected Product")
    html = (
        '<!doctype html><meta name="viewport" content="width=device-width">'
        "<title>Expected Product</title>"
        + ("x" * 600)
    )
    assets = validate_page(
        contract,
        response(body=html.encode("utf-8"), content_type="text/html"),
    )
    assert assets == set()

    with pytest.raises(ContractError, match="mobile viewport"):
        validate_page(
            contract,
            response(
                body=("<title>Expected Product</title>" + ("x" * 600)).encode(),
                content_type="text/html",
            ),
        )


def test_actionable_edges_contract_fails_closed():
    edge = valid_edge()
    validate_actionable_edges(
        {
            "success": True,
            "computing": False,
            "computationState": "ready",
            "multiBookShoppingVersion": "5.9",
            "oddsProviderHealth": valid_shopping()["providerHealth"],
            "edges": [edge],
            "count": 1,
        }
    )

    invalid = dict(edge, canonicalBook="model")
    with pytest.raises(ContractError, match="sportsbook identity"):
        validate_actionable_edges(
            {
                "success": True,
                "computing": False,
                "computationState": "ready",
                "multiBookShoppingVersion": "5.9",
                "oddsProviderHealth": valid_shopping()["providerHealth"],
                "edges": [invalid],
                "count": 1,
            }
        )

    validate_actionable_edges(
        {
            "success": True,
            "computing": True,
            "computationState": "computing",
            "scanJob": {
                "id": "job-466",
                "status": "running",
                "elapsedSeconds": 3,
                "timeoutSeconds": 600,
            },
            "message": "Computing with recommendations withheld",
            "edges": [],
            "count": 0,
        }
    )
    with pytest.raises(ContractError, match="zero rows"):
        validate_actionable_edges(
            {
                "success": True,
                "computing": True,
                "computationState": "computing",
                "scanJob": {
                    "id": "job-466",
                    "status": "queued",
                    "elapsedSeconds": 0,
                    "timeoutSeconds": 600,
                },
                "message": "Computing",
                "edges": [edge],
                "count": 1,
            }
        )


def test_full_gate_uses_only_get_contracts_and_reports_coverage():
    fake = FakeProduction()
    summary = run_gate(
        base_url="https://production.example",
        expected_sha="tested-sha",
        fetcher=fake,
        release_attempts=1,
        contract_attempts=1,
        retry_delay=0,
        sleeper=lambda delay: None,
    )

    assert summary == {
        "pages": 21,
        "assets": 1,
        "admin_boundaries": 8,
        "api_contracts": 12,
        "worker_convergence": 1,
    }
    assert all(call[1] for call in fake.calls)
    paths = [urlsplit(call[1]).path for call in fake.calls]
    assert paths.count("/health") == 2
    assert paths.count("/ready") == 2


def test_public_verification_contract_keeps_losses_and_public_allowlist():
    fake = FakeProduction()
    payload = fake("https://production.example", "/api/verification/ledger", 5).json()
    _validate_public_verification(payload)

    payload["lossesOmitted"] = True
    with pytest.raises(ContractError, match="omit losses"):
        _validate_public_verification(payload)


def test_accuracy_control_plane_contract_withholds_unqualified_claims():
    fake = FakeProduction()
    payload = fake("https://production.example", "/api/accuracy/control-plane", 5).json()
    _validate_accuracy_control_plane(payload)

    payload["industryClaimMade"] = True
    with pytest.raises(ContractError, match="escaped its gate"):
        _validate_accuracy_control_plane(payload)


def test_accuracy_intelligence_contract_keeps_review_and_privacy_boundaries():
    fake = FakeProduction()
    payload = fake("https://production.example", "/api/accuracy/intelligence", 5).json()
    _validate_accuracy_intelligence(payload)

    payload["safety"]["automaticModelPromotion"] = True
    with pytest.raises(ContractError, match="opened automaticModelPromotion"):
        _validate_accuracy_intelligence(payload)


def test_baseline_gate_defers_new_deployed_api_contracts():
    fake = FakeProduction()
    summary = run_gate(
        base_url="https://production.example",
        expected_sha="tested-sha",
        fetcher=fake,
        release_attempts=1,
        contract_attempts=1,
        retry_delay=0,
        sleeper=lambda delay: None,
        baseline_only=True,
    )

    paths = [urlsplit(call[1]).path for call in fake.calls]
    assert summary["api_contracts"] == 4
    assert summary["worker_convergence"] == 0
    assert "/api/edges/today" not in paths
    assert "/api/calibration/markets" not in paths
    assert "/api/tracker/performance" not in paths
    assert "/api/verification/ledger" not in paths
    assert "/api/accuracy/control-plane" not in paths
    assert "/api/accuracy/intelligence" not in paths
    assert "/verification" not in paths
    assert "/pricing" not in paths
    assert "/api/monetization/status" not in paths
    assert summary["pages"] == 19


def test_full_gate_rejects_an_exposed_admin_read():
    fake = FakeProduction(exposed_admin_path="/settings")
    with pytest.raises(ContractError, match="failed closed boundary"):
        run_gate(
            base_url="https://production.example",
            expected_sha="tested-sha",
            fetcher=fake,
            release_attempts=1,
            contract_attempts=1,
            retry_delay=0,
            sleeper=lambda delay: None,
        )


def test_phase_466_workflow_and_roadmap_install_live_gate():
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(
        encoding="utf-8"
    )
    roadmap = (ROOT / "docs" / "MLB_ANALYTICS_HUB_ROADMAP.md").read_text(
        encoding="utf-8"
    )
    worker = (ROOT / "worker.py").read_text(encoding="utf-8")

    assert '"props_scan": props_scan' in worker
    assert "_compute_props_scan_today_payload" in worker
    assert "_write_props_scan_durable_snapshot" in worker
    assert workflow.count("scripts/production_contract_gate.py") == 2
    assert "Validate current production contract" in workflow
    assert "--baseline" in workflow
    assert "Production smoke and readiness gate" in workflow
    assert "--expected-sha ${{ github.sha }}" in workflow
    assert "### Phase 4.66 — Declarative live production contract gate" in roadmap
    assert "Declarative live production contract gate" in roadmap
