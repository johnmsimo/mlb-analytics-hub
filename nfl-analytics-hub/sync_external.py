from __future__ import annotations
import argparse
from app import app
from external_providers import sync_external
from play_by_play import rebuild_advanced_team_stats

p = argparse.ArgumentParser(description="Synchronize external NFL datasets")
p.add_argument("--season", type=int, required=True)
p.add_argument("--datasets", default="pbp,rosters,injuries,depth_charts,snap_counts")
p.add_argument("--skip-analytics", action="store_true")
args = p.parse_args()

with app.app_context():
    datasets = [x.strip() for x in args.datasets.split(",") if x.strip()]
    print(sync_external(args.season, datasets))
    if "pbp" in datasets and not args.skip_analytics:
        print(rebuild_advanced_team_stats(args.season))
