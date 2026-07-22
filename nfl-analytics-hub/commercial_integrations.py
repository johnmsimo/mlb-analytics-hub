"""Credentialed integrations for weather, odds, coaches and transactions.

All connectors are opt-in and fail closed when credentials are absent. Provider
responses are normalized into warehouse models while retaining raw payloads.
"""
from __future__ import annotations
import os
from datetime import datetime, timezone, date
from urllib.parse import quote
import requests
from database import db
from db_models import (Coach, CoachingAssignment, DataSyncRun, Game, LeagueTransaction,
                       OddsSnapshot, Player, Season, Team, WeatherObservation)
from source_registry import capture_raw, register_source
from external_providers import _date, _float, _int, _team, _ensure_player

STADIUM_COORDS = {
 "State Farm Stadium": (33.5276,-112.2626), "Mercedes-Benz Stadium": (33.7554,-84.4008),
 "M&T Bank Stadium": (39.2780,-76.6227), "Highmark Stadium": (42.7738,-78.7870),
 "Bank of America Stadium": (35.2258,-80.8528), "Soldier Field": (41.8623,-87.6167),
 "Paycor Stadium": (39.0955,-84.5161), "Cleveland Browns Stadium": (41.5061,-81.6995),
 "AT&T Stadium": (32.7473,-97.0945), "Empower Field at Mile High": (39.7439,-105.0201),
 "Ford Field": (42.3400,-83.0456), "Lambeau Field": (44.5013,-88.0622),
 "NRG Stadium": (29.6847,-95.4107), "Lucas Oil Stadium": (39.7601,-86.1639),
 "EverBank Stadium": (30.3239,-81.6373), "GEHA Field at Arrowhead Stadium": (39.0489,-94.4839),
 "Allegiant Stadium": (36.0908,-115.1830), "SoFi Stadium": (33.9535,-118.3392),
 "Hard Rock Stadium": (25.9580,-80.2389), "U.S. Bank Stadium": (44.9736,-93.2575),
 "Gillette Stadium": (42.0909,-71.2643), "Caesars Superdome": (29.9511,-90.0812),
 "MetLife Stadium": (40.8135,-74.0745), "Lincoln Financial Field": (39.9008,-75.1675),
 "Acrisure Stadium": (40.4468,-80.0158), "Levi's Stadium": (37.4030,-121.9700),
 "Lumen Field": (47.5952,-122.3316), "Raymond James Stadium": (27.9759,-82.5033),
 "Nissan Stadium": (36.1665,-86.7713), "Northwest Stadium": (38.9078,-76.8645),
}
ALIASES = {"Arizona Cardinals":"ARI","Atlanta Falcons":"ATL","Baltimore Ravens":"BAL","Buffalo Bills":"BUF","Carolina Panthers":"CAR","Chicago Bears":"CHI","Cincinnati Bengals":"CIN","Cleveland Browns":"CLE","Dallas Cowboys":"DAL","Denver Broncos":"DEN","Detroit Lions":"DET","Green Bay Packers":"GB","Houston Texans":"HOU","Indianapolis Colts":"IND","Jacksonville Jaguars":"JAC","Kansas City Chiefs":"KC","Las Vegas Raiders":"LV","Los Angeles Chargers":"LAC","Los Angeles Rams":"LAR","Miami Dolphins":"MIA","Minnesota Vikings":"MIN","New England Patriots":"NE","New Orleans Saints":"NO","New York Giants":"NYG","New York Jets":"NYJ","Philadelphia Eagles":"PHI","Pittsburgh Steelers":"PIT","San Francisco 49ers":"SF","Seattle Seahawks":"SEA","Tampa Bay Buccaneers":"TB","Tennessee Titans":"TEN","Washington Commanders":"WAS"}

def _source(key,name,url,minutes):
    return register_source(key,name,source_type="http",base_url=url,refresh_interval_minutes=minutes,metadata={"credentialed":True})

def _run(source, details):
    r=DataSyncRun(source=source,details=details); db.session.add(r); db.session.flush(); return r

def _finish(run, source, read, written, exc=None):
    now=datetime.now(timezone.utc); run.records_read=read; run.records_written=written; run.finished_at=now
    run.status="failed" if exc else "success"; run.error=str(exc) if exc else None
    if exc: source.last_failure_at=now
    else: source.last_success_at=now
    db.session.commit()

def _json_get(url, headers=None, params=None):
    timeout=float(os.getenv("EXTERNAL_DATA_TIMEOUT","60"))
    resp=requests.get(url,headers=headers or {},params=params or {},timeout=timeout)
    resp.raise_for_status(); return resp.json()

def _games(season, week=None):
    q=db.select(Game).where(Game.season==season)
    if week is not None: q=q.where(Game.week==week)
    return db.session.scalars(q).all()

def _team_map(): return {t.abbreviation:t for t in db.session.scalars(db.select(Team)).all()}

def _game_by_teams(season, away, home):
    tm=_team_map(); a=tm.get(_team(away)); h=tm.get(_team(home))
    if not a or not h: return None
    return db.session.scalar(db.select(Game).where(Game.season==season,Game.away_team_id==a.id,Game.home_team_id==h.id).order_by(Game.kickoff_at.desc()).limit(1))

def sync_weather(season:int, week:int|None=None):
    key=os.getenv("OPENWEATHER_API_KEY")
    if not key: raise RuntimeError("OPENWEATHER_API_KEY is required")
    source=_source("openweather","OpenWeather current conditions","https://api.openweathermap.org/data/2.5/weather",30)
    run=_run("openweather:weather",{"season":season,"week":week}); read=written=0
    try:
        for game in _games(season,week):
            coords=STADIUM_COORDS.get(game.venue or "")
            if not coords: continue
            payload=_json_get(source.base_url,params={"lat":coords[0],"lon":coords[1],"appid":key,"units":"imperial"}); read+=1
            observed=datetime.fromtimestamp(payload.get("dt",datetime.now().timestamp()),tz=timezone.utc)
            row=db.session.scalar(db.select(WeatherObservation).where(WeatherObservation.game_id==game.id,WeatherObservation.observed_at==observed,WeatherObservation.source_key==source.key))
            if not row: row=WeatherObservation(game_id=game.id,observed_at=observed,source_key=source.key); db.session.add(row)
            main=payload.get("main") or {}; wind=payload.get("wind") or {}; weather=(payload.get("weather") or [{}])[0]
            row.temperature_f=_float(main.get("temp")); row.feels_like_f=_float(main.get("feels_like")); row.humidity_pct=_float(main.get("humidity")); row.pressure_hpa=_float(main.get("pressure")); row.wind_speed_mph=_float(wind.get("speed")); row.wind_gust_mph=_float(wind.get("gust")); row.wind_direction_deg=_float(wind.get("deg")); row.cloud_pct=_float((payload.get("clouds") or {}).get("all")); row.precipitation_mm=_float((payload.get("rain") or {}).get("1h") or (payload.get("snow") or {}).get("1h")); row.condition=weather.get("description"); row.raw_payload=payload
            capture_raw(source,"weather",f"{game.external_id}:{int(observed.timestamp())}",payload,season=season,week=game.week); written+=1
        db.session.commit(); _finish(run,source,read,written); return {"dataset":"weather","read":read,"written":written}
    except Exception as exc: db.session.rollback(); _finish(run,source,read,written,exc); raise

def sync_odds(season:int, week:int|None=None):
    key=os.getenv("ODDS_API_KEY")
    if not key: raise RuntimeError("ODDS_API_KEY is required")
    url=os.getenv("ODDS_API_URL","https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds")
    source=_source("the-odds-api","The Odds API NFL markets",url,10); run=_run("the-odds-api:odds",{"season":season,"week":week}); read=written=0
    try:
        payload=_json_get(url,params={"apiKey":key,"regions":os.getenv("ODDS_REGIONS","us"),"markets":os.getenv("ODDS_MARKETS","h2h,spreads,totals"),"oddsFormat":"american"})
        captured=datetime.now(timezone.utc)
        for event in payload:
            read+=1; away=ALIASES.get(event.get("away_team"),event.get("away_team")); home=ALIASES.get(event.get("home_team"),event.get("home_team")); game=_game_by_teams(season,away,home)
            if not game: continue
            for book in event.get("bookmakers",[]):
                for market in book.get("markets",[]):
                    ts=market.get("last_update") or book.get("last_update"); at=datetime.fromisoformat(ts.replace("Z","+00:00")) if ts else captured
                    for out in market.get("outcomes",[]):
                        q=db.select(OddsSnapshot).where(OddsSnapshot.game_id==game.id,OddsSnapshot.bookmaker==book.get("key"),OddsSnapshot.market==market.get("key"),OddsSnapshot.outcome==out.get("name"),OddsSnapshot.captured_at==at)
                        row=db.session.scalar(q)
                        if not row: row=OddsSnapshot(game_id=game.id,bookmaker=book.get("key") or "unknown",market=market.get("key") or "unknown",outcome=out.get("name") or "unknown",captured_at=at); db.session.add(row)
                        row.provider_event_id=event.get("id"); row.line=_float(out.get("point")); row.price_american=_int(out.get("price")); row.raw_payload=out; written+=1
            capture_raw(source,"odds_event",event.get("id") or f"{away}:{home}:{captured.isoformat()}",event,season=season,week=game.week)
        db.session.commit(); _finish(run,source,read,written); return {"dataset":"odds","read":read,"written":written}
    except Exception as exc: db.session.rollback(); _finish(run,source,read,written,exc); raise

def _sportsdata(path_env, default_path, season):
    key=os.getenv("SPORTSDATAIO_API_KEY")
    if not key: raise RuntimeError("SPORTSDATAIO_API_KEY is required")
    base=os.getenv("SPORTSDATAIO_BASE_URL","https://api.sportsdata.io/v3/nfl")
    path=os.getenv(path_env,default_path).format(season=season)
    return _json_get(base.rstrip("/")+"/"+path.lstrip("/"),headers={"Ocp-Apim-Subscription-Key":key})

def sync_coaches(season:int):
    source=_source("sportsdataio","SportsDataIO NFL feeds",os.getenv("SPORTSDATAIO_BASE_URL","https://api.sportsdata.io/v3/nfl"),1440); run=_run("sportsdataio:coaches",{"season":season}); read=written=0
    try:
        rows=_sportsdata("SPORTSDATAIO_COACHES_PATH","scores/json/TeamSeasonStats/{season}",season); teams=_team_map()
        if not db.session.get(Season,season): db.session.add(Season(year=season))
        for row in rows:
            team=teams.get(_team(row.get("Team") or row.get("Key"))); names=[("Head Coach",row.get("HeadCoach") or row.get("HeadCoachName")),("Offensive Coordinator",row.get("OffensiveCoordinator")),("Defensive Coordinator",row.get("DefensiveCoordinator"))]
            if not team: continue
            for role,name in names:
                if not name: continue
                read+=1; ext=f"sportsdataio:{role}:{name}"; coach=db.session.scalar(db.select(Coach).where(Coach.external_id==ext))
                if not coach: coach=Coach(external_id=ext,full_name=name); db.session.add(coach); db.session.flush()
                assignment=db.session.scalar(db.select(CoachingAssignment).where(CoachingAssignment.coach_id==coach.id,CoachingAssignment.team_id==team.id,CoachingAssignment.season==season,CoachingAssignment.role==role))
                if not assignment: db.session.add(CoachingAssignment(coach_id=coach.id,team_id=team.id,season=season,role=role)); written+=1
                capture_raw(source,"coach_assignment",f"{season}:{team.abbreviation}:{role}",row,season=season)
        db.session.commit(); _finish(run,source,read,written); return {"dataset":"coaches","read":read,"written":written}
    except Exception as exc: db.session.rollback(); _finish(run,source,read,written,exc); raise

def sync_transactions(season:int):
    source=_source("sportsdataio","SportsDataIO NFL feeds",os.getenv("SPORTSDATAIO_BASE_URL","https://api.sportsdata.io/v3/nfl"),60); run=_run("sportsdataio:transactions",{"season":season}); read=written=0
    try:
        rows=_sportsdata("SPORTSDATAIO_TRANSACTIONS_PATH","scores/json/Transactions/{season}",season); teams=_team_map()
        for row in rows:
            read+=1; ext=str(row.get("TransactionID") or row.get("TransactionId") or row.get("Id") or ""); d=_date(row.get("Date") or row.get("TransactionDate"))
            if not ext or not d: continue
            item=db.session.scalar(db.select(LeagueTransaction).where(LeagueTransaction.external_id==f"sportsdataio:{ext}"))
            if not item: item=LeagueTransaction(external_id=f"sportsdataio:{ext}",transaction_date=d); db.session.add(item); written+=1
            player=_ensure_player({"player_id":row.get("PlayerID") or row.get("PlayerId"),"player_name":row.get("Name") or row.get("PlayerName")})
            team=teams.get(_team(row.get("Team") or row.get("TeamKey"))); item.player_id=player.id if player else None; item.team_id=team.id if team else None; item.transaction_type=row.get("Type") or row.get("TransactionType"); item.description=row.get("Description"); item.source_key=source.key; item.raw_payload=row
            capture_raw(source,"transaction",ext,row,season=season)
        db.session.commit(); _finish(run,source,read,written); return {"dataset":"transactions","read":read,"written":written}
    except Exception as exc: db.session.rollback(); _finish(run,source,read,written,exc); raise

def sync_commercial(season:int,datasets:list[str],week:int|None=None):
    funcs={"weather":lambda:sync_weather(season,week),"odds":lambda:sync_odds(season,week),"coaches":lambda:sync_coaches(season),"transactions":lambda:sync_transactions(season),"live_games":lambda:sync_live_games(season,week)}
    result={}
    for name in datasets:
        if name not in funcs: raise ValueError(f"unsupported commercial dataset: {name}")
        result[name]=funcs[name]()
    return result


def _nws_get(url):
    user_agent = os.getenv("NWS_USER_AGENT")
    if not user_agent:
        raise RuntimeError("NWS_USER_AGENT is required (example: nfl-analytics-hub/2.0 contact@example.com)")
    return _json_get(url, headers={"User-Agent": user_agent, "Accept": "application/geo+json"})


def sync_weather_nws(season:int, week:int|None=None):
    """Import the nearest hourly NWS forecast period for each scheduled game.

    NWS is keyless, but requires an identifying User-Agent. Indoor venues are
    still recorded when coordinates are known so downstream models can decide
    whether to ignore outdoor conditions.
    """
    source=_source("nws","National Weather Service hourly forecast","https://api.weather.gov",30)
    run=_run("nws:weather",{"season":season,"week":week}); read=written=0
    try:
        for game in _games(season,week):
            coords=STADIUM_COORDS.get(game.venue or "")
            if not coords: continue
            point=_nws_get(f"https://api.weather.gov/points/{coords[0]},{coords[1]}"); read+=1
            hourly=(point.get("properties") or {}).get("forecastHourly")
            if not hourly: continue
            forecast=_nws_get(hourly); read+=1
            periods=(forecast.get("properties") or {}).get("periods") or []
            if not periods: continue
            kickoff=game.kickoff_at or datetime.now(timezone.utc)
            def distance(p):
                try: return abs((datetime.fromisoformat(p["startTime"].replace("Z","+00:00"))-kickoff).total_seconds())
                except Exception: return 10**18
            p=min(periods,key=distance)
            observed=datetime.fromisoformat(p["startTime"].replace("Z","+00:00"))
            row=db.session.scalar(db.select(WeatherObservation).where(WeatherObservation.game_id==game.id,WeatherObservation.observed_at==observed,WeatherObservation.source_key==source.key))
            if not row:
                row=WeatherObservation(game_id=game.id,observed_at=observed,source_key=source.key); db.session.add(row)
            row.temperature_f=_float(p.get("temperature")); row.wind_speed_mph=_float(str(p.get("windSpeed") or "").split()[0]); row.wind_direction_deg=None
            row.precipitation_mm=None; row.condition=p.get("shortForecast"); row.raw_payload=p
            capture_raw(source,"weather",f"{game.external_id}:{p.get('number')}",p,season=season,week=game.week); written+=1
        db.session.commit(); _finish(run,source,read,written); return {"dataset":"weather","provider":"nws","read":read,"written":written}
    except Exception as exc:
        db.session.rollback(); _finish(run,source,read,written,exc); raise


# Preserve the original OpenWeather adapter and select a provider at runtime.
sync_weather_openweather = sync_weather

def sync_weather(season:int, week:int|None=None):
    provider=os.getenv("WEATHER_PROVIDER","nws").strip().lower()
    if provider == "nws": return sync_weather_nws(season,week)
    if provider == "openweather": return sync_weather_openweather(season,week)
    raise ValueError("WEATHER_PROVIDER must be 'nws' or 'openweather'")


def sync_live_games(season:int, week:int|None=None):
    """Refresh game status and scores from SportsDataIO.

    Endpoint templates are configurable because feed paths can vary by product
    entitlement. Default path follows the standard Scores by Season shape.
    """
    source=_source("sportsdataio","SportsDataIO NFL feeds",os.getenv("SPORTSDATAIO_BASE_URL","https://api.sportsdata.io/v3/nfl"),2)
    run=_run("sportsdataio:live_games",{"season":season,"week":week}); read=written=0
    try:
        rows=_sportsdata("SPORTSDATAIO_SCORES_PATH","scores/json/Scores/{season}",season)
        teams=_team_map()
        for row in rows:
            row_week=_int(row.get("Week"))
            if week is not None and row_week != week: continue
            read+=1
            home=teams.get(_team(row.get("HomeTeam"))); away=teams.get(_team(row.get("AwayTeam")))
            if not home or not away: continue
            ext=str(row.get("GlobalGameID") or row.get("GameKey") or row.get("ScoreID") or "")
            game=db.session.scalar(db.select(Game).where(Game.external_id==ext)) if ext else None
            if not game:
                game=db.session.scalar(db.select(Game).where(Game.season==season,Game.week==row_week,Game.home_team_id==home.id,Game.away_team_id==away.id))
            if not game:
                game=Game(external_id=ext or f"sportsdataio-{season}-{row_week}-{away.abbreviation}-{home.abbreviation}",season=season,season_type=str(row.get("SeasonType") or "REG")[:8],week=row_week or 0,home_team_id=home.id,away_team_id=away.id)
                db.session.add(game)
            game.home_score=_int(row.get("HomeScore")); game.away_score=_int(row.get("AwayScore")); game.state=row.get("Status")
            game.status_detail=row.get("QuarterDescription") or row.get("Status"); game.completed=str(row.get("Status") or "").lower() in {"final","f","completed"}
            if row.get("DateTime"):
                try: game.kickoff_at=datetime.fromisoformat(str(row["DateTime"]).replace("Z","+00:00"))
                except ValueError: pass
            game.venue=row.get("StadiumDetails",{}).get("Name") if isinstance(row.get("StadiumDetails"),dict) else game.venue
            capture_raw(source,"live_game",ext or game.external_id,row,season=season,week=row_week); written+=1
        db.session.commit(); _finish(run,source,read,written); return {"dataset":"live_games","provider":"sportsdataio","read":read,"written":written}
    except Exception as exc:
        db.session.rollback(); _finish(run,source,read,written,exc); raise
