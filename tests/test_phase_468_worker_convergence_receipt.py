import json
from datetime import date
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts.production_contract_gate import (
    ContractError,
    HttpResponse,
    convergence_probe_date,
    validate_completion_receipt,
    wait_for_edge_convergence,
)


def json_response(payload, *, elapsed=0.05):
    return HttpResponse(
        status=200,
        headers={"content-type": "application/json"},
        body=json.dumps(payload).encode("utf-8"),
        elapsed_seconds=elapsed,
    )


class ConvergingProduction:
    def __init__(self, expected_sha, probe_date):
        self.expected_sha = expected_sha
        self.probe_date = probe_date
        self.edge_calls = 0
        self.paths = []

    def __call__(self, _base_url, path, _timeout):
        self.paths.append(urlsplit(path).path)
        clean_path = urlsplit(path).path
        if clean_path == "/health":
            return json_response({"status": "ok", "version": self.expected_sha})
        if clean_path == "/ready":
            return json_response(
                {
                    "status": "ready",
                    "jobs": {"connected": True, "workerReady": True},
                }
            )
        if clean_path == "/api/edges/today":
            query = parse_qs(urlsplit(path).query)
            assert query["date"] == [self.probe_date]
            assert query["requiredRelease"] == [self.expected_sha]
            self.edge_calls += 1
            if self.edge_calls == 1:
                return json_response(
                    {
                        "success": True,
                        "computing": True,
                        "computationState": "computing",
                        "scanJob": {
                            "id": "job-468",
                            "status": "running",
                            "elapsedSeconds": 9,
                            "attempt": 1,
                            "maxAttempts": 2,
                            "timeoutSeconds": 600,
                            "error": None,
                        },
                        "message": "Computing in durable worker",
                        "edges": [],
                        "count": 0,
                    }
                )
            return json_response(
                {
                    "success": True,
                    "computing": False,
                    "computationState": "ready",
                    "completionReceipt": {
                        "contractVersion": "4.68",
                        "source": "durable-worker",
                        "date": self.probe_date,
                        "completedAt": "2026-08-15T12:00:00+00:00",
                        "release": self.expected_sha,
                    },
                    "edges": [],
                    "count": 0,
                }
            )
        raise AssertionError(f"unexpected path {path}")


def test_convergence_waits_for_worker_receipt_and_keeps_web_ready():
    expected_sha = "a" * 40
    probe_date = "2026-08-18"
    fake = ConvergingProduction(expected_sha, probe_date)
    delays = []

    payload = wait_for_edge_convergence(
        base_url="https://production.example",
        expected_sha=expected_sha,
        probe_date=probe_date,
        fetcher=fake,
        attempts=2,
        retry_delay=10,
        sleeper=delays.append,
    )

    assert payload["computationState"] == "ready"
    assert payload["completionReceipt"]["release"] == expected_sha
    assert fake.edge_calls == 2
    assert fake.paths.count("/health") == 1
    assert fake.paths.count("/ready") == 1
    assert delays == [10]


def test_completion_receipt_is_bound_to_worker_date_and_release():
    payload = {
        "computationState": "ready",
        "completionReceipt": {
            "contractVersion": "4.68",
            "source": "durable-worker",
            "date": "2026-08-18",
            "completedAt": "2026-08-15T12:00:00+00:00",
            "release": "expected-sha",
        },
    }
    validate_completion_receipt(
        payload,
        expected_sha="expected-sha",
        probe_date="2026-08-18",
    )

    payload["completionReceipt"]["release"] = "old-sha"
    with pytest.raises(ContractError, match="release mismatch"):
        validate_completion_receipt(
            payload,
            expected_sha="expected-sha",
            probe_date="2026-08-18",
        )


def test_probe_date_is_deterministic_and_future_bounded():
    today = date(2026, 8, 15)
    first = convergence_probe_date("release-sha", today=today)
    second = convergence_probe_date("release-sha", today=today)

    assert first == second
    assert date.fromisoformat(first) > today
    assert (date.fromisoformat(first) - today).days <= 7


def test_phase_468_contract_is_installed_and_documented():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    worker = (root / "worker.py").read_text(encoding="utf-8")
    app_source = (root / "app.py").read_text(encoding="utf-8")
    gate = (root / "scripts" / "production_contract_gate.py").read_text(
        encoding="utf-8"
    )
    workflow = (root / ".github" / "workflows" / "deploy.yml").read_text(
        encoding="utf-8"
    )
    roadmap = (root / "docs" / "MLB_ANALYTICS_HUB_ROADMAP.md").read_text(
        encoding="utf-8"
    )

    assert '"contractVersion": "4.68"' in worker
    assert '"source": "durable-worker"' in worker
    assert '"release": app_module._APP_VERSION' in worker
    assert 'required_release != app_module._APP_VERSION' in worker
    assert "'completionReceipt': base.get('completionReceipt')" in app_source
    assert "def _props_scan_dedupe_key" in app_source
    assert "required_release=required_release" in app_source
    assert "def wait_for_edge_convergence" in gate
    assert "requiredRelease=" in gate
    assert "--settle-attempts" in gate
    assert "--settle-attempts 61" in workflow
    assert "--settle-delay 10" in workflow
    assert "### Phase 4.68 — Durable worker convergence receipt" in roadmap\n    assert "Phase 4.69 is the active phase." in roadmap
    assert "Durable worker convergence receipt" in roadmap
