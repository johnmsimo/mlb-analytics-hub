"""External provider integrations.

The default implementation uses nflverse public release assets. Commercial
providers can be added behind the same interface without changing warehouse
models or admin APIs.
"""
from __future__ import annotations

import csv
import gzip
import io
import os
from datetime import date, datetime, timezone
from typing import Iterable

import requests

from database import db
from db_models import (DataSource, DataSyncRun, DepthChartEntry, Game, InjuryReport,
                       Player, PlayerTeamSeason, Season, SnapCount, Team)
from source_registry import capture_raw, register_source

NFLVERSE_BASE = "https://github.com/nflverse/nflverse-data/releases/download"
TEAM_ALIASES = {"JAX": "JAC", "LA": "LAR", "STL": "LAR", "SD": "LAC", "OAK": "LV"}


def _team(value):
    key = str(value or "").strip().upper()
    return TEAM_ALIASES.get(key, key)


def _int(value):
    try:
        return int(float(value)) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _float(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _download_csv(url: str) -> Iterable[dict]:
    timeout = float(os.environ.get("EXTERNAL_DATA_TIMEOUT", "60"))
    with requests.get(url, timeout=timeout, stream=True, headers={"User-Agent": "nfl-analytics-hub/1.0"}) as response:
        response.raise_for_status()
        raw = response.content
    if url.endswith(".gz"):
        raw = gzip.decompress(raw)
    yield from csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))


def _source() -> DataSource:
    return register_source(
        "nflverse",
        "nflverse public NFL datasets",
        source_type="http",
        base_url="https://github.com/nflverse/nflverse-data/releases",
        license_name="CC-BY-4.0 and upstream source terms",
        attribution="Data provided through nflverse; preserve dataset-specific attribution.",
        refresh_interval_minutes=60,
        metadata={"provider": "nflverse", "format": "csv"},
    )


def _start_run(dataset: str, season: int | None) -> DataSyncRun:
    run = DataSyncRun(source=f"nflverse:{dataset}", details={"dataset": dataset, "season": season})
    db.session.add(run)
    db.session.flush()
    return run


def _finish(run, source, read, written, error=None):
    run.records_read = read
    run.records_written = written
    run.finished_at = datetime.now(timezone.utc)
    run.status = "failed" if error else "success"
    run.error = str(error) if error else None
    if error:
        source.last_failure_at = run.finished_at
    else:
        source.last_success_at = run.finished_at
    db.session.commit()


def _game_lookup(season: int):
    teams = {t.abbreviation: t for t in db.session.scalars(db.select(Team)).all()}
    games = db.session.scalars(db.select(Game).where(Game.season == season)).all()
    by_matchup = {}
    for g in games:
        home = db.session.get(Team, g.home_team_id)
        away = db.session.get(Team, g.away_team_id)
        if home and away:
            by_matchup[(g.week, away.abbreviation, home.abbreviation)] = g
    return teams, by_matchup


def sync_pbp(season: int) -> dict:
    """Import nflverse play-by-play for a season directly into the Play table."""
    from db_models import Play
    source = _source(); run = _start_run("pbp", season)
    read = written = skipped = 0
    url = f"{NFLVERSE_BASE}/pbp/play_by_play_{season}.csv.gz"
    try:
        teams, games = _game_lookup(season)
        for row in _download_csv(url):
            read += 1
            week = _int(row.get("week"))
            game = games.get((week, _team(row.get("away_team")), _team(row.get("home_team"))))
            play_id = str(row.get("play_id") or "")
            if not game or not play_id:
                skipped += 1; continue
            external_id = f"nflverse:{row.get('game_id')}:{play_id}"
            play = db.session.scalar(db.select(Play).where(Play.external_id == external_id))
            if not play:
                play = Play(external_id=external_id, game_id=game.id, sequence=_int(play_id) or read)
                db.session.add(play)
            posteam, defteam = teams.get(_team(row.get("posteam"))), teams.get(_team(row.get("defteam")))
            play.drive_id = str(row.get("drive") or "") or None
            play.quarter = _int(row.get("qtr"))
            play.clock_seconds = _int(row.get("quarter_seconds_remaining"))
            play.offense_team_id = posteam.id if posteam else None
            play.defense_team_id = defteam.id if defteam else None
            play.down = _int(row.get("down")); play.yards_to_go = _int(row.get("ydstogo"))
            y100 = _int(row.get("yardline_100")); play.yard_line = 100 - y100 if y100 is not None else None
            play.play_type = row.get("play_type"); play.description = row.get("desc")
            play.yards_gained = _float(row.get("yards_gained"))
            play.first_down = str(row.get("first_down") or "0") == "1"
            play.touchdown = str(row.get("touchdown") or "0") == "1"
            play.turnover = str(row.get("interception") or "0") == "1" or str(row.get("fumble_lost") or "0") == "1"
            play.expected_points_before = _float(row.get("ep")); play.expected_points_after = _float(row.get("ep_after"))
            play.epa = _float(row.get("epa")); play.success = str(row.get("success") or "0") == "1"
            play.win_probability_before = _float(row.get("wp")); play.win_probability_after = _float(row.get("vegas_wp"))
            play.wpa = _float(row.get("wpa")); play.personnel = row.get("offense_personnel"); play.formation = row.get("offense_formation")
            play.raw_payload = row
            capture_raw(source, "play", external_id, row, season=season, week=week)
            written += 1
            if written % 5000 == 0:
                db.session.commit()
        db.session.commit(); _finish(run, source, read, written)
        return {"provider": "nflverse", "dataset": "pbp", "season": season, "read": read, "written": written, "skipped": skipped, "url": url}
    except Exception as exc:
        db.session.rollback(); source = _source(); run = db.session.get(DataSyncRun, run.id) or _start_run("pbp", season); _finish(run, source, read, written, exc)
        raise


def _load_nflreadpy(dataset: str, season: int):
    try:
        import nflreadpy as nfl
    except ImportError as exc:
        raise RuntimeError("nflreadpy is required for rosters, injuries, depth charts, and snap counts") from exc
    fn = {
        "rosters": nfl.load_rosters_weekly,
        "injuries": nfl.load_injuries,
        "depth_charts": nfl.load_depth_charts,
        "snap_counts": nfl.load_snap_counts,
    }[dataset]
    frame = fn([season])
    return frame.to_dicts() if hasattr(frame, "to_dicts") else frame.to_dict("records")


def _ensure_player(row) -> Player | None:
    ext = str(row.get("gsis_id") or row.get("player_id") or row.get("nflverse_id") or "").strip()
    name = str(row.get("full_name") or row.get("player_name") or row.get("name") or "").strip()
    if not ext or not name:
        return None
    player = db.session.scalar(db.select(Player).where(Player.external_id == ext))
    if not player:
        player = Player(external_id=ext, full_name=name)
        db.session.add(player); db.session.flush()
    player.full_name = name; player.position = row.get("position") or player.position
    return player


def sync_rosters(season: int) -> dict:
    source = _source(); run = _start_run("rosters", season); rows = _load_nflreadpy("rosters", season)
    teams = {t.abbreviation: t for t in db.session.scalars(db.select(Team)).all()}; read = written = 0
    try:
        if not db.session.get(Season, season): db.session.add(Season(year=season))
        for row in rows:
            read += 1; player = _ensure_player(row); team = teams.get(_team(row.get("team")))
            if not player or not team: continue
            link = db.session.scalar(db.select(PlayerTeamSeason).where(PlayerTeamSeason.player_id == player.id, PlayerTeamSeason.team_id == team.id, PlayerTeamSeason.season == season))
            if not link:
                link = PlayerTeamSeason(player_id=player.id, team_id=team.id, season=season); db.session.add(link)
            link.jersey_number = str(row.get("jersey_number") or "") or None; link.depth_position = row.get("depth_chart_position"); link.status = row.get("status")
            capture_raw(source, "roster", f"{season}:{team.abbreviation}:{player.external_id}:{row.get('week')}", row, season=season, week=_int(row.get("week")))
            written += 1
        db.session.commit(); _finish(run, source, read, written); return {"dataset": "rosters", "season": season, "read": read, "written": written}
    except Exception as exc:
        db.session.rollback(); _finish(run, source, read, written, exc); raise


def sync_injuries(season: int) -> dict:
    source = _source(); run = _start_run("injuries", season); rows = _load_nflreadpy("injuries", season)
    teams = {t.abbreviation: t for t in db.session.scalars(db.select(Team)).all()}; read = written = 0
    try:
        for row in rows:
            read += 1; player = _ensure_player(row); team = teams.get(_team(row.get("team"))); report_date = _date(row.get("report_date") or row.get("date_modified") or row.get("date"))
            week = _int(row.get("week"))
            if not player or not team or not report_date or week is None: continue
            item = db.session.scalar(db.select(InjuryReport).where(InjuryReport.player_id == player.id, InjuryReport.team_id == team.id, InjuryReport.season == season, InjuryReport.week == week, InjuryReport.report_date == report_date))
            if not item: item = InjuryReport(player_id=player.id, team_id=team.id, season=season, week=week, report_date=report_date); db.session.add(item)
            item.game_status = row.get("report_status") or row.get("game_status"); item.practice_status = row.get("practice_status"); item.primary_injury = row.get("primary_injury") or row.get("injury"); item.secondary_injury = row.get("secondary_injury"); item.raw_payload = row
            capture_raw(source, "injury", f"{season}:{week}:{team.abbreviation}:{player.external_id}:{report_date}", row, season=season, week=week); written += 1
        db.session.commit(); _finish(run, source, read, written); return {"dataset": "injuries", "season": season, "read": read, "written": written}
    except Exception as exc:
        db.session.rollback(); _finish(run, source, read, written, exc); raise


def sync_depth_charts(season: int) -> dict:
    source = _source(); run = _start_run("depth_charts", season); rows = _load_nflreadpy("depth_charts", season)
    teams = {t.abbreviation: t for t in db.session.scalars(db.select(Team)).all()}; read = written = 0
    try:
        for row in rows:
            read += 1; player = _ensure_player(row); team = teams.get(_team(row.get("team"))); chart_date = _date(row.get("dt") or row.get("date") or row.get("chart_date"))
            if not player or not team or not chart_date: continue
            depth_pos = row.get("pos_abb") or row.get("depth_position") or row.get("position")
            item = db.session.scalar(db.select(DepthChartEntry).where(DepthChartEntry.player_id == player.id, DepthChartEntry.team_id == team.id, DepthChartEntry.chart_date == chart_date, DepthChartEntry.depth_position == depth_pos))
            if not item: item = DepthChartEntry(player_id=player.id, team_id=team.id, season=season, chart_date=chart_date, depth_position=depth_pos); db.session.add(item)
            item.week = _int(row.get("week")); item.position = row.get("position"); item.depth_rank = _int(row.get("pos_rank") or row.get("depth_rank")); item.raw_payload = row
            capture_raw(source, "depth_chart", f"{team.abbreviation}:{player.external_id}:{chart_date}:{depth_pos}", row, season=season, week=item.week); written += 1
        db.session.commit(); _finish(run, source, read, written); return {"dataset": "depth_charts", "season": season, "read": read, "written": written}
    except Exception as exc:
        db.session.rollback(); _finish(run, source, read, written, exc); raise


def sync_snap_counts(season: int) -> dict:
    source = _source(); run = _start_run("snap_counts", season); rows = _load_nflreadpy("snap_counts", season)
    teams, games = _game_lookup(season); read = written = 0
    try:
        for row in rows:
            read += 1; player = _ensure_player(row); team = teams.get(_team(row.get("team"))); week = _int(row.get("week"))
            game = games.get((week, _team(row.get("opponent") if row.get("location") == "Home" else row.get("team")), _team(row.get("team") if row.get("location") == "Home" else row.get("opponent"))))
            if not player or not team or not game or week is None: continue
            item = db.session.scalar(db.select(SnapCount).where(SnapCount.game_id == game.id, SnapCount.player_id == player.id))
            if not item: item = SnapCount(game_id=game.id, player_id=player.id, team_id=team.id, season=season, week=week); db.session.add(item)
            item.offense_snaps = _int(row.get("offense_snaps")) or 0; item.offense_pct = _float(row.get("offense_pct"))
            item.defense_snaps = _int(row.get("defense_snaps")) or 0; item.defense_pct = _float(row.get("defense_pct"))
            item.special_teams_snaps = _int(row.get("st_snaps") or row.get("special_teams_snaps")) or 0; item.special_teams_pct = _float(row.get("st_pct") or row.get("special_teams_pct")); item.raw_payload = row
            capture_raw(source, "snap_count", f"{game.external_id}:{player.external_id}", row, season=season, week=week); written += 1
        db.session.commit(); _finish(run, source, read, written); return {"dataset": "snap_counts", "season": season, "read": read, "written": written}
    except Exception as exc:
        db.session.rollback(); _finish(run, source, read, written, exc); raise


def sync_external(season: int, datasets: list[str]) -> dict:
    funcs = {"pbp": sync_pbp, "rosters": sync_rosters, "injuries": sync_injuries, "depth_charts": sync_depth_charts, "snap_counts": sync_snap_counts}
    result = {}
    for dataset in datasets:
        if dataset not in funcs: raise ValueError(f"unsupported dataset: {dataset}")
        result[dataset] = funcs[dataset](season)
    return result
