import os
import requests
from flask import Flask, render_template, jsonify
from datetime import datetime, timezone
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

MLB_API = "https://statsapi.mlb.com/api/v1"

TEAM_LOGOS = {
    "ARI": "https://www.mlbstatic.com/team-logos/109.svg",
    "ATL": "https://www.mlbstatic.com/team-logos/144.svg",
    "BAL": "https://www.mlbstatic.com/team-logos/110.svg",
    "BOS": "https://www.mlbstatic.com/team-logos/111.svg",
    "CHC": "https://www.mlbstatic.com/team-logos/112.svg",
    "CWS": "https://www.mlbstatic.com/team-logos/145.svg",
    "CIN": "https://www.mlbstatic.com/team-logos/113.svg",
    "CLE": "https://www.mlbstatic.com/team-logos/114.svg",
    "COL": "https://www.mlbstatic.com/team-logos/115.svg",
    "DET": "https://www.mlbstatic.com/team-logos/116.svg",
    "HOU": "https://www.mlbstatic.com/team-logos/117.svg",
    "KC":  "https://www.mlbstatic.com/team-logos/118.svg",
    "LAA": "https://www.mlbstatic.com/team-logos/108.svg",
    "LAD": "https://www.mlbstatic.com/team-logos/119.svg",
    "MIA": "https://www.mlbstatic.com/team-logos/146.svg",
    "MIL": "https://www.mlbstatic.com/team-logos/158.svg",
    "MIN": "https://www.mlbstatic.com/team-logos/142.svg",
    "NYM": "https://www.mlbstatic.com/team-logos/121.svg",
    "NYY": "https://www.mlbstatic.com/team-logos/147.svg",
    "OAK": "https://www.mlbstatic.com/team-logos/133.svg",
    "PHI": "https://www.mlbstatic.com/team-logos/143.svg",
    "PIT": "https://www.mlbstatic.com/team-logos/134.svg",
    "SD":  "https://www.mlbstatic.com/team-logos/135.svg",
    "SEA": "https://www.mlbstatic.com/team-logos/136.svg",
    "SF":  "https://www.mlbstatic.com/team-logos/137.svg",
    "STL": "https://www.mlbstatic.com/team-logos/138.svg",
    "TB":  "https://www.mlbstatic.com/team-logos/139.svg",
    "TEX": "https://www.mlbstatic.com/team-logos/140.svg",
    "TOR": "https://www.mlbstatic.com/team-logos/141.svg",
    "WSH": "https://www.mlbstatic.com/team-logos/120.svg",
}

PARK_FACTORS = {
    "COL": {"run_factor": 1.18, "hr_factor": 1.22, "name": "Coors Field"},
    "BOS": {"run_factor": 1.08, "hr_factor": 1.09, "name": "Fenway Park"},
    "CIN": {"run_factor": 1.07, "hr_factor": 1.11, "name": "Great American Ball Park"},
    "PHI": {"run_factor": 1.06, "hr_factor": 1.07, "name": "Citizens Bank Park"},
    "TEX": {"run_factor": 1.05, "hr_factor": 1.06, "name": "Globe Life Field"},
    "NYY": {"run_factor": 1.04, "hr_factor": 1.10, "name": "Yankee Stadium"},
    "CHC": {"run_factor": 1.03, "hr_factor": 1.05, "name": "Wrigley Field"},
    "MIL": {"run_factor": 1.02, "hr_factor": 1.04, "name": "American Family Field"},
    "LAD": {"run_factor": 0.97, "hr_factor": 0.96, "name": "Dodger Stadium"},
    "OAK": {"run_factor": 0.96, "hr_factor": 0.93, "name": "Oakland Coliseum"},
    "SF":  {"run_factor": 0.94, "hr_factor": 0.89, "name": "Oracle Park"},
    "SD":  {"run_factor": 0.93, "hr_factor": 0.91, "name": "Petco Park"},
    "MIA": {"run_factor": 0.92, "hr_factor": 0.88, "name": "loanDepot park"},
}

def get_todays_date():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def fetch_mlb_schedule(date_str):
    try:
        url = f"{MLB_API}/schedule"
        params = {
            "sportId": 1,
            "date": date_str,
            "hydrate": "team,probablePitcher,linescore,weather,venue"
        }
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        games = []
        for date_entry in data.get("dates", []):
            for g in date_entry.get("games", []):
                games.append(g)
        return games
    except Exception as e:
        return []

def parse_game(g):
    game_pk   = g.get("gamePk", "")
    status    = g.get("status", {}).get("abstractGameState", "Preview")
    game_time = g.get("gameDate", "")

    away = g.get("teams", {}).get("away", {}).get("team", {})
    home = g.get("teams", {}).get("home", {}).get("team", {})
    away_abbr = away.get("abbreviation", "???")
    home_abbr = home.get("abbreviation", "???")
    away_name = away.get("name", "Away")
    home_name = home.get("name", "Home")

    away_pitcher = g.get("teams", {}).get("away", {}).get("probablePitcher", {})
    home_pitcher = g.get("teams", {}).get("home", {}).get("probablePitcher", {})

    venue = g.get("venue", {}).get("name", "")
    weather = g.get("weather", {})
    temp    = weather.get("temp", "N/A")
    wind    = weather.get("wind", "N/A")
    cond    = weather.get("condition", "N/A")

    park = PARK_FACTORS.get(home_abbr, {"run_factor": 1.0, "hr_factor": 1.0})
    park_factor = park["run_factor"]

    # Simple edge calc based on park + weather
    try:
        temp_val = int(str(temp).replace("°","").strip())
        heat_boost = max(0, (temp_val - 70) * 0.2)
    except:
        heat_boost = 0
    edge = round(min(15, max(1, (park_factor - 1) * 40 + heat_boost + 5)), 1)
    bar_pct = int(min(95, max(10, edge * 6.5)))

    # Format local time
    try:
        dt = datetime.fromisoformat(game_time.replace("Z", "+00:00"))
        local_time = dt.strftime("%-I:%M %p ET")
    except:
        local_time = "TBD"

    return {
        "gamePk":        game_pk,
        "status":        status,
        "gameTime":      local_time,
        "awayAbbr":      away_abbr,
        "homeAbbr":      home_abbr,
        "awayName":      away_name,
        "homeName":      home_name,
        "awayLogo":      TEAM_LOGOS.get(away_abbr, ""),
        "homeLogo":      TEAM_LOGOS.get(home_abbr, ""),
        "awayPitcher":   away_pitcher.get("fullName", "TBD"),
        "homePitcher":   home_pitcher.get("fullName", "TBD"),
        "venue":         venue,
        "temp":          temp,
        "wind":          wind,
        "condition":     cond,
        "parkFactor":    park_factor,
        "edge":          edge,
        "barPct":        bar_pct,
    }

def fetch_pitcher_stats(pitcher_id):
    try:
        url = f"{MLB_API}/people/{pitcher_id}/stats"
        params = {"stats": "season", "group": "pitching", "season": datetime.now().year}
        r = requests.get(url, params=params, timeout=8)
        r.raise_for_status()
        splits = r.json().get("stats", [{}])[0].get("splits", [])
        if splits:
            s = splits[0].get("stat", {})
            return {
                "era":   s.get("era", "N/A"),
                "whip":  s.get("whip", "N/A"),
                "k9":    s.get("strikeoutsPer9Inn", "N/A"),
                "ip":    s.get("inningsPitched", "N/A"),
                "wins":  s.get("wins", 0),
                "losses":s.get("losses", 0),
            }
    except:
        pass
    return {"era": "N/A", "whip": "N/A", "k9": "N/A", "ip": "N/A", "wins": 0, "losses": 0}

# ─── Routes ───────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("mlb_analytics_hub.html")

@app.route("/deep-dive/<int:game_pk>")
def deep_dive(game_pk):
    return render_template("deep_dive_analytics.html", game_pk=game_pk)

@app.route("/api/games/today")
def api_games_today():
    date_str = get_todays_date()
    raw      = fetch_mlb_schedule(date_str)
    games    = [parse_game(g) for g in raw]
    return jsonify({"success": True, "date": date_str, "games": games, "count": len(games)})

@app.route("/api/game/<int:game_pk>")
def api_game_detail(game_pk):
    try:
        url = f"{MLB_API}/game/{game_pk}/boxscore"
        r   = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()

        away_team = data.get("teams", {}).get("away", {})
        home_team = data.get("teams", {}).get("home", {})

        def get_batters(team_data):
            batters = []
            for pid, p in team_data.get("players", {}).items():
                pos = p.get("position", {}).get("abbreviation", "")
                if pos not in ["P", ""] and p.get("gameStatus", {}).get("isCurrentBatter") is not None:
                    s = p.get("stats", {}).get("batting", {})
                    batters.append({
                        "name":   p.get("person", {}).get("fullName", ""),
                        "pos":    pos,
                        "avg":    p.get("seasonStats", {}).get("batting", {}).get("avg", ".000"),
                        "ab":     s.get("atBats", 0),
                        "hits":   s.get("hits", 0),
                        "hr":     s.get("homeRuns", 0),
                        "rbi":    s.get("rbi", 0),
                    })
            return batters[:9]

        return jsonify({
            "success":  True,
            "gamePk":   game_pk,
            "awayBatters": get_batters(away_team),
            "homeBatters": get_batters(home_team),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/api/pitchers/<int:game_pk>")
def api_pitchers(game_pk):
    try:
        raw = fetch_mlb_schedule(get_todays_date())
        for g in raw:
            if g.get("gamePk") == game_pk:
                away_p = g.get("teams", {}).get("away", {}).get("probablePitcher", {})
                home_p = g.get("teams", {}).get("home", {}).get("probablePitcher", {})
                return jsonify({
                    "success": True,
                    "awayPitcher": {
                        "id":   away_p.get("id"),
                        "name": away_p.get("fullName", "TBD"),
                        "stats": fetch_pitcher_stats(away_p.get("id")) if away_p.get("id") else {}
                    },
                    "homePitcher": {
                        "id":   home_p.get("id"),
                        "name": home_p.get("fullName", "TBD"),
                        "stats": fetch_pitcher_stats(home_p.get("id")) if home_p.get("id") else {}
                    }
                })
        return jsonify({"success": False, "error": "Game not found"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
