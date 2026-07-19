"""Data quality checks and provenance summaries for the NFL warehouse."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import and_, func, or_, select

from database import db
from db_models import (
    Coach,
    CoachingAssignment,
    DataQualityIssue,
    Game,
    Player,
    PlayerGameStat,
    PlayerTeamSeason,
    Team,
)


def _now():
    return datetime.now(timezone.utc)


def _issue(check_name: str, severity: str, entity_type: str, entity_id: str | None,
           message: str, details: dict | None = None) -> DataQualityIssue:
    return DataQualityIssue(
        check_name=check_name,
        severity=severity,
        entity_type=entity_type,
        entity_id=entity_id,
        message=message,
        details=details or {},
        detected_at=_now(),
        resolved=False,
    )


def run_quality_checks(*, clear_open: bool = True) -> dict:
    """Run deterministic warehouse checks and persist actionable issues."""
    if clear_open:
        db.session.execute(db.delete(DataQualityIssue).where(DataQualityIssue.resolved.is_(False)))

    issues: list[DataQualityIssue] = []

    # Team identity integrity.
    duplicate_abbrs = db.session.execute(
        select(Team.abbreviation, func.count(Team.id))
        .group_by(Team.abbreviation)
        .having(func.count(Team.id) > 1)
    ).all()
    for abbr, count in duplicate_abbrs:
        issues.append(_issue("duplicate_team_abbreviation", "error", "team", abbr,
                             f"{count} team rows share abbreviation {abbr}."))

    # Games with incomplete identity or impossible scores.
    bad_games = db.session.scalars(select(Game).where(or_(
        Game.home_team_id == Game.away_team_id,
        and_(Game.completed.is_(True), or_(Game.home_score.is_(None), Game.away_score.is_(None))),
        Game.home_score < 0,
        Game.away_score < 0,
    ))).all()
    for game in bad_games:
        issues.append(_issue("invalid_game", "error", "game", game.external_id,
                             "Game has invalid teams, completion state, or score.",
                             {"home_score": game.home_score, "away_score": game.away_score,
                              "completed": game.completed}))

    # Player facts must align with the game's teams.
    rows = db.session.execute(
        select(PlayerGameStat, Game)
        .join(Game, PlayerGameStat.game_id == Game.id)
        .where(and_(PlayerGameStat.team_id != Game.home_team_id, PlayerGameStat.team_id != Game.away_team_id))
    ).all()
    for stat, game in rows:
        issues.append(_issue("player_team_not_in_game", "error", "player_game_stat", str(stat.id),
                             "Player stat team is not a participant in the game.",
                             {"game": game.external_id, "team_id": stat.team_id}))

    # Negative counting stats are almost always upstream corruption.
    negative_stats = db.session.scalars(select(PlayerGameStat).where(or_(
        PlayerGameStat.attempts < 0,
        PlayerGameStat.completions < 0,
        PlayerGameStat.carries < 0,
        PlayerGameStat.receptions < 0,
        PlayerGameStat.targets < 0,
    ))).all()
    for stat in negative_stats:
        issues.append(_issue("negative_counting_stat", "error", "player_game_stat", str(stat.id),
                             "Player game contains a negative counting statistic."))

    # Membership coverage.
    missing_memberships = db.session.execute(
        select(PlayerGameStat.player_id, PlayerGameStat.team_id, Game.season)
        .join(Game, PlayerGameStat.game_id == Game.id)
        .outerjoin(PlayerTeamSeason, and_(
            PlayerTeamSeason.player_id == PlayerGameStat.player_id,
            PlayerTeamSeason.team_id == PlayerGameStat.team_id,
            PlayerTeamSeason.season == Game.season,
        ))
        .where(PlayerTeamSeason.id.is_(None))
        .distinct()
    ).all()
    for player_id, team_id, season in missing_memberships:
        issues.append(_issue("missing_player_membership", "warning", "player", str(player_id),
                             "Player game fact has no matching player-team-season membership.",
                             {"team_id": team_id, "season": season}))

    # Coaches need assignments to be useful.
    unassigned_coaches = db.session.scalars(
        select(Coach).outerjoin(CoachingAssignment).where(CoachingAssignment.id.is_(None))
    ).all()
    for coach in unassigned_coaches:
        issues.append(_issue("coach_without_assignment", "warning", "coach", str(coach.id),
                             "Coach has no team/season assignment."))

    db.session.add_all(issues)
    db.session.commit()

    by_severity = {"error": 0, "warning": 0, "info": 0}
    for issue in issues:
        by_severity[issue.severity] = by_severity.get(issue.severity, 0) + 1
    return {"total": len(issues), "by_severity": by_severity}
