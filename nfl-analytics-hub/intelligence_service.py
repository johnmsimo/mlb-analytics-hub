"""Version 2.0 intelligence services backed by the normalized warehouse."""
from __future__ import annotations
from datetime import datetime, timezone
from statistics import mean
from database import db
from db_models import (DataSource, DataSyncRun, Game, InjuryReport, OddsSnapshot,
                       Player, Play, Prediction, Team, TeamAdvancedSeasonStat,
                       TeamSeasonStat, WeatherObservation)


def _team(abbr: str) -> Team | None:
    return db.session.scalar(db.select(Team).where(Team.abbreviation == abbr.upper()))


def _latest_source_status():
    now = datetime.now(timezone.utc)
    rows = db.session.scalars(db.select(DataSource).order_by(DataSource.key)).all()
    out = []
    for row in rows:
        last = row.last_success_at
        age = None
        if last:
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            age = round((now-last).total_seconds()/60, 1)
        expected = row.refresh_interval_minutes or 1440
        status = "never" if age is None else ("fresh" if age <= expected*1.5 else "stale")
        out.append({"key":row.key,"name":row.name,"enabled":row.enabled,
                    "last_success_at":last.isoformat() if last else None,
                    "age_minutes":age,"expected_minutes":expected,"status":status})
    return out


def platform_health():
    sources = _latest_source_status()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": sources,
        "summary": {
            "total": len(sources),
            "fresh": sum(x["status"] == "fresh" for x in sources),
            "stale": sum(x["status"] == "stale" for x in sources),
            "never": sum(x["status"] == "never" for x in sources),
        },
        "latest_runs": [{"source":r.source,"status":r.status,"finished_at":r.finished_at.isoformat() if r.finished_at else None,
                         "records_written":r.records_written,"error":r.error}
                        for r in db.session.scalars(db.select(DataSyncRun).order_by(DataSyncRun.id.desc()).limit(10)).all()]
    }


def team_intelligence(abbr: str, season: int):
    team = _team(abbr)
    if not team:
        return None
    basic = db.session.scalar(db.select(TeamSeasonStat).where(
        TeamSeasonStat.team_id == team.id, TeamSeasonStat.season == season,
        TeamSeasonStat.season_type == "REG"))
    advanced = db.session.scalar(db.select(TeamAdvancedSeasonStat).where(
        TeamAdvancedSeasonStat.team_id == team.id, TeamAdvancedSeasonStat.season == season,
        TeamAdvancedSeasonStat.season_type == "REG"))
    injuries = db.session.scalars(db.select(InjuryReport).where(
        InjuryReport.team_id == team.id, InjuryReport.season == season).order_by(InjuryReport.report_date.desc()).limit(25)).all()
    games = db.session.scalars(db.select(Game).where(
        Game.season == season, db.or_(Game.home_team_id == team.id, Game.away_team_id == team.id)
    ).order_by(Game.week.desc()).limit(20)).all()
    recent = []
    for g in games:
        is_home = g.home_team_id == team.id
        pf = g.home_score if is_home else g.away_score
        pa = g.away_score if is_home else g.home_score
        recent.append({"game_id":g.external_id,"week":g.week,"completed":g.completed,"points_for":pf,"points_against":pa})
    strengths, concerns = [], []
    if advanced:
        if (advanced.offensive_epa_per_play or 0) > .05: strengths.append("Positive offensive EPA per play")
        if (advanced.defensive_epa_per_play or 0) < -.03: strengths.append("Above-average defensive EPA suppression")
        if (advanced.offensive_success_rate or 0) > .46: strengths.append("Sustainable offensive success rate")
        if (advanced.third_down_success_rate or 0) < .35: concerns.append("Third-down efficiency is a concern")
        if (advanced.explosive_play_rate or 0) < .08: concerns.append("Limited explosive-play production")
    active_injuries = [i for i in injuries if (i.game_status or "").lower() not in {"", "available", "full"}]
    if len(active_injuries) >= 5: concerns.append("Elevated current injury volume")
    return {
        "team":{"id":team.id,"abbreviation":team.abbreviation,"name":team.name,"conference":team.conference,"division":team.division},
        "season":season,
        "record": None if not basic else {"games":basic.games,"wins":basic.wins,"losses":basic.losses,"ties":basic.ties,"win_pct":basic.win_pct,"ppg":basic.ppg,"papg":basic.papg,"point_differential":basic.point_differential},
        "advanced": None if not advanced else {c:getattr(advanced,c) for c in ["offensive_plays","defensive_plays","offensive_epa_per_play","defensive_epa_per_play","offensive_success_rate","defensive_success_rate","early_down_success_rate","third_down_success_rate","red_zone_success_rate","explosive_play_rate"]},
        "injury_count":len(active_injuries),
        "injuries":[{"player_id":i.player_id,"status":i.game_status,"practice":i.practice_status,"injury":i.primary_injury,"week":i.week,"date":i.report_date.isoformat()} for i in active_injuries[:12]],
        "recent_games":recent[:8],"strengths":strengths,"concerns":concerns,
        "generated_at":datetime.now(timezone.utc).isoformat(),
    }


def matchup_intelligence(game_id: str):
    game = db.session.scalar(db.select(Game).where(Game.external_id == game_id))
    if not game:
        return None
    home, away = db.session.get(Team, game.home_team_id), db.session.get(Team, game.away_team_id)
    hs, as_ = team_intelligence(home.abbreviation, game.season), team_intelligence(away.abbreviation, game.season)
    def rating(report):
        rec = report.get("record") or {}; adv = report.get("advanced") or {}
        return (rec.get("win_pct") or .5)*50 + (adv.get("offensive_epa_per_play") or 0)*100 - (adv.get("defensive_epa_per_play") or 0)*100 - report.get("injury_count",0)*.5
    hr, ar = rating(hs), rating(as_)
    home_prob = max(.08, min(.92, .5 + (hr-ar)/100 + .025))
    odds = db.session.scalars(db.select(OddsSnapshot).where(OddsSnapshot.game_id == game.id).order_by(OddsSnapshot.captured_at.desc()).limit(30)).all()
    weather = db.session.scalar(db.select(WeatherObservation).where(WeatherObservation.game_id == game.id).order_by(WeatherObservation.observed_at.desc()).limit(1))
    prediction = db.session.scalar(db.select(Prediction).where(Prediction.game_id == game.id).order_by(Prediction.created_at.desc()).limit(1))
    return {"game":{"id":game.external_id,"season":game.season,"week":game.week,"kickoff_at":game.kickoff_at.isoformat() if game.kickoff_at else None,"venue":game.venue,"home":home.abbreviation,"away":away.abbreviation},
            "home":hs,"away":as_,"model":{"home_win_probability":round(home_prob,4),"away_win_probability":round(1-home_prob,4),"confidence":round(min(.95,.55+abs(home_prob-.5)),3),"stored_prediction":prediction.predicted_value if prediction else None},
            "weather":None if not weather else {"temperature_f":weather.temperature_f,"wind_mph":weather.wind_speed_mph,"condition":weather.condition,"observed_at":weather.observed_at.isoformat()},
            "market":{"snapshot_count":len(odds),"latest_captured_at":odds[0].captured_at.isoformat() if odds else None},
            "generated_at":datetime.now(timezone.utc).isoformat()}


def live_games(season: int, week: int | None = None):
    q = db.select(Game).where(Game.season == season)
    if week is not None: q = q.where(Game.week == week)
    games = db.session.scalars(q.order_by(Game.kickoff_at)).all()
    out=[]
    for g in games:
        home, away = db.session.get(Team,g.home_team_id),db.session.get(Team,g.away_team_id)
        last_play=db.session.scalar(db.select(Play).where(Play.game_id==g.id).order_by(Play.sequence.desc()).limit(1))
        out.append({"id":g.external_id,"week":g.week,"state":g.state,"completed":g.completed,"kickoff_at":g.kickoff_at.isoformat() if g.kickoff_at else None,
                    "home":{"team":home.abbreviation,"score":g.home_score},"away":{"team":away.abbreviation,"score":g.away_score},
                    "last_play":None if not last_play else {"sequence":last_play.sequence,"quarter":last_play.quarter,"clock_seconds":last_play.clock_seconds,"description":last_play.description,"epa":last_play.epa,"wpa":last_play.wpa}})
    return out
