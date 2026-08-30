import copy
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import app as mlb_app
from canonical_consistency import normalize_candidate
from public_verification import (
    build_public_verification_ledger,
    select_public_release_entries,
)


NOW = datetime(2026, 8, 30, 15, 30, tzinfo=timezone.utc)


def actionable_candidate(*, index=1, probability=0.62, **changes):
    row = {
        "gamePk": 560,
        "gameStatus": "Scheduled",
        "gameAbstractState": "Preview",
        "gameStartIso": "2099-08-30T23:10:00+00:00",
        "player": f"Release Hitter {index}",
        "playerId": 56000 + index,
        "playerRole": "batter",
        "playerPosition": "CF",
        "lineupStatus": "confirmed",
        "team": "NYY",
        "marketKey": "batter_hits",
        "line": 0.5,
        "recommendedSide": "Over",
        "adjProb": probability,
        "bestAvailablePrice": -110,
        "bestAvailableBook": "Book A",
        "bestOverPrice": -110,
        "bestOverBook": "Book A",
        "bestUnderPrice": -105,
        "bestUnderBook": "Book B",
        "oddsUpdatedAt": NOW.isoformat(),
        "modelVersion": "hits-xgb-2026.08",
        "matchupSimulationVersion": "4.35",
        "gameSimN": 1500,
        "grade": "pending",
        "marketValidationVersion": "4.38",
        "calibrationStatus": "passed",
        "calibrationDriftStatus": "stable",
        "marketGateStatus": "promoted",
        "marketGatePromoted": True,
        "marketSideGateStatus": "promoted",
        "promotionStatus": "promoted",
    }
    row.update(changes)
    return row


def normalized_candidate(**changes):
    return normalize_candidate(
        actionable_candidate(**changes),
        surface="edge_lab",
        now=NOW,
    )


def test_release_selection_matches_daily_board_threshold_and_limit():
    candidates = [
        normalized_candidate(index=index, probability=0.55 + index / 100)
        for index in range(1, 11)
    ]
    below_threshold = normalized_candidate(index=20, probability=0.53)

    releases = select_public_release_entries(
        candidates + [below_threshold],
        released_at=NOW,
    )

    assert len(releases) == 8
    assert all(row["publicRelease"] is True for row in releases)
    assert all(row["visibility"] == "public" for row in releases)
    assert all(row["publicationReceipt"]["publicReleaseEligible"] is True for row in releases)
    assert all(row["learningReceipt"]["measurementEligible"] is True for row in releases)
    assert "Release Hitter 20" not in {row["player"] for row in releases}


def test_tampered_or_non_actionable_evidence_never_releases():
    tampered = normalized_candidate(index=1)
    tampered["evidenceReceipt"] = copy.deepcopy(tampered["evidenceReceipt"])
    tampered["evidenceReceipt"]["price"]["american"] = -125
    projection = normalized_candidate(index=2)
    projection["actionable"] = False
    projection["actionabilityStage"] = "Projected"

    assert select_public_release_entries(
        [tampered, projection], released_at=NOW
    ) == []


def test_release_enters_public_ledger_and_uses_fixed_public_risk_units():
    release = select_public_release_entries(
        [normalized_candidate(index=1)], released_at=NOW
    )[0]
    release.update({
        "grade": "win",
        "gradedAt": (NOW + timedelta(hours=8)).isoformat(),
        "profitUnits": 0.9091,
        "stakeUnits": 4.0,
    })

    payload = build_public_verification_ledger(
        [release], as_of=date(2026, 8, 30), window_days=1
    )

    assert payload["metrics"]["releasedCount"] == 1
    assert payload["metrics"]["gradedCount"] == 1
    assert payload["metrics"]["unitsRisked"] == 1.0
    assert payload["metrics"]["roi"] == 0.9091


def test_worker_publication_is_idempotent_and_preserves_private_collision(
    monkeypatch, tmp_path,
):
    tracker = tmp_path / "daily_tracker.json"
    private = {
        "id": "manual-private",
        "date": "2026-08-30",
        "gamePk": 560,
        "player": "Release Hitter 1",
        "playerId": 56001,
        "marketKey": "batter_hits",
        "line": 0.5,
        "recommendedSide": "Over",
        "source": "manual",
        "private": True,
        "notes": "never publish this",
        "grade": "pending",
    }
    tracker.write_text(json.dumps({
        "2026-08-30": {"entries": [private]},
    }), encoding="utf-8")
    monkeypatch.setattr(mlb_app, "TRACKER_STORE", str(tracker))

    candidate = actionable_candidate(
        index=1,
        oddsUpdatedAt=datetime.now(timezone.utc).isoformat(),
    )
    first = mlb_app._publish_public_recommendations(
        "2026-08-30", [candidate], released_at=datetime.now(timezone.utc).isoformat()
    )
    second = mlb_app._publish_public_recommendations(
        "2026-08-30", [candidate], released_at=datetime.now(timezone.utc).isoformat()
    )
    rows = json.loads(tracker.read_text(encoding="utf-8"))["2026-08-30"]["entries"]

    assert first["newReleaseCount"] == 1
    assert second["newReleaseCount"] == 0
    assert second["existingReleaseCount"] == 1
    assert len(rows) == 2
    assert rows[0]["source"] == "manual"
    assert rows[0]["notes"] == "never publish this"
    assert "publicationReceipt" not in rows[0]
    public = next(row for row in rows if row.get("publicRelease") is True)
    assert public["recordType"] == "public_recommendation_release"
    assert public["source"] == "recommendation_engine"


def test_automatic_grading_adds_row_level_timestamp(monkeypatch, tmp_path):
    tracker = tmp_path / "daily_tracker.json"
    tracker.write_text(json.dumps({
        "2026-08-30": {
            "entries": [{
                "id": "nrfi-release",
                "date": "2026-08-30",
                "gamePk": 560,
                "player": "NYY @ BOS",
                "marketKey": "nrfi",
                "line": 0.5,
                "recommendedSide": "Under",
                "openingPrice": -110,
                "adjProb": 0.60,
                "grade": "pending",
            }],
        },
    }), encoding="utf-8")
    schedule = [{
        "gamePk": 560,
        "status": {"detailedState": "Final"},
        "linescore": {
            "innings": [{"away": {"runs": 0}, "home": {"runs": 0}}],
        },
    }]
    monkeypatch.setattr(mlb_app, "TRACKER_STORE", str(tracker))
    monkeypatch.setattr(mlb_app, "fetch_schedule", lambda _date: schedule)
    monkeypatch.setattr(mlb_app, "_check_admin_auth", lambda: None)

    with mlb_app.app.test_request_context(
        "/api/tracker/grade/2026-08-30", method="POST"
    ):
        response = mlb_app.api_tracker_grade("2026-08-30")

    row = response.get_json()["entries"][0]
    assert row["grade"] == "win"
    assert row["gradedAt"]


def test_durable_worker_receipts_release_before_publishing_snapshot():
    root = Path(__file__).resolve().parents[1]
    worker = (root / "worker.py").read_text(encoding="utf-8")
    app_source = (root / "app.py").read_text(encoding="utf-8")

    publish = worker.index('app_module._publish_public_recommendations(')
    durable = worker.index('app_module._write_props_scan_durable_snapshot(')
    assert publish < durable
    assert 'payload["publicVerificationRelease"]' in worker
    assert "minimum_edge=0.03" in app_source
    assert "limit=8" in app_source
    assert "'privateTrackerFieldsIncluded': False" in app_source
    assert "'publicVerificationRelease': base.get('publicVerificationRelease')" in app_source
