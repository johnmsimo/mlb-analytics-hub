"""Phase 5.6 public, read-only verification ledger.

The ledger is deliberately derived from the immutable Phase 5.4 prediction
receipt.  It publishes only priced system recommendations and returns a strict
allowlist of public fields; Tracker settings, notes, dollar stakes, bankroll,
admin identity, and rejected row contents never cross this boundary.
"""

from __future__ import annotations

import json
import math
import os
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any

from continuous_learning import (
    CONTINUOUS_LEARNING_VERSION,
    build_prediction_receipt,
)


PUBLIC_VERIFICATION_VERSION = "5.6"
DEFAULT_WINDOW_DAYS = 90
MAX_WINDOW_DAYS = 366

# Legacy rows predate an explicit visibility flag.  Only model-owned sources
# qualify for that compatibility path; user/manual/draft sources always fail
# closed.  New writers may set publicRelease explicitly.
PUBLIC_SYSTEM_SOURCES = frozenset({
    "batx",
    "canonical_edge_finder",
    "edge_finder",
    "model",
    "model_pipeline",
    "recommendation_engine",
    "stacked",
    "xgb",
})
PRIVATE_SOURCE_MARKERS = (
    "admin",
    "draft",
    "manual",
    "my_hub",
    "private",
    "tracker",
    "user",
)
INVALID_BOOKS = frozenset({
    "",
    "model",
    "n/a",
    "na",
    "none",
    "projection",
    "research",
    "sim",
    "simulation",
    "unknown",
    "unpriced",
})


def _number(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _grade(row: Mapping[str, Any]) -> str:
    value = str(row.get("grade") or row.get("result") or "pending").strip().lower()
    aliases = {
        "won": "win", "w": "win", "hit": "win",
        "lost": "loss", "l": "loss", "miss": "loss",
        "void": "push", "cancelled": "push", "canceled": "push",
    }
    value = aliases.get(value, value)
    return value if value in {"pending", "win", "loss", "push"} else "pending"


def _public_source(row: Mapping[str, Any]) -> bool:
    if row.get("publicRelease") is False or row.get("private") is True:
        return False
    visibility = str(row.get("visibility") or "").strip().lower()
    if visibility in {"admin", "private", "user"}:
        return False
    source = str(row.get("source") or "").strip().lower().replace("-", "_")
    if not source or any(marker in source for marker in PRIVATE_SOURCE_MARKERS):
        return False
    if row.get("publicRelease") is True:
        return True
    return source in PUBLIC_SYSTEM_SOURCES


def _receipt_is_intact(row: Mapping[str, Any]) -> bool:
    receipt = row.get("learningReceipt")
    if not isinstance(receipt, Mapping):
        return False
    expected = build_prediction_receipt(row)
    return bool(
        receipt.get("version") == CONTINUOUS_LEARNING_VERSION
        and receipt.get("predictionFingerprint") == expected["predictionFingerprint"]
        and receipt.get("snapshot") == expected["snapshot"]
        and receipt.get("measurementEligible") is True
        and receipt.get("outcomeFieldsIncluded") is False
    )


def _priced(row: Mapping[str, Any]) -> bool:
    price = _number(row.get("openingPrice"))
    book = str(row.get("book") or "").strip().lower()
    return bool(
        price is not None
        and price != 0
        and abs(price) >= 100
        and book not in INVALID_BOOKS
    )


def _release_reason(row: Mapping[str, Any]) -> str | None:
    if not _public_source(row):
        return "private_or_unpublished"
    if not _receipt_is_intact(row):
        return "invalid_prediction_receipt"
    if not _priced(row):
        return "missing_verified_price"
    receipt = row.get("learningReceipt") or {}
    snapshot = receipt.get("snapshot") or {}
    if not str(row.get("player") or "").strip():
        return "missing_public_identity"
    if _time(snapshot.get("savedAt")) is None:
        return "missing_release_timestamp"
    return None


def _clv_fields(row: Mapping[str, Any]) -> tuple[int | None, float | None]:
    lineage = row.get("oddsLineage") if isinstance(row.get("oddsLineage"), Mapping) else {}
    eligible = row.get("clvEligible") is True or lineage.get("clvEligible") is True
    closing_price = _number(row.get("closingPrice"))
    clv_edge = _number(row.get("clvEdge"))
    if not eligible or closing_price is None or abs(closing_price) < 100:
        return None, None
    return int(closing_price), round(clv_edge, 6) if clv_edge is not None else None


def _public_row(row: Mapping[str, Any]) -> dict[str, Any]:
    receipt = row["learningReceipt"]
    snapshot = receipt["snapshot"]
    closing_price, clv_edge = _clv_fields(row)
    probability = _number(snapshot.get("servedProbability"))
    price = _number(row.get("openingPrice"))
    return {
        "publicId": str(receipt["predictionFingerprint"])[:16],
        "receiptFingerprint": receipt["predictionFingerprint"],
        "receiptVersion": receipt["version"],
        "receiptVerified": True,
        "releasedAt": snapshot["savedAt"],
        "gradedAt": str(row.get("gradedAt") or "") or None,
        "gamePk": snapshot.get("gamePk"),
        "player": str(row.get("player") or "").strip(),
        "marketKey": snapshot.get("marketKey"),
        "side": str(snapshot.get("side") or "").title(),
        "line": snapshot.get("line"),
        "probability": round(probability, 6) if probability is not None else None,
        "sportsbook": str(row.get("book") or "").strip(),
        "openingPrice": int(price) if price is not None else None,
        "closingPrice": closing_price,
        "clvEdge": clv_edge,
        "result": _grade(row),
    }


def _ece(rows: list[dict[str, Any]]) -> float | None:
    pairs = [(row["probability"], 1 if row["result"] == "win" else 0) for row in rows]
    if not pairs:
        return None
    bins: dict[int, list[tuple[float, int]]] = {}
    for probability, outcome in pairs:
        bins.setdefault(min(9, int(probability * 10)), []).append((probability, outcome))
    total = len(pairs)
    value = sum(
        len(values) / total * abs(
            sum(probability for probability, _ in values) / len(values)
            - sum(outcome for _, outcome in values) / len(values)
        )
        for values in bins.values()
    )
    return round(value, 6)


def _metrics(public_rows: list[dict[str, Any]], raw_by_id: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    counts = Counter(row["result"] for row in public_rows)
    decision_rows = [row for row in public_rows if row["result"] in {"win", "loss"}]
    clv_rows = [row for row in decision_rows if row["clvEdge"] is not None]
    brier = None
    if decision_rows:
        brier = round(sum(
            (row["probability"] - (1 if row["result"] == "win" else 0)) ** 2
            for row in decision_rows
        ) / len(decision_rows), 6)

    roi_pairs: list[tuple[float, float]] = []
    for row in decision_rows:
        raw = raw_by_id.get(row["receiptFingerprint"], {})
        risk = _number(raw.get("stakeUnits"))
        profit = _number(raw.get("profitUnits"))
        if risk is not None and risk > 0 and profit is not None:
            roi_pairs.append((risk, profit))
    units_risked = sum(risk for risk, _ in roi_pairs)
    profit_units = sum(profit for _, profit in roi_pairs)
    roi = round(profit_units / units_risked, 6) if units_risked else None

    return {
        "releasedCount": len(public_rows),
        "settledCount": len(decision_rows) + counts["push"],
        "gradedCount": len(decision_rows),
        "wins": counts["win"],
        "losses": counts["loss"],
        "pushes": counts["push"],
        "pending": counts["pending"],
        "winRate": round(counts["win"] / len(decision_rows), 6) if decision_rows else None,
        "brierScore": brier,
        "ece": _ece(decision_rows),
        "roiEligibleCount": len(roi_pairs),
        "unitsRisked": round(units_risked, 4) if roi_pairs else None,
        "profitUnits": round(profit_units, 4) if roi_pairs else None,
        "roi": roi,
        "clvGradedCount": len(clv_rows),
        "averageClvEdge": round(sum(row["clvEdge"] for row in clv_rows) / len(clv_rows), 6) if clv_rows else None,
        "beatCloseRate": round(sum(row["clvEdge"] > 0 for row in clv_rows) / len(clv_rows), 6) if clv_rows else None,
    }


def build_public_verification_ledger(
    entries: Iterable[Mapping[str, Any]],
    *,
    as_of: date | datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> dict[str, Any]:
    """Build the deterministic public ledger without mutating Tracker data."""
    if isinstance(as_of, datetime):
        anchor = as_of.astimezone(timezone.utc).date()
    else:
        anchor = as_of or datetime.now(timezone.utc).date()
    window = max(1, min(int(window_days), MAX_WINDOW_DAYS))
    cutoff = anchor - timedelta(days=window - 1)
    withheld: Counter[str] = Counter()
    public_rows: list[dict[str, Any]] = []
    raw_by_id: dict[str, Mapping[str, Any]] = {}
    seen: set[str] = set()

    for row in entries or ():
        if not isinstance(row, Mapping):
            withheld["malformed_row"] += 1
            continue
        reason = _release_reason(row)
        if reason:
            withheld[reason] += 1
            continue
        public = _public_row(row)
        released = _time(public["releasedAt"])
        if released is None or released.date() < cutoff or released.date() > anchor:
            withheld["outside_window"] += 1
            continue
        fingerprint = public["receiptFingerprint"]
        if fingerprint in seen:
            withheld["duplicate_receipt"] += 1
            continue
        seen.add(fingerprint)
        public_rows.append(public)
        raw_by_id[fingerprint] = row

    public_rows.sort(key=lambda row: (row["releasedAt"], row["publicId"]), reverse=True)
    return {
        "success": True,
        "version": PUBLIC_VERIFICATION_VERSION,
        "readOnly": True,
        "failClosed": True,
        "lossesOmitted": False,
        "privateTrackerFieldsIncluded": False,
        "window": {
            "days": window,
            "from": cutoff.isoformat(),
            "through": anchor.isoformat(),
        },
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "metrics": _metrics(public_rows, raw_by_id),
        "sampleDefinitions": {
            "graded": "Verified win/loss recommendations; pushes and pending rows excluded.",
            "clvGraded": "Graded recommendations with accepted closing-line lineage.",
            "roi": "Graded recommendations with both non-dollar unit risk and unit profit.",
        },
        "ledger": public_rows,
        "withheld": {
            "count": sum(withheld.values()),
            "reasonCounts": dict(sorted(withheld.items())),
            "rawRowsIncluded": False,
        },
    }


def load_tracker_entries(path: str) -> list[Mapping[str, Any]]:
    """Read day buckets from Tracker; unreadable storage produces an empty ledger."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    if not isinstance(payload, Mapping):
        return []
    rows: list[Mapping[str, Any]] = []
    for day in payload.values():
        if isinstance(day, Mapping):
            values = day.get("entries", [])
        elif isinstance(day, list):
            values = day
        else:
            continue
        if isinstance(values, list):
            rows.extend(value for value in values if isinstance(value, Mapping))
    return rows


def install_public_verification(app_module: Any) -> None:
    """Register the Phase 5.6 page and API exactly once per Flask worker."""
    flask_app = app_module.app
    if getattr(flask_app, "_phase_56_public_verification_installed", False):
        return
    from flask import Blueprint, jsonify, render_template, request

    blueprint = Blueprint("public_verification", __name__)

    @blueprint.get("/verification")
    def verification_page():
        return render_template("public_verification.html")

    @blueprint.get("/api/verification/ledger")
    def verification_api():
        try:
            window = int(request.args.get("window", DEFAULT_WINDOW_DAYS))
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "window must be an integer"}), 400
        requested_date = str(request.args.get("date") or "").strip()
        try:
            anchor = date.fromisoformat(requested_date) if requested_date else datetime.now(timezone.utc).date()
        except ValueError:
            return jsonify({"success": False, "error": "date must use YYYY-MM-DD"}), 400
        tracker_path = getattr(app_module, "TRACKER_STORE", None)
        if not tracker_path:
            tracker_path = os.path.join(os.path.dirname(__file__), "data", "daily_tracker.json")
        payload = build_public_verification_ledger(
            load_tracker_entries(tracker_path),
            as_of=anchor,
            window_days=window,
        )
        response = jsonify(payload)
        response.headers["Cache-Control"] = "public, max-age=30, stale-while-revalidate=60"
        response.headers["X-Verification-Contract"] = PUBLIC_VERIFICATION_VERSION
        return response

    flask_app.register_blueprint(blueprint)
    setattr(flask_app, "_phase_56_public_verification_installed", True)

