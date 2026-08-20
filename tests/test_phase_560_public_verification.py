import json
from datetime import date
from types import SimpleNamespace

from flask import Flask

from continuous_learning import build_prediction_receipt
from public_verification import (
    PUBLIC_VERIFICATION_VERSION,
    build_publication_receipt,
    build_public_verification_ledger,
    install_public_verification,
)


def recommendation(*, identity, grade="pending", source="xgb", **changes):
    row = {
        "id": identity,
        "savedAt": "2026-08-19T16:00:00+00:00",
        "gradedAt": None,
        "source": source,
        "gamePk": 777,
        "player": "Verified Player",
        "marketKey": "batter_hits",
        "recommendedSide": "Over",
        "line": 0.5,
        "adjProb": 0.60,
        "openingImplied": 0.52,
        "openingPrice": -110,
        "book": "Book A",
        "grade": grade,
        "stakeUnits": 1.0,
        "profitUnits": None,
        "notes": "private note",
        "stakeDollars": 100,
        "bankroll": 5000,
    }
    row.update(changes)
    row["learningReceipt"] = build_prediction_receipt(row)
    publication = build_publication_receipt(row)
    if publication["publicReleaseEligible"]:
        row["publicationReceipt"] = publication
    return row


def test_ledger_keeps_every_result_and_separates_denominators():
    win = recommendation(
        identity="win",
        grade="win",
        gradedAt="2026-08-20T02:00:00+00:00",
        profitUnits=0.91,
        closingPrice=-125,
        closingImplied=0.5556,
        clvEdge=0.0356,
        clvEligible=True,
    )
    loss = recommendation(
        identity="loss",
        grade="loss",
        gradedAt="2026-08-20T02:05:00+00:00",
        profitUnits=-1.0,
    )
    push = recommendation(identity="push", grade="push")
    pending = recommendation(identity="pending")

    payload = build_public_verification_ledger(
        [win, loss, push, pending], as_of=date(2026, 8, 20), window_days=30
    )

    assert payload["version"] == PUBLIC_VERIFICATION_VERSION
    assert payload["lossesOmitted"] is False
    assert {row["result"] for row in payload["ledger"]} == {
        "win", "loss", "push", "pending"
    }
    metrics = payload["metrics"]
    assert metrics["releasedCount"] == 4
    assert metrics["settledCount"] == 3
    assert metrics["gradedCount"] == 2
    assert metrics["wins"] == metrics["losses"] == 1
    assert metrics["clvGradedCount"] == 1
    assert metrics["roiEligibleCount"] == 2
    assert metrics["roi"] == -0.045
    assert metrics["brierScore"] == 0.26
    assert metrics["ece"] == 0.1


def test_ledger_fails_closed_without_exposing_rejected_or_private_data():
    valid = recommendation(identity="public")
    manual = recommendation(identity="manual", source="manual")
    draft = recommendation(identity="draft", source="my_hub_verified_decision_draft")
    unpriced = recommendation(identity="unpriced", openingPrice=None)
    tampered = recommendation(identity="tampered")
    tampered["line"] = 1.5
    price_tampered = recommendation(identity="price-tampered")
    price_tampered["openingPrice"] = -125

    payload = build_public_verification_ledger(
        [valid, manual, draft, unpriced, tampered, price_tampered],
        as_of=date(2026, 8, 20),
        window_days=30,
    )

    assert [row["publicId"] for row in payload["ledger"]] == [
        valid["publicationReceipt"]["releaseFingerprint"][:16]
    ]
    encoded = json.dumps(payload)
    assert "private note" not in encoded
    assert "stakeDollars" not in encoded
    assert "bankroll" not in encoded
    assert payload["withheld"]["rawRowsIncluded"] is False
    assert payload["withheld"]["reasonCounts"] == {
        "invalid_prediction_receipt": 1,
        "invalid_publication_receipt": 1,
        "missing_verified_price": 1,
        "private_or_unpublished": 2,
    }


def test_explicit_public_release_still_requires_integrity_and_price():
    released = recommendation(identity="released", source="new_engine", publicRelease=True)
    private = recommendation(identity="private", source="xgb", publicRelease=False)
    payload = build_public_verification_ledger(
        [released, private], as_of=date(2026, 8, 20), window_days=30
    )
    assert len(payload["ledger"]) == 1
    assert payload["ledger"][0]["player"] == "Verified Player"


def test_flask_installer_serves_mobile_page_and_read_only_api(tmp_path):
    tracker = tmp_path / "daily_tracker.json"
    tracker.write_text(
        json.dumps({"2026-08-19": {"entries": [recommendation(identity="api")]}}),
        encoding="utf-8",
    )
    app = Flask(__name__, template_folder=str(tmp_path))
    (tmp_path / "public_verification.html").write_text(
        '<!doctype html><meta name="viewport" content="width=device-width">Public Verification Ledger',
        encoding="utf-8",
    )
    module = SimpleNamespace(app=app, TRACKER_STORE=str(tracker))
    install_public_verification(module)
    install_public_verification(module)

    with app.test_client() as client:
        page = client.get("/verification")
        response = client.get("/api/verification/ledger?date=2026-08-20&window=30")
        invalid = client.get("/api/verification/ledger?window=nope")
        mutation = client.post("/api/verification/ledger")

    assert page.status_code == 200
    assert response.status_code == 200
    assert response.headers["X-Verification-Contract"] == "5.6"
    assert response.get_json()["metrics"]["releasedCount"] == 1
    assert invalid.status_code == 400
    assert mutation.status_code == 405
