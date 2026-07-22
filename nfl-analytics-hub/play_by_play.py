"""Play-by-play ingestion and advanced team aggregation.

Accepted input: JSONL or CSV. Required fields: game_external_id, play_external_id,
sequence. Team fields may use abbreviations. Unknown optional fields are preserved.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from sqlalchemy import func

from database import db
from db_models import Game, Play, Team, TeamAdvancedSeasonStat


def _bool(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _num(value, cast=float):
    if value in (None, ""):
        return None
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


def _rows(path: Path):
    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    yield json.loads(line)
    else:
        with path.open(newline="", encoding="utf-8-sig") as fh:
            yield from csv.DictReader(fh)


def import_play_by_play(path: str) -> dict:
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(source)
    teams = {t.abbreviation: t for t in db.session.scalars(db.select(Team)).all()}
    games = {g.external_id: g for g in db.session.scalars(db.select(Game)).all()}
    read = written = skipped = 0
    for row in _rows(source):
        read += 1
        game = games.get(str(row.get("game_external_id") or row.get("game_id") or ""))
        external_id = str(row.get("play_external_id") or row.get("play_id") or "")
        sequence = _num(row.get("sequence") or row.get("play_sequence"), int)
        if not game or not external_id or sequence is None:
            skipped += 1
            continue
        offense = teams.get(str(row.get("offense_team") or "").upper())
        defense = teams.get(str(row.get("defense_team") or "").upper())
        play = db.session.scalar(db.select(Play).where(Play.external_id == external_id))
        if not play:
            play = Play(external_id=external_id, game_id=game.id, sequence=sequence)
            db.session.add(play)
        play.drive_id = row.get("drive_id")
        play.quarter = _num(row.get("quarter"), int)
        play.clock_seconds = _num(row.get("clock_seconds"), int)
        play.offense_team_id = offense.id if offense else None
        play.defense_team_id = defense.id if defense else None
        play.down = _num(row.get("down"), int)
        play.yards_to_go = _num(row.get("yards_to_go"), int)
        play.yard_line = _num(row.get("yard_line"), int)
        play.play_type = row.get("play_type")
        play.description = row.get("description")
        play.yards_gained = _num(row.get("yards_gained"))
        play.first_down = _bool(row.get("first_down"))
        play.touchdown = _bool(row.get("touchdown"))
        play.turnover = _bool(row.get("turnover"))
        play.expected_points_before = _num(row.get("expected_points_before"))
        play.expected_points_after = _num(row.get("expected_points_after"))
        play.epa = _num(row.get("epa"))
        play.success = _bool(row.get("success"))
        play.win_probability_before = _num(row.get("win_probability_before"))
        play.win_probability_after = _num(row.get("win_probability_after"))
        play.wpa = _num(row.get("wpa"))
        play.personnel = row.get("personnel")
        play.formation = row.get("formation")
        play.raw_payload = dict(row)
        written += 1
    db.session.commit()
    return {"records_read": read, "records_written": written, "records_skipped": skipped}


def rebuild_advanced_team_stats(season: int | None = None) -> dict:
    q = db.select(Game)
    if season is not None:
        q = q.where(Game.season == season)
    games = db.session.scalars(q).all()
    game_ids = [g.id for g in games]
    if not game_ids:
        return {"season": season, "rows": 0}
    game_map = {g.id: g for g in games}
    plays = db.session.scalars(db.select(Play).where(Play.game_id.in_(game_ids))).all()
    buckets = {}
    for p in plays:
        game = game_map[p.game_id]
        for team_id, side in ((p.offense_team_id, "offense"), (p.defense_team_id, "defense")):
            if not team_id:
                continue
            key = (team_id, game.season, game.season_type)
            b = buckets.setdefault(key, {"off": [], "def": []})
            b["off" if side == "offense" else "def"].append(p)
    rows = 0
    for (team_id, year, season_type), bucket in buckets.items():
        stat = db.session.scalar(db.select(TeamAdvancedSeasonStat).where(
            TeamAdvancedSeasonStat.team_id == team_id,
            TeamAdvancedSeasonStat.season == year,
            TeamAdvancedSeasonStat.season_type == season_type,
        ))
        if not stat:
            stat = TeamAdvancedSeasonStat(team_id=team_id, season=year, season_type=season_type)
            db.session.add(stat)
        off, deff = bucket["off"], bucket["def"]
        stat.offensive_plays, stat.defensive_plays = len(off), len(deff)
        stat.offensive_epa = sum(p.epa or 0 for p in off)
        stat.defensive_epa = sum(p.epa or 0 for p in deff)
        stat.offensive_epa_per_play = stat.offensive_epa / len(off) if off else None
        stat.defensive_epa_per_play = stat.defensive_epa / len(deff) if deff else None
        stat.offensive_success_rate = sum(bool(p.success) for p in off) / len(off) if off else None
        stat.defensive_success_rate = sum(bool(p.success) for p in deff) / len(deff) if deff else None
        early = [p for p in off if p.down in (1, 2)]
        third = [p for p in off if p.down == 3]
        red = [p for p in off if p.yard_line is not None and p.yard_line >= 80]
        stat.early_down_success_rate = sum(bool(p.success) for p in early) / len(early) if early else None
        stat.third_down_success_rate = sum(bool(p.success) for p in third) / len(third) if third else None
        stat.red_zone_success_rate = sum(bool(p.success) for p in red) / len(red) if red else None
        stat.explosive_play_rate = sum((p.yards_gained or 0) >= 20 for p in off) / len(off) if off else None
        rows += 1
    db.session.commit()
    return {"season": season, "rows": rows, "plays": len(plays)}
