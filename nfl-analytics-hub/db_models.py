"""Normalized NFL data warehouse models.

Raw facts are stored at game/player-game grain. Derived metrics are versioned
in analytics_snapshots so model outputs can be reproduced and audited.
"""
from __future__ import annotations

from datetime import datetime, timezone

from database import db


def utcnow():
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class Team(TimestampMixin, db.Model):
    __tablename__ = "teams"
    id = db.Column(db.Integer, primary_key=True)
    external_id = db.Column(db.String(32), unique=True, index=True)
    abbreviation = db.Column(db.String(8), nullable=False, unique=True, index=True)
    name = db.Column(db.String(120), nullable=False)
    city = db.Column(db.String(80))
    conference = db.Column(db.String(8), index=True)
    division = db.Column(db.String(16), index=True)
    active = db.Column(db.Boolean, nullable=False, default=True)


class Season(db.Model):
    __tablename__ = "seasons"
    year = db.Column(db.Integer, primary_key=True)
    current = db.Column(db.Boolean, nullable=False, default=False)


class Game(TimestampMixin, db.Model):
    __tablename__ = "games"
    id = db.Column(db.Integer, primary_key=True)
    external_id = db.Column(db.String(40), nullable=False, unique=True, index=True)
    season = db.Column(db.Integer, db.ForeignKey("seasons.year"), nullable=False, index=True)
    season_type = db.Column(db.String(8), nullable=False, index=True)
    week = db.Column(db.Integer, nullable=False, index=True)
    kickoff_at = db.Column(db.DateTime(timezone=True), index=True)
    venue = db.Column(db.String(160))
    state = db.Column(db.String(16), index=True)
    status_detail = db.Column(db.String(80))
    completed = db.Column(db.Boolean, nullable=False, default=False, index=True)
    home_team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False, index=True)
    away_team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False, index=True)
    home_score = db.Column(db.Integer)
    away_score = db.Column(db.Integer)

    __table_args__ = (
        db.CheckConstraint("home_team_id <> away_team_id", name="ck_game_distinct_teams"),
        db.Index("ix_games_season_week_type", "season", "week", "season_type"),
    )


class TeamGameStat(TimestampMixin, db.Model):
    __tablename__ = "team_game_stats"
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey("games.id", ondelete="CASCADE"), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False)
    opponent_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False)
    home = db.Column(db.Boolean, nullable=False)
    points = db.Column(db.Integer)
    points_allowed = db.Column(db.Integer)
    total_yards = db.Column(db.Integer)
    passing_yards = db.Column(db.Integer)
    rushing_yards = db.Column(db.Integer)
    turnovers = db.Column(db.Integer)
    first_downs = db.Column(db.Integer)
    plays = db.Column(db.Integer)
    epa = db.Column(db.Float)
    success_rate = db.Column(db.Float)

    __table_args__ = (db.UniqueConstraint("game_id", "team_id", name="uq_team_game_stat"),)


class Player(TimestampMixin, db.Model):
    __tablename__ = "players"
    id = db.Column(db.Integer, primary_key=True)
    external_id = db.Column(db.String(40), nullable=False, unique=True, index=True)
    full_name = db.Column(db.String(140), nullable=False, index=True)
    first_name = db.Column(db.String(70))
    last_name = db.Column(db.String(70), index=True)
    position = db.Column(db.String(8), index=True)
    birth_date = db.Column(db.Date)
    height_inches = db.Column(db.Integer)
    weight_lbs = db.Column(db.Integer)
    college = db.Column(db.String(120))
    active = db.Column(db.Boolean, nullable=False, default=True, index=True)


class PlayerTeamSeason(TimestampMixin, db.Model):
    __tablename__ = "player_team_seasons"
    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, db.ForeignKey("players.id", ondelete="CASCADE"), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False)
    season = db.Column(db.Integer, db.ForeignKey("seasons.year"), nullable=False)
    jersey_number = db.Column(db.String(8))
    depth_position = db.Column(db.String(16))
    status = db.Column(db.String(32))

    __table_args__ = (db.UniqueConstraint("player_id", "team_id", "season", name="uq_player_team_season"),)


class PlayerGameStat(TimestampMixin, db.Model):
    __tablename__ = "player_game_stats"
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey("games.id", ondelete="CASCADE"), nullable=False)
    player_id = db.Column(db.Integer, db.ForeignKey("players.id", ondelete="CASCADE"), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False)
    opponent_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False)
    position = db.Column(db.String(8), index=True)
    home = db.Column(db.Boolean, nullable=False)
    completions = db.Column(db.Integer, default=0)
    attempts = db.Column(db.Integer, default=0)
    passing_yards = db.Column(db.Float, default=0)
    passing_tds = db.Column(db.Float, default=0)
    interceptions = db.Column(db.Float, default=0)
    sacks = db.Column(db.Float, default=0)
    carries = db.Column(db.Float, default=0)
    rushing_yards = db.Column(db.Float, default=0)
    rushing_tds = db.Column(db.Float, default=0)
    receptions = db.Column(db.Float, default=0)
    targets = db.Column(db.Float, default=0)
    receiving_yards = db.Column(db.Float, default=0)
    receiving_tds = db.Column(db.Float, default=0)
    fumbles_lost = db.Column(db.Float, default=0)

    __table_args__ = (
        db.UniqueConstraint("game_id", "player_id", name="uq_player_game_stat"),
        db.Index("ix_player_stats_player_game", "player_id", "game_id"),
    )


class Coach(TimestampMixin, db.Model):
    __tablename__ = "coaches"
    id = db.Column(db.Integer, primary_key=True)
    external_id = db.Column(db.String(40), unique=True, index=True)
    full_name = db.Column(db.String(140), nullable=False, index=True)
    birth_date = db.Column(db.Date)
    active = db.Column(db.Boolean, nullable=False, default=True)


class CoachingAssignment(TimestampMixin, db.Model):
    __tablename__ = "coaching_assignments"
    id = db.Column(db.Integer, primary_key=True)
    coach_id = db.Column(db.Integer, db.ForeignKey("coaches.id", ondelete="CASCADE"), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False)
    season = db.Column(db.Integer, db.ForeignKey("seasons.year"), nullable=False)
    role = db.Column(db.String(80), nullable=False, index=True)
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)

    __table_args__ = (db.UniqueConstraint("coach_id", "team_id", "season", "role", name="uq_coach_assignment"),)


class AnalyticsSnapshot(TimestampMixin, db.Model):
    __tablename__ = "analytics_snapshots"
    id = db.Column(db.Integer, primary_key=True)
    entity_type = db.Column(db.String(24), nullable=False, index=True)
    entity_id = db.Column(db.String(40), nullable=False, index=True)
    season = db.Column(db.Integer, index=True)
    week = db.Column(db.Integer, index=True)
    metric = db.Column(db.String(80), nullable=False, index=True)
    value = db.Column(db.Float)
    payload = db.Column(db.JSON)
    model_version = db.Column(db.String(40), nullable=False, default="baseline-v1")
    calculated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)

    __table_args__ = (
        db.UniqueConstraint("entity_type", "entity_id", "season", "week", "metric", "model_version", name="uq_analytics_snapshot"),
    )


class DataSyncRun(db.Model):
    __tablename__ = "data_sync_runs"
    id = db.Column(db.Integer, primary_key=True)
    source = db.Column(db.String(80), nullable=False, index=True)
    started_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    finished_at = db.Column(db.DateTime(timezone=True))
    status = db.Column(db.String(20), nullable=False, default="running", index=True)
    records_read = db.Column(db.Integer, nullable=False, default=0)
    records_written = db.Column(db.Integer, nullable=False, default=0)
    error = db.Column(db.Text)
    details = db.Column(db.JSON)

class TeamSeasonStat(TimestampMixin, db.Model):
    __tablename__ = "team_season_stats"
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    season = db.Column(db.Integer, db.ForeignKey("seasons.year"), nullable=False)
    season_type = db.Column(db.String(8), nullable=False, default="REG")
    games = db.Column(db.Integer, nullable=False, default=0)
    wins = db.Column(db.Integer, nullable=False, default=0)
    losses = db.Column(db.Integer, nullable=False, default=0)
    ties = db.Column(db.Integer, nullable=False, default=0)
    points_for = db.Column(db.Integer, nullable=False, default=0)
    points_against = db.Column(db.Integer, nullable=False, default=0)
    point_differential = db.Column(db.Integer, nullable=False, default=0)
    win_pct = db.Column(db.Float)
    ppg = db.Column(db.Float)
    papg = db.Column(db.Float)
    home_wins = db.Column(db.Integer, nullable=False, default=0)
    away_wins = db.Column(db.Integer, nullable=False, default=0)
    current_streak = db.Column(db.String(12))
    calculated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        db.UniqueConstraint("team_id", "season", "season_type", name="uq_team_season_stat"),
        db.Index("ix_team_season_stats_lookup", "season", "season_type", "team_id"),
    )


class PlayerSeasonStat(TimestampMixin, db.Model):
    __tablename__ = "player_season_stats"
    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, db.ForeignKey("players.id", ondelete="CASCADE"), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False)
    season = db.Column(db.Integer, db.ForeignKey("seasons.year"), nullable=False)
    season_type = db.Column(db.String(8), nullable=False, default="REG")
    games = db.Column(db.Integer, nullable=False, default=0)
    completions = db.Column(db.Integer, nullable=False, default=0)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    passing_yards = db.Column(db.Float, nullable=False, default=0)
    passing_tds = db.Column(db.Float, nullable=False, default=0)
    interceptions = db.Column(db.Float, nullable=False, default=0)
    carries = db.Column(db.Float, nullable=False, default=0)
    rushing_yards = db.Column(db.Float, nullable=False, default=0)
    rushing_tds = db.Column(db.Float, nullable=False, default=0)
    receptions = db.Column(db.Float, nullable=False, default=0)
    targets = db.Column(db.Float, nullable=False, default=0)
    receiving_yards = db.Column(db.Float, nullable=False, default=0)
    receiving_tds = db.Column(db.Float, nullable=False, default=0)
    total_yards = db.Column(db.Float, nullable=False, default=0)
    total_tds = db.Column(db.Float, nullable=False, default=0)
    fantasy_points = db.Column(db.Float, nullable=False, default=0)
    calculated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        db.UniqueConstraint("player_id", "team_id", "season", "season_type", name="uq_player_season_stat"),
        db.Index("ix_player_season_stats_lookup", "season", "season_type", "player_id"),
    )


class CoachSeasonStat(TimestampMixin, db.Model):
    __tablename__ = "coach_season_stats"
    id = db.Column(db.Integer, primary_key=True)
    coach_id = db.Column(db.Integer, db.ForeignKey("coaches.id", ondelete="CASCADE"), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False)
    season = db.Column(db.Integer, db.ForeignKey("seasons.year"), nullable=False)
    role = db.Column(db.String(80), nullable=False)
    games = db.Column(db.Integer, nullable=False, default=0)
    wins = db.Column(db.Integer, nullable=False, default=0)
    losses = db.Column(db.Integer, nullable=False, default=0)
    ties = db.Column(db.Integer, nullable=False, default=0)
    win_pct = db.Column(db.Float)
    points_for = db.Column(db.Integer, nullable=False, default=0)
    points_against = db.Column(db.Integer, nullable=False, default=0)
    point_differential = db.Column(db.Integer, nullable=False, default=0)
    team_ppg = db.Column(db.Float)
    team_papg = db.Column(db.Float)
    calculated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        db.UniqueConstraint("coach_id", "team_id", "season", "role", name="uq_coach_season_stat"),
        db.Index("ix_coach_season_stats_lookup", "season", "coach_id", "team_id"),
    )


class DataSource(TimestampMixin, db.Model):
    __tablename__ = "data_sources"
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(80), nullable=False, unique=True, index=True)
    name = db.Column(db.String(160), nullable=False)
    source_type = db.Column(db.String(40), nullable=False, default="file")
    base_url = db.Column(db.String(500))
    license_name = db.Column(db.String(160))
    attribution = db.Column(db.Text)
    enabled = db.Column(db.Boolean, nullable=False, default=True, index=True)
    refresh_interval_minutes = db.Column(db.Integer)
    last_success_at = db.Column(db.DateTime(timezone=True))
    last_failure_at = db.Column(db.DateTime(timezone=True))
    metadata_json = db.Column(db.JSON)


class RawIngestRecord(db.Model):
    __tablename__ = "raw_ingest_records"
    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.Integer, db.ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False)
    entity_type = db.Column(db.String(40), nullable=False, index=True)
    external_id = db.Column(db.String(120), nullable=False, index=True)
    season = db.Column(db.Integer, index=True)
    week = db.Column(db.Integer, index=True)
    payload = db.Column(db.JSON, nullable=False)
    payload_hash = db.Column(db.String(64), nullable=False, index=True)
    observed_at = db.Column(db.DateTime(timezone=True))
    ingested_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)

    __table_args__ = (
        db.UniqueConstraint("source_id", "entity_type", "external_id", "payload_hash", name="uq_raw_ingest_version"),
        db.Index("ix_raw_ingest_lookup", "source_id", "entity_type", "external_id"),
    )


class DataQualityIssue(db.Model):
    __tablename__ = "data_quality_issues"
    id = db.Column(db.Integer, primary_key=True)
    check_name = db.Column(db.String(100), nullable=False, index=True)
    severity = db.Column(db.String(16), nullable=False, index=True)
    entity_type = db.Column(db.String(40), nullable=False, index=True)
    entity_id = db.Column(db.String(120), index=True)
    message = db.Column(db.Text, nullable=False)
    details = db.Column(db.JSON)
    detected_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    resolved = db.Column(db.Boolean, nullable=False, default=False, index=True)
    resolved_at = db.Column(db.DateTime(timezone=True))


class SchemaVersion(db.Model):
    __tablename__ = "schema_versions"
    version = db.Column(db.String(40), primary_key=True)
    applied_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    description = db.Column(db.String(240))

class Play(TimestampMixin, db.Model):
    __tablename__ = "plays"
    id = db.Column(db.Integer, primary_key=True)
    external_id = db.Column(db.String(80), nullable=False, unique=True, index=True)
    game_id = db.Column(db.Integer, db.ForeignKey("games.id", ondelete="CASCADE"), nullable=False, index=True)
    drive_id = db.Column(db.String(80), index=True)
    sequence = db.Column(db.Integer, nullable=False)
    quarter = db.Column(db.Integer, index=True)
    clock_seconds = db.Column(db.Integer)
    offense_team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), index=True)
    defense_team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), index=True)
    down = db.Column(db.Integer)
    yards_to_go = db.Column(db.Integer)
    yard_line = db.Column(db.Integer)
    play_type = db.Column(db.String(40), index=True)
    description = db.Column(db.Text)
    yards_gained = db.Column(db.Float)
    first_down = db.Column(db.Boolean)
    touchdown = db.Column(db.Boolean)
    turnover = db.Column(db.Boolean)
    expected_points_before = db.Column(db.Float)
    expected_points_after = db.Column(db.Float)
    epa = db.Column(db.Float, index=True)
    success = db.Column(db.Boolean, index=True)
    win_probability_before = db.Column(db.Float)
    win_probability_after = db.Column(db.Float)
    wpa = db.Column(db.Float)
    personnel = db.Column(db.String(40))
    formation = db.Column(db.String(40))
    raw_payload = db.Column(db.JSON)

    __table_args__ = (
        db.UniqueConstraint("game_id", "sequence", name="uq_play_game_sequence"),
        db.Index("ix_plays_game_offense", "game_id", "offense_team_id"),
        db.Index("ix_plays_seasonal_query", "game_id", "play_type", "down"),
    )


class Drive(TimestampMixin, db.Model):
    __tablename__ = "drives"
    id = db.Column(db.Integer, primary_key=True)
    external_id = db.Column(db.String(80), nullable=False, unique=True, index=True)
    game_id = db.Column(db.Integer, db.ForeignKey("games.id", ondelete="CASCADE"), nullable=False, index=True)
    offense_team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), index=True)
    defense_team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), index=True)
    sequence = db.Column(db.Integer, nullable=False)
    start_quarter = db.Column(db.Integer)
    start_yard_line = db.Column(db.Integer)
    end_yard_line = db.Column(db.Integer)
    plays = db.Column(db.Integer, default=0)
    yards = db.Column(db.Float, default=0)
    points = db.Column(db.Integer, default=0)
    result = db.Column(db.String(40), index=True)
    time_of_possession_seconds = db.Column(db.Integer)
    epa = db.Column(db.Float)
    success_rate = db.Column(db.Float)

    __table_args__ = (db.UniqueConstraint("game_id", "sequence", name="uq_drive_game_sequence"),)


class TeamAdvancedSeasonStat(TimestampMixin, db.Model):
    __tablename__ = "team_advanced_season_stats"
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    season = db.Column(db.Integer, db.ForeignKey("seasons.year"), nullable=False)
    season_type = db.Column(db.String(8), nullable=False, default="REG")
    offensive_plays = db.Column(db.Integer, default=0)
    defensive_plays = db.Column(db.Integer, default=0)
    offensive_epa = db.Column(db.Float)
    defensive_epa = db.Column(db.Float)
    offensive_epa_per_play = db.Column(db.Float)
    defensive_epa_per_play = db.Column(db.Float)
    offensive_success_rate = db.Column(db.Float)
    defensive_success_rate = db.Column(db.Float)
    early_down_success_rate = db.Column(db.Float)
    third_down_success_rate = db.Column(db.Float)
    red_zone_success_rate = db.Column(db.Float)
    explosive_play_rate = db.Column(db.Float)
    calculated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        db.UniqueConstraint("team_id", "season", "season_type", name="uq_team_advanced_season"),
        db.Index("ix_team_advanced_lookup", "season", "season_type", "team_id"),
    )


class ModelVersion(TimestampMixin, db.Model):
    __tablename__ = "model_versions"
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(80), nullable=False, index=True)
    version = db.Column(db.String(40), nullable=False)
    target = db.Column(db.String(80), nullable=False)
    algorithm = db.Column(db.String(80), nullable=False)
    feature_schema = db.Column(db.JSON)
    metrics = db.Column(db.JSON)
    artifact_uri = db.Column(db.String(500))
    training_started_at = db.Column(db.DateTime(timezone=True))
    training_finished_at = db.Column(db.DateTime(timezone=True))
    active = db.Column(db.Boolean, nullable=False, default=False, index=True)

    __table_args__ = (db.UniqueConstraint("key", "version", name="uq_model_key_version"),)


class Prediction(TimestampMixin, db.Model):
    __tablename__ = "predictions"
    id = db.Column(db.Integer, primary_key=True)
    model_version_id = db.Column(db.Integer, db.ForeignKey("model_versions.id"), nullable=False, index=True)
    entity_type = db.Column(db.String(24), nullable=False, index=True)
    entity_id = db.Column(db.String(80), nullable=False, index=True)
    game_id = db.Column(db.Integer, db.ForeignKey("games.id", ondelete="CASCADE"), index=True)
    season = db.Column(db.Integer, index=True)
    week = db.Column(db.Integer, index=True)
    target = db.Column(db.String(80), nullable=False, index=True)
    predicted_value = db.Column(db.Float)
    probability = db.Column(db.Float)
    confidence = db.Column(db.Float)
    features = db.Column(db.JSON)
    data_snapshot_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    actual_value = db.Column(db.Float)
    evaluated_at = db.Column(db.DateTime(timezone=True))

    __table_args__ = (
        db.UniqueConstraint("model_version_id", "entity_type", "entity_id", "game_id", "target", name="uq_prediction_identity"),
        db.Index("ix_predictions_dashboard", "season", "week", "target"),
    )


class ScheduledJob(TimestampMixin, db.Model):
    __tablename__ = "scheduled_jobs"
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(80), nullable=False, unique=True, index=True)
    name = db.Column(db.String(160), nullable=False)
    cron = db.Column(db.String(80), nullable=False)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    last_started_at = db.Column(db.DateTime(timezone=True))
    last_finished_at = db.Column(db.DateTime(timezone=True))
    last_status = db.Column(db.String(20))
    last_error = db.Column(db.Text)
    next_run_at = db.Column(db.DateTime(timezone=True))


class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    id = db.Column(db.Integer, primary_key=True)
    actor = db.Column(db.String(120), index=True)
    action = db.Column(db.String(100), nullable=False, index=True)
    entity_type = db.Column(db.String(40), index=True)
    entity_id = db.Column(db.String(120), index=True)
    details = db.Column(db.JSON)
    ip_address = db.Column(db.String(64))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)

class InjuryReport(TimestampMixin, db.Model):
    __tablename__ = "injury_reports"
    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, db.ForeignKey("players.id", ondelete="CASCADE"), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False)
    season = db.Column(db.Integer, db.ForeignKey("seasons.year"), nullable=False)
    week = db.Column(db.Integer, nullable=False)
    report_date = db.Column(db.Date, nullable=False)
    game_status = db.Column(db.String(40), index=True)
    practice_status = db.Column(db.String(40), index=True)
    primary_injury = db.Column(db.String(120), index=True)
    secondary_injury = db.Column(db.String(120))
    source_key = db.Column(db.String(80), nullable=False, default="nflverse")
    raw_payload = db.Column(db.JSON)
    __table_args__ = (
        db.UniqueConstraint("player_id", "team_id", "season", "week", "report_date", name="uq_injury_report"),
        db.Index("ix_injury_current", "season", "week", "team_id", "game_status"),
    )


class DepthChartEntry(TimestampMixin, db.Model):
    __tablename__ = "depth_chart_entries"
    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, db.ForeignKey("players.id", ondelete="CASCADE"), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False)
    season = db.Column(db.Integer, db.ForeignKey("seasons.year"), nullable=False)
    week = db.Column(db.Integer)
    chart_date = db.Column(db.Date, nullable=False)
    position = db.Column(db.String(16), index=True)
    depth_position = db.Column(db.String(24), index=True)
    depth_rank = db.Column(db.Integer, index=True)
    source_key = db.Column(db.String(80), nullable=False, default="nflverse")
    raw_payload = db.Column(db.JSON)
    __table_args__ = (
        db.UniqueConstraint("player_id", "team_id", "chart_date", "depth_position", name="uq_depth_chart_entry"),
        db.Index("ix_depth_chart_current", "season", "week", "team_id", "depth_position", "depth_rank"),
    )


class SnapCount(TimestampMixin, db.Model):
    __tablename__ = "snap_counts"
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey("games.id", ondelete="CASCADE"), nullable=False)
    player_id = db.Column(db.Integer, db.ForeignKey("players.id", ondelete="CASCADE"), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False)
    season = db.Column(db.Integer, nullable=False, index=True)
    week = db.Column(db.Integer, nullable=False, index=True)
    offense_snaps = db.Column(db.Integer, default=0)
    offense_pct = db.Column(db.Float)
    defense_snaps = db.Column(db.Integer, default=0)
    defense_pct = db.Column(db.Float)
    special_teams_snaps = db.Column(db.Integer, default=0)
    special_teams_pct = db.Column(db.Float)
    source_key = db.Column(db.String(80), nullable=False, default="nflverse")
    raw_payload = db.Column(db.JSON)
    __table_args__ = (
        db.UniqueConstraint("game_id", "player_id", name="uq_snap_count"),
        db.Index("ix_snap_player_season", "player_id", "season", "week"),
    )


class LeagueTransaction(TimestampMixin, db.Model):
    __tablename__ = "league_transactions"
    id = db.Column(db.Integer, primary_key=True)
    external_id = db.Column(db.String(120), nullable=False, unique=True, index=True)
    player_id = db.Column(db.Integer, db.ForeignKey("players.id", ondelete="SET NULL"), index=True)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id", ondelete="SET NULL"), index=True)
    transaction_date = db.Column(db.Date, nullable=False, index=True)
    transaction_type = db.Column(db.String(80), index=True)
    description = db.Column(db.Text)
    source_key = db.Column(db.String(80), nullable=False, default="nflverse")
    raw_payload = db.Column(db.JSON)

class WeatherObservation(TimestampMixin, db.Model):
    __tablename__ = "weather_observations"
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey("games.id", ondelete="CASCADE"), nullable=False, index=True)
    observed_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    temperature_f = db.Column(db.Float)
    feels_like_f = db.Column(db.Float)
    humidity_pct = db.Column(db.Float)
    pressure_hpa = db.Column(db.Float)
    wind_speed_mph = db.Column(db.Float)
    wind_gust_mph = db.Column(db.Float)
    wind_direction_deg = db.Column(db.Float)
    precipitation_mm = db.Column(db.Float)
    cloud_pct = db.Column(db.Float)
    condition = db.Column(db.String(80))
    source_key = db.Column(db.String(80), nullable=False, default="openweather")
    raw_payload = db.Column(db.JSON)
    __table_args__ = (
        db.UniqueConstraint("game_id", "observed_at", "source_key", name="uq_weather_observation"),
        db.Index("ix_weather_game_time", "game_id", "observed_at"),
    )


class OddsSnapshot(TimestampMixin, db.Model):
    __tablename__ = "odds_snapshots"
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey("games.id", ondelete="CASCADE"), nullable=False, index=True)
    provider_event_id = db.Column(db.String(120), index=True)
    bookmaker = db.Column(db.String(80), nullable=False, index=True)
    market = db.Column(db.String(40), nullable=False, index=True)
    outcome = db.Column(db.String(160), nullable=False)
    line = db.Column(db.Float)
    price_american = db.Column(db.Integer)
    captured_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    source_key = db.Column(db.String(80), nullable=False, default="the-odds-api")
    raw_payload = db.Column(db.JSON)
    __table_args__ = (
        db.UniqueConstraint("game_id", "bookmaker", "market", "outcome", "captured_at", name="uq_odds_snapshot"),
        db.Index("ix_odds_game_market_time", "game_id", "market", "captured_at"),
    )
