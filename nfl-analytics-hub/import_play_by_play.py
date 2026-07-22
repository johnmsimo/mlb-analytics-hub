#!/usr/bin/env python3
import argparse
from app import app
from play_by_play import import_play_by_play, rebuild_advanced_team_stats

p = argparse.ArgumentParser()
p.add_argument("path")
p.add_argument("--season", type=int)
a = p.parse_args()
with app.app_context():
    print({"ingestion": import_play_by_play(a.path), "analytics": rebuild_advanced_team_stats(a.season)})
