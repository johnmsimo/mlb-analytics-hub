from pathlib import Path

import pytest

from scripts.production_contract_gate import ContractError, validate_actionable_edges


ROOT = Path(__file__).resolve().parents[1]


def computing_payload():
    return {
        "success": True,
        "computing": True,
        "computationState": "computing",
        "scanJob": {
            "id": "job-467",
            "status": "running",
            "elapsedSeconds": 17,
            "attempt": 1,
            "maxAttempts": 2,
            "timeoutSeconds": 600,
            "error": None,
        },
        "message": "Computing recommendations in the durable worker",
        "edges": [],
        "count": 0,
    }


def test_live_gate_requires_bounded_durable_job_metadata():
    validate_actionable_edges(computing_payload())

    missing_job = computing_payload()
    missing_job["scanJob"] = None
    with pytest.raises(ContractError, match="durable job state"):
        validate_actionable_edges(missing_job)

    terminal_job = computing_payload()
    terminal_job["scanJob"]["status"] = "error"
    with pytest.raises(ContractError, match="terminal status"):
        validate_actionable_edges(terminal_job)


def test_live_gate_rejects_failed_or_unavailable_scan_states():
    for state in ("failed", "unavailable"):
        with pytest.raises(ContractError, match=f"computation is {state}"):
            validate_actionable_edges(
                {
                    "success": True,
                    "computing": False,
                    "computationState": state,
                    "scanJob": {
                        "id": "job-467",
                        "status": "error" if state == "failed" else "unavailable",
                    },
                    "message": f"Props scan is {state}",
                    "edges": [],
                    "count": 0,
                }
            )


def test_phase_467_contract_is_installed_and_documented():
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    queue_source = (ROOT / "task_queue.py").read_text(encoding="utf-8")
    gate_source = (ROOT / "scripts" / "production_contract_gate.py").read_text(
        encoding="utf-8"
    )
    roadmap = (ROOT / "docs" / "MLB_ANALYTICS_HUB_ROADMAP.md").read_text(
        encoding="utf-8"
    )

    assert "def get_deduped_job" in queue_source
    assert "Background job exceeded its bounded completion window." in queue_source
    assert "def _props_scan_job_state" in app_source
    assert "'computationState': 'failed'" in app_source
    assert "'canonicalFingerprint': p.get('canonicalFingerprint')" in app_source
    assert "PASS post-scan web isolation" in gate_source
    assert "Phase 4.67 is the active phase." in roadmap
    assert "Durable recommendation scan lifecycle" in roadmap
