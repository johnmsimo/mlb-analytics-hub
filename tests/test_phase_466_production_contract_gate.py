import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts.production_contract_gate import (
    ADMIN_READ_PATHS,
    PHASE_56_PAGE_CONTRACTS,
    PUBLIC_PAGE_CONTRACTS,
    ContractError,
    HttpResponse,
    PageContract,
    run_gate,
    validate_actionable_edges,
    validate_page,
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
            for contract in PUBLIC_PAGE_CONTRACTS + PHASE_56_PAGE_CONTRACTS
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
                    "alerts": {
                        "failClosed": True,
                        "serverPersistence": False,
                        "freshness": {"maximumOddsAgeSeconds": 900},
                    },
                }
            )
        if clean_path == "/api/games/today":
            return json_response({"success": True, "games": [], "count": 0})
        if clean_path == "/api/edges/today":
            edges = [valid_edge()]
            probe_date = parse_qs(urlsplit(path).query).get("date", ["today"])[0]
            return json_response(
                {
                    "success": True,
                    "computing": False,
                    "computationState": "ready",
                    "scanJob": None,
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
                            "receiptVersion": "5.4.0",
                            "receiptVerified": True,
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
        "pages": 20,
        "assets": 1,
        "admin_boundaries": 8,
        "api_contracts": 8,
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
    assert "/verification" not in paths
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
