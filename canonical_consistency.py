"""Canonical cross-page candidate contract for MLB Analytics Hub.

Every recommendation-capable surface can carry a different presentation, but
the underlying player/market/line decision must be represented identically.
This module normalizes the existing validated candidate contract and adds a
small consistency audit without making research rows look actionable.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Any, Iterable, Mapping

from actionability import evaluate_actionability
from candidate_integrity import evaluate_candidate


CANONICAL_CONTRACT_VERSION = "4.57"
CANONICAL_FIELDS = (
    "canonicalCandidateId",
    "playerId",
    "player",
    "team",
    "gamePk",
    "canonicalMarketKey",
    "canonicalSide",
    "line",
    "canonicalProbability",
    "canonicalEdge",
    "canonicalPrice",
    "canonicalBook",
    "integrityStatus",
    "actionabilityStage",
    "actionable",
    "recommendationGrade",
)
_ROW_KEYS = (
    "props",
    "edges",
    "topProps",
    "entries",
    "picks",
    "candidates",
    "rows",
)


def _number(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _probability(row: Mapping[str, Any]) -> float | None:
    for key in (
        "canonicalProbability",
        "probability",
        "blendedProb",
        "adjProb",
        "winProb",
        "modelProbability",
        "modelProbabilityPct",
    ):
        number = _number(row.get(key))
        if number is None:
            continue
        if number > 1.0:
            number /= 100.0
        if 0.0 < number < 1.0:
            return round(number, 6)
    return None


def _edge(row: Mapping[str, Any]) -> float | None:
    for key in (
        "canonicalEdge",
        "edge",
        "estimatedEdgePct",
        "edgePct",
        "evPct",
    ):
        number = _number(row.get(key))
        if number is None:
            continue
        if abs(number) > 1.0:
            number /= 100.0
        return round(number, 6)
    return None


def _price(row: Mapping[str, Any]) -> float | None:
    for key in (
        "canonicalPrice",
        "price",
        "bestAvailablePrice",
        "marketPrice",
        "bestOverPrice",
        "best_over_price",
        "bestUnderPrice",
        "best_under_price",
    ):
        number = _number(row.get(key))
        if number is not None and number != 0:
            return int(number) if number.is_integer() else number
    return None


def _book(row: Mapping[str, Any]) -> str | None:
    # A book name without a quoted price is not usable betting evidence.
    if _price(row) is None:
        return None
    for key in (
        "canonicalBook",
        "book",
        "bestAvailableBook",
        "bestBook",
        "bestOverBook",
        "best_over_book",
        "bestUnderBook",
        "best_under_book",
        "bookmaker",
    ):
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _stable_identity(row: Mapping[str, Any]) -> str:
    identity = "|".join(
        str(row.get(key) or "").strip().lower()
        for key in (
            "gamePk",
            "playerId",
            "player",
            "team",
            "canonicalMarketKey",
            "canonicalSide",
            "line",
        )
    )
    return "canonical:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _recommendation_grade(row: Mapping[str, Any]) -> str:
    value = (
        row.get("recommendationGrade")
        or row.get("gradeLabel")
        or row.get("confidenceTier")
        or "Research"
    )
    return str(value).strip() or "Research"


def _snapshot(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in CANONICAL_FIELDS
    }


def _fingerprint(snapshot: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(snapshot), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def normalize_candidate(
    source: Mapping[str, Any],
    *,
    surface: str = "unknown",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return one candidate with one canonical identity and decision snapshot."""
    row = dict(source)
    try:
        evaluated = evaluate_candidate(row, now=now)
    except Exception:
        # A research surface must remain available if an optional producer has
        # malformed metadata; it is still explicitly non-actionable.
        evaluated = row
        evaluated.setdefault("integrityStatus", "rejected")
        evaluated.setdefault("integrityReasons", ["candidate normalization failed"])
        evaluated["actionable"] = False

    actionability = evaluate_actionability(
        evaluated,
        require_market_validation=(
            "marketGatePromoted" in evaluated
            or "promotionStatus" in evaluated
        ),
    )
    evaluated.update(actionability)

    evaluated["canonicalContractVersion"] = CANONICAL_CONTRACT_VERSION
    evaluated["canonicalSurface"] = surface
    evaluated["canonicalCandidateId"] = (
        evaluated.get("canonicalCandidateId") or _stable_identity(evaluated)
    )
    evaluated["canonicalProbability"] = _probability(evaluated)
    evaluated["canonicalEdge"] = _edge(evaluated)
    evaluated["canonicalPrice"] = _price(evaluated)
    evaluated["canonicalBook"] = _book(evaluated)
    evaluated["canonicalMarketKey"] = (
        evaluated.get("canonicalMarketKey")
        or evaluated.get("marketKey")
        or evaluated.get("market")
    )
    evaluated["canonicalSide"] = (
        evaluated.get("canonicalSide")
        or evaluated.get("recommendedSide")
        or evaluated.get("side")
        or "Over"
    )
    evaluated["recommendationGrade"] = _recommendation_grade(evaluated)
    evaluated["canonicalSnapshot"] = _snapshot(evaluated)
    evaluated["canonicalFingerprint"] = _fingerprint(
        evaluated["canonicalSnapshot"]
    )
    return evaluated


def normalize_rows(
    sources: Iterable[Mapping[str, Any]],
    *,
    surface: str = "unknown",
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Normalize and de-duplicate rows while preserving the strongest record."""
    normalized = [
        normalize_candidate(source, surface=surface, now=now)
        for source in sources
        if isinstance(source, Mapping)
    ]
    by_id: dict[str, dict[str, Any]] = {}
    duplicate_count = 0
    for row in normalized:
        identity = str(row["canonicalCandidateId"])
        current = by_id.get(identity)
        if current is None:
            by_id[identity] = row
            continue
        duplicate_count += 1
        current_score = (
            bool(current.get("actionable")),
            _number(current.get("canonicalEdge")) or -1.0,
            _number(current.get("canonicalProbability")) or -1.0,
        )
        row_score = (
            bool(row.get("actionable")),
            _number(row.get("canonicalEdge")) or -1.0,
            _number(row.get("canonicalProbability")) or -1.0,
        )
        if row_score > current_score:
            by_id[identity] = row

    rows = list(by_id.values())
    stage_counts = Counter(
        str(row.get("actionabilityStage") or "Research")
        for row in rows
    )
    audit = {
        "version": CANONICAL_CONTRACT_VERSION,
        "surface": surface,
        "sourceCount": len(normalized),
        "uniqueCount": len(rows),
        "duplicateCount": duplicate_count,
        "actionableCount": sum(bool(row.get("actionable")) for row in rows),
        "stageCounts": dict(sorted(stage_counts.items())),
        "contractFields": list(CANONICAL_FIELDS),
    }
    return rows, audit


def normalize_payload(
    payload: Mapping[str, Any],
    *,
    surface: str,
    row_keys: Iterable[str] = _ROW_KEYS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Normalize known row collections in a producer payload."""
    result = dict(payload)
    audits: dict[str, dict[str, Any]] = {}
    all_rows: list[dict[str, Any]] = []
    for key in row_keys:
        value = result.get(key)
        if not isinstance(value, list):
            continue
        rows, audit = normalize_rows(value, surface=surface, now=now)
        result[key] = rows
        audits[key] = audit
        all_rows.extend(rows)
    result["canonicalContractVersion"] = CANONICAL_CONTRACT_VERSION
    result["canonicalSurface"] = surface
    result["canonicalCandidateAudit"] = {
        "version": CANONICAL_CONTRACT_VERSION,
        "surface": surface,
        "collections": audits,
        "rowCount": len(all_rows),
        "actionableCount": sum(bool(row.get("actionable")) for row in all_rows),
    }
    return result


def consistency_audit(
    surfaces: Mapping[str, Iterable[Mapping[str, Any]]],
) -> dict[str, Any]:
    """Compare canonical decision fields for identical candidates across surfaces."""
    by_id: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for surface, rows in surfaces.items():
        normalized, _ = normalize_rows(rows, surface=surface)
        for row in normalized:
            by_id[str(row["canonicalCandidateId"])].append(
                (surface, row["canonicalSnapshot"])
            )

    mismatches = []
    compared = 0
    for candidate_id, records in sorted(by_id.items()):
        if len({surface for surface, _ in records}) < 2:
            continue
        compared += 1
        fingerprints = {
            _fingerprint(snapshot)
            for _, snapshot in records
        }
        if len(fingerprints) <= 1:
            continue
        mismatches.append({
            "canonicalCandidateId": candidate_id,
            "surfaces": {
                surface: snapshot
                for surface, snapshot in records
            },
            "fields": [
                field
                for field in CANONICAL_FIELDS
                if len({
                    snapshot.get(field)
                    for _, snapshot in records
                }) > 1
            ],
        })

    return {
        "version": CANONICAL_CONTRACT_VERSION,
        "surfaceCount": len(surfaces),
        "comparedCandidateCount": compared,
        "mismatchCount": len(mismatches),
        "consistent": not mismatches,
        "mismatches": mismatches,
    }


def _wrap_payload_function(
    app_module: Any,
    function_name: str,
    *,
    surface: str,
    row_keys: Iterable[str],
) -> None:
    original = getattr(app_module, function_name, None)
    if not callable(original) or getattr(original, "_canonical_consistency_wrapped", False):
        return

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        value = original(*args, **kwargs)
        if isinstance(value, Mapping):
            return normalize_payload(
                value,
                surface=surface,
                row_keys=row_keys,
            )
        if isinstance(value, list):
            rows, _ = normalize_rows(value, surface=surface)
            return rows
        return value

    wrapped._canonical_consistency_wrapped = True
    wrapped._canonical_consistency_original = original
    setattr(app_module, function_name, wrapped)


def install_canonical_consistency(app_module: Any) -> None:
    """Install normalization at the shared producer boundary once per process."""
    if getattr(app_module, "_canonical_consistency_installed", False):
        return

    _wrap_payload_function(
        app_module,
        "_props_scan_today_payload",
        surface="props",
        row_keys=("props", "rows", "candidates"),
    )
    _wrap_payload_function(
        app_module,
        "_edge_finder_payload",
        surface="edge_lab",
        row_keys=("edges", "rows", "candidates"),
    )
    _wrap_payload_function(
        app_module,
        "_mc_board_payload",
        surface="monte_carlo",
        row_keys=("topProps", "props", "rows", "candidates"),
    )
    _wrap_payload_function(
        app_module,
        "_tracker_today_payload",
        surface="tracker",
        row_keys=("entries", "picks", "rows"),
    )
    _wrap_payload_function(
        app_module,
        "_build_tracker_rows_for_game",
        surface="game_research",
        row_keys=(),
    )
    app_module._canonical_consistency_installed = True


_RESPONSE_CONTRACTS = (
    (
        "/api/props/projections",
        "props",
        ("projections", "props", "batters", "pitchers", "rows", "candidates"),
    ),
    (
        "/api/props/scan/today",
        "props",
        ("props", "rows", "candidates"),
    ),
    (
        "/api/edges/today",
        "edge_lab",
        ("edges", "props", "rows", "candidates"),
    ),
    (
        "/api/projections/monte-carlo",
        "monte_carlo",
        ("topProps", "props", "rows", "candidates"),
    ),
    (
        "/api/cheatsheets/today",
        "cheatsheets",
        ("rows", "props", "candidates", "batters"),
    ),
    (
        "/api/cheatsheet",
        "cheatsheets",
        ("rows", "props", "candidates", "batters"),
    ),
    (
        "/api/tracker",
        "tracker",
        ("entries", "picks", "rows", "candidates"),
    ),
    (
        "/api/deepdive",
        "deep_dive",
        ("props", "projections", "rows", "candidates"),
    ),
    (
        "/api/gameside",
        "gameside",
        ("props", "projections", "rows", "candidates"),
    ),
)


def install_canonical_response_hook(app_module: Any) -> None:
    """Normalize JSON responses used by recommendation-capable pages."""
    flask_app = app_module.app
    if getattr(app_module, "_canonical_response_hook_installed", False):
        return

    @flask_app.after_request
    def _canonical_response_hook(response: Any) -> Any:
        path = str(getattr(app_module.request, "path", "") or "")
        contract = next(
            (
                item
                for item in _RESPONSE_CONTRACTS
                if path.startswith(item[0])
            ),
            None,
        )
        if contract is None or "json" not in str(
            response.headers.get("Content-Type") or ""
        ).lower():
            return response
        payload = response.get_json(silent=True)
        if not isinstance(payload, Mapping):
            return response
        _, surface, row_keys = contract
        normalized = normalize_payload(payload, surface=surface, row_keys=row_keys)
        response.set_data(app_module.json.dumps(normalized, default=str))
        response.headers["Content-Length"] = str(len(response.get_data()))
        return response

    app_module._canonical_response_hook_installed = True


def install_canonical_consistency_api(app_module: Any) -> None:
    """Expose an inspectable cross-page contract endpoint for UI and QA."""
    flask_app = app_module.app
    if "api_canonical_candidates" in flask_app.view_functions:
        return

    install_canonical_consistency(app_module)
    install_canonical_response_hook(app_module)

    @flask_app.route("/api/candidates/canonical", methods=["GET"])
    def api_canonical_candidates():
        date_str = app_module.request.args.get("date")
        surface_filter = (
            app_module.request.args.get("surface") or "all"
        ).strip().lower()
        payloads: dict[str, dict[str, Any]] = {}
        calls = (
            ("props", "_props_scan_today_payload", (date_str,), {}),
            ("edge_lab", "_edge_finder_payload", (date_str,), {"min_edge": 0.0}),
            ("monte_carlo", "_mc_board_payload", (date_str,), {}),
            ("tracker", "_tracker_today_payload", (date_str,), {}),
        )
        for surface, function_name, args, kwargs in calls:
            if surface_filter not in {"all", surface}:
                continue
            function = getattr(app_module, function_name, None)
            if not callable(function):
                continue
            try:
                payloads[surface] = normalize_payload(
                    function(*args, **kwargs),
                    surface=surface,
                )
            except TypeError:
                try:
                    payloads[surface] = normalize_payload(
                        function(*args),
                        surface=surface,
                    )
                except Exception:
                    payloads[surface] = {
                        "canonicalContractVersion": CANONICAL_CONTRACT_VERSION,
                        "canonicalSurface": surface,
                        "canonicalCandidateAudit": {
                            "version": CANONICAL_CONTRACT_VERSION,
                            "surface": surface,
                            "state": "unavailable",
                        },
                    }
            except Exception:
                payloads[surface] = {
                    "canonicalContractVersion": CANONICAL_CONTRACT_VERSION,
                    "canonicalSurface": surface,
                    "canonicalCandidateAudit": {
                        "version": CANONICAL_CONTRACT_VERSION,
                        "surface": surface,
                        "state": "unavailable",
                    },
                }

        surface_rows = {
            surface: [
                row
                for key in _ROW_KEYS
                for row in (payload.get(key) or [])
                if isinstance(row, Mapping)
            ]
            for surface, payload in payloads.items()
        }
        audit = consistency_audit(surface_rows)
        return app_module.jsonify({
            "success": True,
            "contractVersion": CANONICAL_CONTRACT_VERSION,
            "date": date_str,
            "surface": surface_filter,
            "surfaces": payloads,
            "consistencyAudit": audit,
        })
