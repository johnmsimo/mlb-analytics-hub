"""Build reproducible season-level warehouse aggregates from normalized game facts."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import delete, select

from database import db
from db_models import (
    AnalyticsSnapshot, CoachSeasonStat, CoachingAssignment, Game, PlayerGameStat,
    PlayerSeasonStat, TeamGameStat, TeamSeasonStat,
)


def utcnow():
    return datetime.now(timezone.utc)


def rebuild_team_game_facts(season: int | None = None) -> int:
    query = select(Game).where(Game.completed.is_(True))
    if season is not None:
        query = query.where(Game.season == season)
    games = db.session.scalars(query).all()
    written = 0
    for game in games:
        if game.home_score is None or game.away_score is None:
            continue
        rows = (
            (game.home_team_id, game.away_team_id, True, game.home_score, game.away_score),
            (game.away_team_id, game.home_team_id, False, game.away_score, game.home_score),
        )
        for team_id, opponent_id, home, points, allowed in rows:
            fact = db.session.scalar(select(TeamGameStat).where(
                TeamGameStat.game_id == game.id, TeamGameStat.team_id == team_id))
            if not fact:
                fact = TeamGameStat(game_id=game.id, team_id=team_id,
                                    opponent_id=opponent_id, home=home)
                db.session.add(fact)
            fact.opponent_id = opponent_id
            fact.home = home
            fact.points = points
            fact.points_allowed = allowed
            written += 1
    db.session.commit()
    return written


def rebuild_team_seasons(season: int | None = None) -> int:
    query = select(Game).where(Game.completed.is_(True))
    if season is not None:
        query = query.where(Game.season == season)
    games = db.session.scalars(query.order_by(Game.kickoff_at, Game.id)).all()
    buckets = defaultdict(list)
    for game in games:
        if game.home_score is None or game.away_score is None:
            continue
        buckets[(game.home_team_id, game.season, game.season_type)].append((game, True))
        buckets[(game.away_team_id, game.season, game.season_type)].append((game, False))

    count = 0
    for (team_id, year, season_type), entries in buckets.items():
        wins = losses = ties = pf = pa = home_wins = away_wins = 0
        streak_result = None
        streak_count = 0
        for game, is_home in entries:
            points = game.home_score if is_home else game.away_score
            allowed = game.away_score if is_home else game.home_score
            pf += int(points)
            pa += int(allowed)
            result = "T"
            if points > allowed:
                result = "W"; wins += 1
                home_wins += int(is_home); away_wins += int(not is_home)
            elif points < allowed:
                result = "L"; losses += 1
            else:
                ties += 1
            if result == streak_result:
                streak_count += 1
            else:
                streak_result, streak_count = result, 1
        total = len(entries)
        row = db.session.scalar(select(TeamSeasonStat).where(
            TeamSeasonStat.team_id == team_id, TeamSeasonStat.season == year,
            TeamSeasonStat.season_type == season_type))
        if not row:
            row = TeamSeasonStat(team_id=team_id, season=year, season_type=season_type)
            db.session.add(row)
        row.games, row.wins, row.losses, row.ties = total, wins, losses, ties
        row.points_for, row.points_against = pf, pa
        row.point_differential = pf - pa
        row.win_pct = round((wins + ties * .5) / total, 4) if total else None
        row.ppg = round(pf / total, 3) if total else None
        row.papg = round(pa / total, 3) if total else None
        row.home_wins, row.away_wins = home_wins, away_wins
        row.current_streak = f"{streak_result}{streak_count}" if streak_result else None
        row.calculated_at = utcnow()
        count += 1
    db.session.commit()
    return count


def rebuild_player_seasons(season: int | None = None) -> int:
    query = select(PlayerGameStat, Game).join(Game, PlayerGameStat.game_id == Game.id)
    if season is not None:
        query = query.where(Game.season == season)
    buckets = defaultdict(list)
    for stat, game in db.session.execute(query).all():
        buckets[(stat.player_id, stat.team_id, game.season, game.season_type)].append(stat)
    count = 0
    fields = ["completions", "attempts", "passing_yards", "passing_tds", "interceptions",
              "carries", "rushing_yards", "rushing_tds", "receptions", "targets",
              "receiving_yards", "receiving_tds"]
    for (player_id, team_id, year, season_type), stats in buckets.items():
        values = {field: sum(float(getattr(s, field) or 0) for s in stats) for field in fields}
        row = db.session.scalar(select(PlayerSeasonStat).where(
            PlayerSeasonStat.player_id == player_id, PlayerSeasonStat.team_id == team_id,
            PlayerSeasonStat.season == year, PlayerSeasonStat.season_type == season_type))
        if not row:
            row = PlayerSeasonStat(player_id=player_id, team_id=team_id,
                                   season=year, season_type=season_type)
            db.session.add(row)
        row.games = len(stats)
        for field, value in values.items():
            setattr(row, field, int(value) if field in {"completions", "attempts"} else value)
        row.total_yards = values["passing_yards"] + values["rushing_yards"] + values["receiving_yards"]
        row.total_tds = values["passing_tds"] + values["rushing_tds"] + values["receiving_tds"]
        row.fantasy_points = round(values["passing_yards"] / 25 + values["passing_tds"] * 4
            - values["interceptions"] * 2 + values["rushing_yards"] / 10
            + values["rushing_tds"] * 6 + values["receptions"]
            + values["receiving_yards"] / 10 + values["receiving_tds"] * 6, 2)
        row.calculated_at = utcnow()
        count += 1
    db.session.commit()
    return count


def rebuild_coach_seasons(season: int | None = None) -> int:
    query = select(CoachingAssignment)
    if season is not None:
        query = query.where(CoachingAssignment.season == season)
    assignments = db.session.scalars(query).all()
    count = 0
    for assignment in assignments:
        team_stat = db.session.scalar(select(TeamSeasonStat).where(
            TeamSeasonStat.team_id == assignment.team_id,
            TeamSeasonStat.season == assignment.season,
            TeamSeasonStat.season_type == "REG"))
        row = db.session.scalar(select(CoachSeasonStat).where(
            CoachSeasonStat.coach_id == assignment.coach_id,
            CoachSeasonStat.team_id == assignment.team_id,
            CoachSeasonStat.season == assignment.season,
            CoachSeasonStat.role == assignment.role))
        if not row:
            row = CoachSeasonStat(coach_id=assignment.coach_id, team_id=assignment.team_id,
                                  season=assignment.season, role=assignment.role)
            db.session.add(row)
        if team_stat:
            for field in ("games", "wins", "losses", "ties", "win_pct", "points_for",
                          "points_against", "point_differential"):
                setattr(row, field, getattr(team_stat, field))
            row.team_ppg, row.team_papg = team_stat.ppg, team_stat.papg
        row.calculated_at = utcnow()
        count += 1
    db.session.commit()
    return count


def rebuild_analytics(season: int | None = None) -> dict:
    facts = rebuild_team_game_facts(season)
    teams = rebuild_team_seasons(season)
    players = rebuild_player_seasons(season)
    coaches = rebuild_coach_seasons(season)
    return {"team_game_facts": facts, "team_seasons": teams,
            "player_seasons": players, "coach_seasons": coaches}
