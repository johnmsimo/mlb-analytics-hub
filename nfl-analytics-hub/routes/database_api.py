"""Database health, inventory, and controlled ingestion endpoints."""
from __future__ import annotations

import os

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import func, select

from database import db
from data_ingestion import sync_cached_data
from analytics_warehouse import rebuild_analytics
from db_models import (Coach, CoachingAssignment, CoachSeasonStat, DataSyncRun, Game,
    Player, PlayerGameStat, PlayerSeasonStat, Team, TeamGameStat, TeamSeasonStat,
    DataSource, RawIngestRecord, DataQualityIssue)


database_bp = Blueprint("database", __name__, url_prefix="/api/data")


@database_bp.get("/status")
def status():
    counts = {}
    for name, model in {
        "teams": Team, "games": Game, "players": Player,
        "player_game_stats": PlayerGameStat, "team_game_stats": TeamGameStat,
        "team_season_stats": TeamSeasonStat, "player_season_stats": PlayerSeasonStat,
        "coaches": Coach, "coaching_assignments": CoachingAssignment,
        "coach_season_stats": CoachSeasonStat,
        "data_sources": DataSource, "raw_ingest_records": RawIngestRecord,
        "data_quality_issues": DataQualityIssue,
    }.items():
        counts[name] = db.session.scalar(select(func.count()).select_from(model))
    counts["open_quality_issues"] = db.session.scalar(select(func.count()).select_from(DataQualityIssue).where(DataQualityIssue.resolved.is_(False)))
    latest = db.session.scalar(select(DataSyncRun).order_by(DataSyncRun.id.desc()).limit(1))
    return jsonify({
        "database": current_app.config["SQLALCHEMY_DATABASE_URI"].split("@")[-1],
        "counts": counts,
        "latest_sync": None if not latest else {
            "id": latest.id, "source": latest.source, "status": latest.status,
            "records_read": latest.records_read, "records_written": latest.records_written,
            "started_at": latest.started_at.isoformat() if latest.started_at else None,
            "finished_at": latest.finished_at.isoformat() if latest.finished_at else None,
            "error": latest.error,
        },
    })


@database_bp.post("/sync")
def sync():
    result = sync_cached_data(os.environ.get("NFL_DATA_DIR") or os.path.join(current_app.root_path, "data"))
    return jsonify(result), 201


@database_bp.post("/rebuild-analytics")
def rebuild():
    season = request.args.get("season", type=int)
    return jsonify({"season": season, "results": rebuild_analytics(season)}), 201


@database_bp.get("/coaches")
def coaches():
    season = request.args.get("season", type=int)
    query = db.select(CoachingAssignment, Coach, Team).join(
        Coach, CoachingAssignment.coach_id == Coach.id).join(
        Team, CoachingAssignment.team_id == Team.id)
    if season is not None:
        query = query.where(CoachingAssignment.season == season)
    rows = db.session.execute(query.order_by(CoachingAssignment.season.desc(), Team.abbreviation, CoachingAssignment.role)).all()
    payload = []
    for assignment, coach, team in rows:
        stat = db.session.scalar(db.select(CoachSeasonStat).where(
            CoachSeasonStat.coach_id == coach.id, CoachSeasonStat.team_id == team.id,
            CoachSeasonStat.season == assignment.season, CoachSeasonStat.role == assignment.role))
        payload.append({
            "id": coach.id, "name": coach.full_name, "team": team.abbreviation,
            "season": assignment.season, "role": assignment.role,
            "start_date": assignment.start_date.isoformat() if assignment.start_date else None,
            "end_date": assignment.end_date.isoformat() if assignment.end_date else None,
            "record": None if not stat else {"games": stat.games, "wins": stat.wins,
                "losses": stat.losses, "ties": stat.ties, "win_pct": stat.win_pct,
                "point_differential": stat.point_differential, "ppg": stat.team_ppg,
                "papg": stat.team_papg},
        })
    return jsonify({"season": season, "count": len(payload), "coaches": payload})

@database_bp.get("/sources")
def sources():
    from db_models import DataSource, RawIngestRecord
    rows = db.session.scalars(db.select(DataSource).order_by(DataSource.name)).all()
    payload = []
    for source in rows:
        raw_count = db.session.scalar(db.select(func.count()).select_from(RawIngestRecord).where(
            RawIngestRecord.source_id == source.id))
        payload.append({
            "key": source.key, "name": source.name, "type": source.source_type,
            "enabled": source.enabled, "base_url": source.base_url,
            "license": source.license_name, "attribution": source.attribution,
            "refresh_interval_minutes": source.refresh_interval_minutes,
            "last_success_at": source.last_success_at.isoformat() if source.last_success_at else None,
            "last_failure_at": source.last_failure_at.isoformat() if source.last_failure_at else None,
            "raw_versions": raw_count,
        })
    return jsonify({"count": len(payload), "sources": payload})


@database_bp.post("/quality/run")
def quality_run():
    from data_quality import run_quality_checks
    return jsonify(run_quality_checks()), 201


@database_bp.get("/quality")
def quality_issues():
    from db_models import DataQualityIssue
    severity = request.args.get("severity")
    resolved = request.args.get("resolved", "false").lower() in {"1", "true", "yes"}
    limit = min(max(request.args.get("limit", 100, type=int), 1), 500)
    query = db.select(DataQualityIssue).where(DataQualityIssue.resolved.is_(resolved))
    if severity:
        query = query.where(DataQualityIssue.severity == severity)
    rows = db.session.scalars(query.order_by(DataQualityIssue.detected_at.desc()).limit(limit)).all()
    return jsonify({"count": len(rows), "issues": [{
        "id": row.id, "check": row.check_name, "severity": row.severity,
        "entity_type": row.entity_type, "entity_id": row.entity_id,
        "message": row.message, "details": row.details,
        "detected_at": row.detected_at.isoformat() if row.detected_at else None,
        "resolved": row.resolved,
    } for row in rows]})


@database_bp.get("/teams/<abbr>/profile")
def team_profile(abbr):
    from db_models import TeamSeasonStat
    team = db.session.scalar(db.select(Team).where(Team.abbreviation == abbr.upper()))
    if not team:
        return jsonify({"error": "team_not_found"}), 404
    seasons = db.session.scalars(db.select(TeamSeasonStat).where(
        TeamSeasonStat.team_id == team.id).order_by(TeamSeasonStat.season.desc())).all()
    assignments = db.session.execute(db.select(CoachingAssignment, Coach).join(
        Coach, CoachingAssignment.coach_id == Coach.id).where(
        CoachingAssignment.team_id == team.id).order_by(CoachingAssignment.season.desc())).all()
    return jsonify({
        "team": {"id": team.id, "abbreviation": team.abbreviation, "name": team.name,
                 "city": team.city, "conference": team.conference, "division": team.division,
                 "active": team.active},
        "seasons": [{"season": row.season, "type": row.season_type, "games": row.games,
                     "wins": row.wins, "losses": row.losses, "ties": row.ties,
                     "win_pct": row.win_pct, "points_for": row.points_for,
                     "points_against": row.points_against,
                     "point_differential": row.point_differential,
                     "ppg": row.ppg, "papg": row.papg,
                     "streak": row.current_streak} for row in seasons],
        "coaches": [{"id": coach.id, "name": coach.full_name,
                     "season": assignment.season, "role": assignment.role}
                    for assignment, coach in assignments],
    })


@database_bp.get("/players/<int:player_id>/profile")
def player_profile(player_id):
    from db_models import PlayerSeasonStat
    player = db.session.get(Player, player_id)
    if not player:
        return jsonify({"error": "player_not_found"}), 404
    rows = db.session.execute(db.select(PlayerSeasonStat, Team).join(
        Team, PlayerSeasonStat.team_id == Team.id).where(
        PlayerSeasonStat.player_id == player.id).order_by(PlayerSeasonStat.season.desc())).all()
    return jsonify({
        "player": {"id": player.id, "external_id": player.external_id,
                   "name": player.full_name, "position": player.position,
                   "height_inches": player.height_inches, "weight_lbs": player.weight_lbs,
                   "college": player.college, "active": player.active},
        "seasons": [{"season": stat.season, "type": stat.season_type,
                     "team": team.abbreviation, "games": stat.games,
                     "passing_yards": stat.passing_yards, "passing_tds": stat.passing_tds,
                     "interceptions": stat.interceptions, "rushing_yards": stat.rushing_yards,
                     "rushing_tds": stat.rushing_tds, "receptions": stat.receptions,
                     "targets": stat.targets, "receiving_yards": stat.receiving_yards,
                     "receiving_tds": stat.receiving_tds, "total_yards": stat.total_yards,
                     "total_tds": stat.total_tds, "fantasy_points": stat.fantasy_points}
                    for stat, team in rows],
    })


@database_bp.get("/coaches/<int:coach_id>/profile")
def coach_profile(coach_id):
    coach = db.session.get(Coach, coach_id)
    if not coach:
        return jsonify({"error": "coach_not_found"}), 404
    rows = db.session.execute(db.select(CoachingAssignment, Team).join(
        Team, CoachingAssignment.team_id == Team.id).where(
        CoachingAssignment.coach_id == coach.id).order_by(CoachingAssignment.season.desc())).all()
    stats = db.session.scalars(db.select(CoachSeasonStat).where(
        CoachSeasonStat.coach_id == coach.id)).all()
    stat_map = {(x.team_id, x.season, x.role): x for x in stats}
    history = []
    for assignment, team in rows:
        stat = stat_map.get((team.id, assignment.season, assignment.role))
        history.append({"team": team.abbreviation, "season": assignment.season,
                        "role": assignment.role,
                        "start_date": assignment.start_date.isoformat() if assignment.start_date else None,
                        "end_date": assignment.end_date.isoformat() if assignment.end_date else None,
                        "record": None if not stat else {"games": stat.games, "wins": stat.wins,
                            "losses": stat.losses, "ties": stat.ties, "win_pct": stat.win_pct,
                            "point_differential": stat.point_differential,
                            "ppg": stat.team_ppg, "papg": stat.team_papg}})
    return jsonify({"coach": {"id": coach.id, "name": coach.full_name,
                               "active": coach.active}, "history": history})
