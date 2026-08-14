import io
import subprocess
from pathlib import Path

from scripts.deploy_with_lease_retry import (
    is_retryable_lease_conflict,
    parse_args,
    run_with_lease_retry,
)


ROOT = Path(__file__).resolve().parents[1]
LEASE_CONFLICT = """
Error: failed to acquire leases: Unrecoverable error: failed to get lease on VM
784750df563738: machine ID 784750df563738 lease currently held by a deployment token
"""


def completed(command, returncode, output):
    return subprocess.CompletedProcess(command, returncode, stdout=output)


def test_retry_classifier_requires_both_fly_lease_markers():
    assert is_retryable_lease_conflict(LEASE_CONFLICT) is True
    assert is_retryable_lease_conflict("failed to acquire lease") is False
    assert is_retryable_lease_conflict("lease currently held") is False
    assert is_retryable_lease_conflict("remote builder timed out") is False


def test_lease_conflict_retries_then_succeeds():
    calls = []
    results = [
        completed(["flyctl", "deploy"], 1, LEASE_CONFLICT),
        completed(["flyctl", "deploy"], 0, "deployed\n"),
    ]

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return results.pop(0)

    sleeps = []
    stream = io.StringIO()
    returncode = run_with_lease_retry(
        ["flyctl", "deploy"],
        attempts=4,
        base_delay_seconds=2,
        runner=runner,
        sleeper=sleeps.append,
        stream=stream,
    )

    assert returncode == 0
    assert len(calls) == 2
    assert sleeps == [2]
    assert "retrying in 2s (2/4)" in stream.getvalue()
    assert stream.getvalue().endswith("deployed\n")


def test_non_lease_failure_fails_immediately():
    calls = []
    sleeps = []

    def runner(command, **kwargs):
        calls.append(command)
        return completed(command, 42, "Error: authentication failed\n")

    returncode = run_with_lease_retry(
        ["flyctl", "deploy"],
        attempts=4,
        runner=runner,
        sleeper=sleeps.append,
        stream=io.StringIO(),
    )

    assert returncode == 42
    assert calls == [["flyctl", "deploy"]]
    assert sleeps == []


def test_lease_retry_is_bounded_and_returns_final_failure():
    calls = []
    sleeps = []

    def runner(command, **kwargs):
        calls.append(command)
        return completed(command, 1, LEASE_CONFLICT)

    returncode = run_with_lease_retry(
        ["flyctl", "deploy"],
        attempts=3,
        base_delay_seconds=1,
        runner=runner,
        sleeper=sleeps.append,
        stream=io.StringIO(),
    )

    assert returncode == 1
    assert len(calls) == 3
    assert sleeps == [1, 2]


def test_cli_preserves_command_after_separator():
    args = parse_args(
        [
            "--attempts",
            "2",
            "--base-delay",
            "0",
            "--",
            "flyctl",
            "deploy",
            "--remote-only",
        ]
    )

    assert args.attempts == 2
    assert args.base_delay == 0
    assert args.command == ["flyctl", "deploy", "--remote-only"]


def test_phase_465_workflow_is_single_flight_and_records_provenance():
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(
        encoding="utf-8"
    )

    assert "'mlb-analytics-hub-production'" in workflow
    assert "format('quality-{0}', github.ref)" in workflow
    assert "cancel-in-progress: false" in workflow
    assert workflow.count("scripts/deploy_with_lease_retry.py") == 2
    assert "Record deployment provenance" in workflow
    assert "$GITHUB_STEP_SUMMARY" in workflow
    assert "Production smoke and readiness gate" in workflow


def test_phase_465_runbook_and_roadmap_capture_operating_contract():
    runbook = (ROOT / "docs" / "DEPLOYMENT_RUNBOOK.md").read_text(encoding="utf-8")
    roadmap = (ROOT / "docs" / "MLB_ANALYTICS_HUB_ROADMAP.md").read_text(
        encoding="utf-8"
    )

    assert "Merge to `Main` is the production deployment trigger." in runbook
    assert "Do not start a manual `flyctl deploy`" in runbook
    assert "only retries the known transient machine-lease collision" in runbook
    assert "Phase 4.65 is the active phase." in roadmap
    assert "Deployment single-flight and lease recovery" in roadmap
