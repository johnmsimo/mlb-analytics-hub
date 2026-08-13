"""Phase 4.61 model lineage and promotion gates.

The production scorer remains the source of truth for inference.  This module
only evaluates metadata and feature contracts, so a failed or incomplete model
cannot be promoted by accident.  Binary artifacts are deliberately not loaded
here; the weekly regeneration job and the normal review/merge workflow remain
the only path to production.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence


MODEL_OPERATIONS_VERSION = "4.61.0"
MIN_HELD_OUT_AUC = 0.53

MARKET_CONTRACTS: dict[str, dict[str, Any]] = {
    "hits": {"tracker_market": "batter_hits", "target": "hit_over_0.5", "line": 0.5},
    "hits_1.5": {"tracker_market": "batter_hits", "target": "hit_over_1.5", "line": 1.5},
    "tb": {"tracker_market": "total_bases", "target": "tb_over_1.5", "line": 1.5},
    "tb_2.5": {"tracker_market": "total_bases", "target": "tb_over_2.5", "line": 2.5},
    "tb_3.5": {"tracker_market": "total_bases", "target": "tb_over_3.5", "line": 3.5},
    "hr": {"tracker_market": "home_runs", "target": "hr_over_0.5", "line": 0.5},
    "rbi": {"tracker_market": "rbi", "target": "rbi_over_0.5", "line": 0.5},
    "rbi_1.5": {"tracker_market": "rbi", "target": "rbi_over_1.5", "line": 1.5},
    "k_2.5": {"tracker_market": "pitcher_strikeouts", "target": "k_over_2.5", "line": 2.5},
    "k_3.5": {"tracker_market": "pitcher_strikeouts", "target": "k_over_3.5", "line": 3.5},
    "k_4.5": {"tracker_market": "pitcher_strikeouts", "target": "k_over_4.5", "line": 4.5},
    "k_5.5": {"tracker_market": "pitcher_strikeouts", "target": "k_over_5.5", "line": 5.5},
    "k_6.5": {"tracker_market": "pitcher_strikeouts", "target": "k_over_6.5", "line": 6.5},
    "k_7.5": {"tracker_market": "pitcher_strikeouts", "target": "k_over_7.5", "line": 7.5},
}

FEATURE_ALIASES: dict[str, tuple[str, ...]] = {
    "hits": ("hits", "hits_over_0.5"),
    "hits_1.5": ("hits_1.5", "hits_over_1.5"),
    "tb": ("tb", "tb_over_1.5"),
    "tb_2.5": ("tb_2.5", "tb_over_2.5"),
    "tb_3.5": ("tb_3.5", "tb_over_3.5"),
    "hr": ("hr", "hr_over_0.5"),
    "rbi": ("rbi", "rbi_over_0.5"),
    "rbi_1.5": ("rbi_1.5", "rbi_over_1.5"),
    "k_2.5": ("k_2.5", "k_over_2.5"),
    "k_3.5": ("k_3.5", "k_over_3.5"),
    "k_4.5": ("k_4.5", "k_over_4.5"),
    "k_5.5": ("k_5.5", "k_over_5.5"),
    "k_6.5": ("k_6.5", "k_over_6.5"),
    "k_7.5": ("k_7.5", "k_over_7.5"),
}


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    reasons: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "reasons": list(self.reasons),
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class CandidateEvaluation:
    model_key: str
    gates: tuple[GateResult, ...]
    evaluated_at_utc: str
    version: str = MODEL_OPERATIONS_VERSION

    @property
    def passed(self) -> bool:
        return all(gate.passed for gate in self.gates)

    @property
    def status(self) -> str:
        return "passed" if self.passed else "blocked"

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_key": self.model_key,
            "version": self.version,
            "status": self.status,
            "passed": self.passed,
            "evaluated_at_utc": self.evaluated_at_utc,
            "gates": [gate.to_dict() for gate in self.gates],
        }


def _held_out_gate(metadata: Mapping[str, Any]) -> GateResult:
    auc = _finite_number(metadata.get("test_auc"))
    brier = _finite_number(metadata.get("test_brier"))
    baseline = _finite_number(metadata.get("baserate_brier"))
    n_test = _finite_number(metadata.get("n_test"))
    reasons: list[str] = []
    if auc is None or auc < MIN_HELD_OUT_AUC:
        reasons.append(f"held-out AUC must be >= {MIN_HELD_OUT_AUC}")
    if brier is None or baseline is None or not brier < baseline:
        reasons.append("held-out Brier must beat the base-rate Brier")
    if n_test is None or n_test < 1:
        reasons.append("held-out sample is missing")
    return GateResult(
        "held_out",
        not reasons,
        tuple(reasons),
        {"test_auc": auc, "test_brier": brier, "baserate_brier": baseline, "n_test": n_test},
    )


def _calibration_gate(metadata: Mapping[str, Any]) -> GateResult:
    calibration = str(metadata.get("calibration") or "").lower()
    model_type = str(metadata.get("model_type") or "").lower()
    passed = "isotonic" in calibration or "calibrat" in calibration
    passed = passed and "calibrat" in model_type
    reasons = () if passed else ("calibrated model metadata is required",)
    return GateResult(
        "calibration",
        passed,
        reasons,
        {"calibration": calibration or None, "model_type": model_type or None},
    )


def _feature_parity_gate(
    model_key: str,
    candidate_features: Sequence[str] | None,
    feature_map: Mapping[str, Sequence[str]],
) -> GateResult:
    aliases = FEATURE_ALIASES.get(model_key, (model_key,))
    expected = list(candidate_features or [])
    reasons: list[str] = []
    if not expected:
        reasons.append("candidate feature list is missing")
    alias_values: dict[str, list[str]] = {}
    for alias in aliases:
        if alias in feature_map:
            alias_values[alias] = list(feature_map[alias])
    if not alias_values:
        reasons.append("serve feature map is missing")
    for alias, values in alias_values.items():
        if values != expected:
            reasons.append(f"candidate features do not match serve alias {alias}")
    return GateResult(
        "serve_parity",
        not reasons,
        tuple(reasons),
        {"aliases": list(alias_values), "feature_count": len(expected)},
    )


def _market_validation_gate(model_key: str, metadata: Mapping[str, Any]) -> GateResult:
    contract = MARKET_CONTRACTS.get(model_key)
    reasons: list[str] = []
    if contract is None:
        reasons.append("market contract is unknown")
        contract = {}
    target = str(metadata.get("target") or "")
    if not target or target != contract.get("target"):
        reasons.append("target does not match the market contract")
    line = _finite_number(metadata.get("line"))
    if line is None or line != contract.get("line"):
        reasons.append("line does not match the market contract")
    tracker_market = str(metadata.get("tracker_market") or contract.get("tracker_market") or "")
    if tracker_market != contract.get("tracker_market"):
        reasons.append("tracker market does not match the market contract")
    return GateResult(
        "market_validation",
        not reasons,
        tuple(reasons),
        {"target": target or None, "line": line, "tracker_market": tracker_market or None},
    )


def evaluate_candidate(
    model_key: str,
    metadata: Mapping[str, Any],
    *,
    candidate_features: Sequence[str] | None,
    serve_feature_map: Mapping[str, Sequence[str]],
) -> CandidateEvaluation:
    """Evaluate every production gate for one candidate model."""

    gates = (
        _held_out_gate(metadata),
        _calibration_gate(metadata),
        _feature_parity_gate(model_key, candidate_features, serve_feature_map),
        _market_validation_gate(model_key, metadata),
    )
    return CandidateEvaluation(
        model_key=model_key,
        gates=gates,
        evaluated_at_utc=datetime.now(timezone.utc).isoformat(),
    )


def compare_challenger(
    champion_metadata: Mapping[str, Any],
    challenger_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a conservative promotion decision using held-out metrics."""

    champion_brier = _finite_number(champion_metadata.get("test_brier"))
    challenger_brier = _finite_number(challenger_metadata.get("test_brier"))
    champion_auc = _finite_number(champion_metadata.get("test_auc"))
    challenger_auc = _finite_number(challenger_metadata.get("test_auc"))
    reasons: list[str] = []
    if challenger_brier is None or champion_brier is None or challenger_brier > champion_brier:
        reasons.append("challenger Brier is not better than champion Brier")
    if challenger_auc is None or champion_auc is None or challenger_auc < champion_auc:
        reasons.append("challenger AUC is below champion AUC")
    return {
        "decision": "promote" if not reasons else "hold",
        "reasons": reasons,
        "champion": {"test_auc": champion_auc, "test_brier": champion_brier},
        "challenger": {"test_auc": challenger_auc, "test_brier": challenger_brier},
    }


class ModelRegistry:
    """Small metadata-only registry with explicit promotion and rollback."""

    def __init__(self, champions: Mapping[str, Any] | None = None, history: Sequence[Mapping[str, Any]] | None = None):
        self.champions = dict(champions or {})
        self.history = [dict(item) for item in (history or [])]

    @classmethod
    def load(cls, path: str | Path) -> "ModelRegistry":
        source = Path(path)
        if not source.exists():
            return cls()
        payload = json.loads(source.read_text(encoding="utf-8"))
        return cls(payload.get("champions"), payload.get("history"))

    def save(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps({"version": MODEL_OPERATIONS_VERSION, "champions": self.champions, "history": self.history}, indent=2) + "\n",
            encoding="utf-8",
        )

    def promote(self, evaluation: CandidateEvaluation, artifact_ref: str) -> dict[str, Any]:
        if not evaluation.passed:
            raise ValueError(f"cannot promote blocked candidate {evaluation.model_key}")
        previous = self.champions.get(evaluation.model_key)
        record = {"artifact_ref": artifact_ref, "evaluation": evaluation.to_dict()}
        self.champions[evaluation.model_key] = record
        self.history.append({"action": "promote", "model_key": evaluation.model_key, "previous": previous, "current": record})
        return record

    def rollback(self, model_key: str) -> dict[str, Any]:
        for item in reversed(self.history):
            if item.get("action") == "promote" and item.get("model_key") == model_key and item.get("previous"):
                self.champions[model_key] = item["previous"]
                self.history.append({"action": "rollback", "model_key": model_key, "restored": item["previous"]})
                return item["previous"]
        raise ValueError(f"no rollback target is recorded for {model_key}")

