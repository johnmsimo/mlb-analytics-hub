"""Minimal reproducible model registry and evaluation utilities."""
from __future__ import annotations
from datetime import datetime, timezone
from math import sqrt
from database import db
from db_models import ModelVersion, Prediction


def register_model(key, version, target, algorithm, feature_schema=None, metrics=None, artifact_uri=None, activate=False):
    model = db.session.scalar(db.select(ModelVersion).where(ModelVersion.key == key, ModelVersion.version == version))
    if not model:
        model = ModelVersion(key=key, version=version, target=target, algorithm=algorithm)
        db.session.add(model)
    model.feature_schema = feature_schema or {}
    model.metrics = metrics or {}
    model.artifact_uri = artifact_uri
    if activate:
        for old in db.session.scalars(db.select(ModelVersion).where(ModelVersion.key == key)).all():
            old.active = False
        model.active = True
    db.session.commit()
    return model


def evaluate_predictions(model_id: int) -> dict:
    rows = db.session.scalars(db.select(Prediction).where(
        Prediction.model_version_id == model_id, Prediction.actual_value.is_not(None))).all()
    if not rows:
        return {"count": 0, "mae": None, "rmse": None, "brier": None}
    errors = [(r.predicted_value or 0) - r.actual_value for r in rows if r.predicted_value is not None]
    probs = [(r.probability, r.actual_value) for r in rows if r.probability is not None and r.actual_value in (0, 1)]
    result = {
        "count": len(rows),
        "mae": sum(abs(x) for x in errors) / len(errors) if errors else None,
        "rmse": sqrt(sum(x*x for x in errors) / len(errors)) if errors else None,
        "brier": sum((p-y)**2 for p, y in probs) / len(probs) if probs else None,
    }
    model = db.session.get(ModelVersion, model_id)
    if model:
        model.metrics = {**(model.metrics or {}), **result}
        model.updated_at = datetime.now(timezone.utc)
        db.session.commit()
    return result
