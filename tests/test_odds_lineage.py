from closing_line_integrity import accept_closing_capture
from odds_lineage import (
    MINIMUM_CLV_CLAIM_OBSERVATIONS,
    ODDS_LINEAGE_VERSION,
    build_odds_lineage,
    clv_summary,
    normalize_odds_snapshot,
)


def _lineage(**overrides):
    values = {
        "opening": {
            "price": -120, "impliedProbability": 0.52, "line": 0.5,
            "book": "DraftKings", "capturedAt": "2026-08-09T15:00:00Z",
            "source": "odds_api_live",
        },
        "current": {
            "price": -125, "impliedProbability": 0.5556, "line": 0.5,
            "book": "DraftKings", "capturedAt": "2026-08-09T18:30:00Z",
            "source": "odds_api_live",
        },
        "closing": {
            "price": -135, "impliedProbability": 0.5745, "line": 0.5,
            "book": "DraftKings", "capturedAt": "2026-08-09T18:55:00Z",
            "source": "odds_api_live",
        },
    }
    values.update(overrides)
    receipt = accept_closing_capture(
        opening={"capturedAt": values["opening"]["capturedAt"]},
        closing={
            "capturedAt": values["closing"]["capturedAt"],
            "price": values["closing"]["price"],
            "book": values["closing"]["book"],
            "source": values["closing"]["source"],
        },
        first_pitch="2026-08-09T19:00:00Z",
    )
    return build_odds_lineage(
        line=0.5, closing_integrity=receipt, **values,
    )


def test_snapshot_requires_identity_and_provenance_fields():
    snapshot = normalize_odds_snapshot(
        {"price": -110, "book": "Book", "capturedAt": "2026-08-09T15:00:00Z"},
        role="opening",
        line=0.5,
    )
    assert snapshot["valid"] is False
    assert "source" in snapshot["missing"]


def test_current_snapshot_exposes_freshness_state():
    snapshot = normalize_odds_snapshot(
        {
            "price": -110, "line": 0.5, "book": "Book",
            "capturedAt": "2026-08-09T18:59:00Z", "source": "live",
        },
        role="current",
        reference_at="2026-08-09T19:00:00Z",
    )
    assert snapshot["valid"] is True
    assert snapshot["freshness"]["status"] == "fresh"
    assert snapshot["freshness"]["ageSeconds"] == 60


def test_lineage_requires_verified_close_before_clv_is_eligible():
    lineage = _lineage()
    assert lineage["version"] == ODDS_LINEAGE_VERSION
    assert lineage["clvEligible"] is True
    assert lineage["clvStatus"] == "verified"
    rejected = _lineage(
        closing_integrity={"accepted": False, "fresh": False, "reason": "bad_close"}
    )
    assert rejected["clvEligible"] is False
    assert rejected["clvStatus"] == "rejected"


def test_clv_summary_separates_graded_and_clv_graded_denominators():
    row = {
        "grade": "win",
        "clvEdge": 0.04,
        "oddsLineage": _lineage(),
    }
    unverified = {"grade": "win", "clvEdge": 0.30}
    summary = clv_summary([row, unverified])
    assert summary["gradedCount"] == 2
    assert summary["clvEligibleCount"] == 1
    assert summary["clvGradedCount"] == 1
    assert summary["clvDenominator"] == "clvGradedCount"
    assert summary["claimStatus"] == "insufficient_sample"
    assert summary["minimumClaimObservations"] == MINIMUM_CLV_CLAIM_OBSERVATIONS
