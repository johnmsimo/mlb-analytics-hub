"""Optional in-process scheduler for small deployments.
For scale, run these functions in a dedicated worker process.
"""
from __future__ import annotations
import os
from datetime import datetime, timezone
from apscheduler.schedulers.background import BackgroundScheduler
from analytics_warehouse import rebuild_analytics
from data_ingestion import sync_cached_data
from data_quality import run_quality_checks
from database import db
from db_models import ScheduledJob
from play_by_play import rebuild_advanced_team_stats
from external_providers import sync_external
from commercial_integrations import sync_commercial

_scheduler = None

JOBS = {
    "cached-data-sync": {"name": "Cached data sync", "minutes": 60},
    "analytics-rebuild": {"name": "Warehouse aggregate rebuild", "minutes": 60},
    "quality-checks": {"name": "Data quality checks", "minutes": 60},
    "external-data-sync": {"name": "External NFL data sync", "minutes": 60},
    "commercial-data-sync": {"name": "Credentialed provider sync", "minutes": 15},
}


def _record(key, status, error=None):
    row = db.session.scalar(db.select(ScheduledJob).where(ScheduledJob.key == key))
    if not row:
        cfg = JOBS[key]
        row = ScheduledJob(key=key, name=cfg["name"], cron=f"every {cfg['minutes']} minutes")
        db.session.add(row)
    row.last_status = status
    row.last_error = error
    row.last_finished_at = datetime.now(timezone.utc)
    db.session.commit()


def run_job(app, key):
    with app.app_context():
        row = db.session.scalar(db.select(ScheduledJob).where(ScheduledJob.key == key))
        if row:
            row.last_started_at = datetime.now(timezone.utc)
            db.session.commit()
        try:
            if key == "cached-data-sync":
                sync_cached_data(os.environ.get("NFL_DATA_DIR") or os.path.join(app.root_path, "data"))
            elif key == "analytics-rebuild":
                rebuild_analytics(None); rebuild_advanced_team_stats(None)
            elif key == "quality-checks":
                run_quality_checks()
            elif key == "external-data-sync":
                if os.environ.get("ENABLE_EXTERNAL_SYNC", "false").lower() not in {"1", "true", "yes"}:
                    raise RuntimeError("external sync is disabled; set ENABLE_EXTERNAL_SYNC=true")
                season = int(os.environ.get("EXTERNAL_DATA_SEASON") or datetime.now(timezone.utc).year)
                datasets = [x.strip() for x in os.environ.get("EXTERNAL_DATASETS", "pbp,rosters,injuries,depth_charts,snap_counts").split(",") if x.strip()]
                sync_external(season, datasets)
                if "pbp" in datasets:
                    rebuild_advanced_team_stats(season)
            elif key == "commercial-data-sync":
                if os.environ.get("ENABLE_COMMERCIAL_SYNC", "false").lower() not in {"1", "true", "yes"}:
                    raise RuntimeError("commercial sync is disabled; set ENABLE_COMMERCIAL_SYNC=true")
                season = int(os.environ.get("EXTERNAL_DATA_SEASON") or datetime.now(timezone.utc).year)
                week = int(os.environ["EXTERNAL_DATA_WEEK"]) if os.environ.get("EXTERNAL_DATA_WEEK") else None
                datasets = [x.strip() for x in os.environ.get("COMMERCIAL_DATASETS", "weather,odds,live_games,coaches,transactions").split(",") if x.strip()]
                sync_commercial(season, datasets, week)
            _record(key, "success")
        except Exception as exc:
            _record(key, "failed", str(exc))
            app.logger.exception("scheduled job %s failed", key)


def start_scheduler(app):
    global _scheduler
    if _scheduler or os.environ.get("ENABLE_SCHEDULER", "false").lower() not in {"1", "true", "yes"}:
        return _scheduler
    _scheduler = BackgroundScheduler(timezone="UTC", daemon=True)
    for key, cfg in JOBS.items():
        _scheduler.add_job(run_job, "interval", minutes=cfg["minutes"], args=[app, key], id=key, replace_existing=True)
    _scheduler.start()
    return _scheduler
