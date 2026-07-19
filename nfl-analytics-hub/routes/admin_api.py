"""Operational admin APIs for warehouse, jobs, models, and freshness."""
from __future__ import annotations
from datetime import datetime, timezone
from flask import Blueprint, current_app, jsonify, request, session
from sqlalchemy import func
from database import db
from db_models import (AuditLog, DataQualityIssue, DataSource, DataSyncRun, Game,
    ModelVersion, Play, Prediction, ScheduledJob, TeamAdvancedSeasonStat, InjuryReport, DepthChartEntry, SnapCount, WeatherObservation, OddsSnapshot, LeagueTransaction, Coach)
from model_registry import evaluate_predictions
from play_by_play import rebuild_advanced_team_stats
from scheduled_jobs import JOBS, run_job

admin_bp = Blueprint("admin_api", __name__, url_prefix="/api/admin")


def _audit(action, entity_type=None, entity_id=None, details=None):
    db.session.add(AuditLog(actor=(session.get("user") or {}).get("username"), action=action,
        entity_type=entity_type, entity_id=str(entity_id) if entity_id is not None else None,
        details=details, ip_address=request.remote_addr))
    db.session.commit()


@admin_bp.get("/overview")
def overview():
    latest_sync = db.session.scalar(db.select(DataSyncRun).order_by(DataSyncRun.id.desc()).limit(1))
    latest_game = db.session.scalar(db.select(func.max(Game.kickoff_at)))
    latest_play = db.session.scalar(db.select(func.max(Play.updated_at)))
    return jsonify({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inventory": {
            "games": db.session.scalar(db.select(func.count()).select_from(Game)),
            "plays": db.session.scalar(db.select(func.count()).select_from(Play)),
            "advanced_team_rows": db.session.scalar(db.select(func.count()).select_from(TeamAdvancedSeasonStat)),
            "models": db.session.scalar(db.select(func.count()).select_from(ModelVersion)),
            "predictions": db.session.scalar(db.select(func.count()).select_from(Prediction)),
            "open_quality_issues": db.session.scalar(db.select(func.count()).select_from(DataQualityIssue).where(DataQualityIssue.resolved.is_(False))),
            "injury_reports": db.session.scalar(db.select(func.count()).select_from(InjuryReport)),
            "depth_chart_entries": db.session.scalar(db.select(func.count()).select_from(DepthChartEntry)),
            "snap_counts": db.session.scalar(db.select(func.count()).select_from(SnapCount)),
            "weather_observations": db.session.scalar(db.select(func.count()).select_from(WeatherObservation)),
            "odds_snapshots": db.session.scalar(db.select(func.count()).select_from(OddsSnapshot)),
            "transactions": db.session.scalar(db.select(func.count()).select_from(LeagueTransaction)),
            "coaches": db.session.scalar(db.select(func.count()).select_from(Coach)),
        },
        "freshness": {
            "latest_game_at": latest_game.isoformat() if latest_game else None,
            "latest_play_update": latest_play.isoformat() if latest_play else None,
            "latest_sync": None if not latest_sync else {"source": latest_sync.source, "status": latest_sync.status,
                "finished_at": latest_sync.finished_at.isoformat() if latest_sync.finished_at else None},
        },
        "sources": [{"key": x.key, "enabled": x.enabled,
            "last_success_at": x.last_success_at.isoformat() if x.last_success_at else None,
            "last_failure_at": x.last_failure_at.isoformat() if x.last_failure_at else None}
            for x in db.session.scalars(db.select(DataSource).order_by(DataSource.key)).all()],
        "jobs": [{"key": x.key, "name": x.name, "cron": x.cron, "enabled": x.enabled,
            "last_status": x.last_status, "last_finished_at": x.last_finished_at.isoformat() if x.last_finished_at else None,
            "last_error": x.last_error} for x in db.session.scalars(db.select(ScheduledJob).order_by(ScheduledJob.key)).all()],
    })


@admin_bp.get("/jobs")
def jobs():
    rows = db.session.scalars(db.select(ScheduledJob).order_by(ScheduledJob.key)).all()
    return jsonify({"jobs": [{"id": x.id, "key": x.key, "name": x.name, "cron": x.cron,
        "enabled": x.enabled, "last_status": x.last_status,
        "last_started_at": x.last_started_at.isoformat() if x.last_started_at else None,
        "last_finished_at": x.last_finished_at.isoformat() if x.last_finished_at else None,
        "last_error": x.last_error} for x in rows]})


@admin_bp.post("/jobs/<key>/run")
def run_job_now(key):
    if key not in JOBS:
        return jsonify({"error": "job_not_found"}), 404
    run_job(current_app._get_current_object(), key)
    _audit("job.run", "scheduled_job", key)
    return jsonify({"ok": True, "job": key}), 202


@admin_bp.post("/advanced-analytics/rebuild")
def rebuild_advanced():
    season = request.args.get("season", type=int)
    result = rebuild_advanced_team_stats(season)
    _audit("advanced_analytics.rebuild", "season", season, result)
    return jsonify(result), 201


@admin_bp.get("/models")
def models():
    rows = db.session.scalars(db.select(ModelVersion).order_by(ModelVersion.key, ModelVersion.created_at.desc())).all()
    return jsonify({"models": [{"id": x.id, "key": x.key, "version": x.version, "target": x.target,
        "algorithm": x.algorithm, "active": x.active, "metrics": x.metrics,
        "artifact_uri": x.artifact_uri, "created_at": x.created_at.isoformat()} for x in rows]})


@admin_bp.post("/models/<int:model_id>/evaluate")
def evaluate_model(model_id):
    if not db.session.get(ModelVersion, model_id):
        return jsonify({"error": "model_not_found"}), 404
    result = evaluate_predictions(model_id)
    _audit("model.evaluate", "model", model_id, result)
    return jsonify(result), 201


@admin_bp.post("/external-sync")
def external_sync():
    from external_providers import sync_external
    season = request.args.get("season", type=int)
    if not season:
        return jsonify({"error": "season_required"}), 400
    datasets = [x.strip() for x in request.args.get("datasets", "pbp,rosters,injuries,depth_charts,snap_counts").split(",") if x.strip()]
    allowed = {"pbp", "rosters", "injuries", "depth_charts", "snap_counts"}
    if not datasets or any(x not in allowed for x in datasets):
        return jsonify({"error": "invalid_datasets", "allowed": sorted(allowed)}), 400
    result = sync_external(season, datasets)
    if "pbp" in datasets:
        result["advanced_analytics"] = rebuild_advanced_team_stats(season)
    _audit("external_data.sync", "season", season, {"datasets": datasets})
    return jsonify(result), 201


@admin_bp.post("/commercial-sync")
def commercial_sync():
    from commercial_integrations import sync_commercial
    season = request.args.get("season", type=int)
    if not season:
        return jsonify({"error": "season_required"}), 400
    week = request.args.get("week", type=int)
    datasets = [x.strip() for x in request.args.get("datasets", "weather,odds,live_games,coaches,transactions").split(",") if x.strip()]
    allowed = {"weather", "odds", "live_games", "coaches", "transactions"}
    if not datasets or any(x not in allowed for x in datasets):
        return jsonify({"error": "invalid_datasets", "allowed": sorted(allowed)}), 400
    result = sync_commercial(season, datasets, week)
    _audit("commercial_data.sync", "season", season, {"datasets": datasets, "week": week})
    return jsonify(result), 201


@admin_bp.get("/integrations")
def integrations_status():
    from integration_hub import integration_status
    return jsonify(integration_status())


@admin_bp.post("/integrations/sync")
def integrations_sync():
    from integration_hub import run_integrations
    payload = request.get_json(silent=True) or {}
    season = payload.get("season") or request.args.get("season", type=int)
    if not season:
        return jsonify({"error": "season_required"}), 400
    week = payload.get("week") if "week" in payload else request.args.get("week", type=int)
    datasets = payload.get("datasets") or [x.strip() for x in request.args.get("datasets", "pbp,rosters,injuries,depth_charts,snap_counts,weather,odds,live_games,coaches,transactions").split(",") if x.strip()]
    if not isinstance(datasets, list) or not all(isinstance(x, str) for x in datasets):
        return jsonify({"error": "datasets_must_be_a_list"}), 400
    result = run_integrations(int(season), datasets, int(week) if week is not None else None)
    _audit("integrations.sync", "season", season, {"datasets": datasets, "week": week, "ok": result["ok"]})
    return jsonify(result), 201 if result["ok"] else 207
