"""Pitcher season-stat helpers extracted from the monolithic Flask app."""

from datetime import datetime

import requests


def configure_pitcher_stats_context(namespace):
    globals().update(namespace)


def pitcher_stats_mlb(player_id):
    try:
        r = requests.get(f"{MLB_API}/people/{player_id}/stats?stats=season&group=pitching&season={datetime.now().year}", timeout=8)
        r.raise_for_status()
        splits = r.json().get("stats", [{}])[0].get("splits", [])
        prof = player_profile(player_id)
        if not splits:
            return {'pitchHand': prof.get('throws', 'R')}
        s = splits[0].get("stat", {})
        return {
            "era": s.get("era", "N/A"), "whip": s.get("whip", "N/A"),
            "ip": s.get("inningsPitched", "N/A"),
            "wins": s.get("wins", 0), "losses": s.get("losses", 0),
            "g": s.get("gamesPlayed", 0), "gs": s.get("gamesStarted", 0),
            "k9": round(float(s.get("strikeoutsPer9Inn", 0) or 0), 2),
            "bb9": round(float(s.get("walksPer9Inn", 0) or 0), 2),
            "hr9": round(float(s.get("homeRunsPer9", 0) or 0), 2),
            "pitchHand": prof.get('throws', 'R'),
        }
    except Exception:
        prof = player_profile(player_id)
        return {'pitchHand': prof.get('throws', 'R')}

pitcherstatsmlb = pitcher_stats_mlb


__all__ = ["configure_pitcher_stats_context", "pitcher_stats_mlb", "pitcherstatsmlb"]
