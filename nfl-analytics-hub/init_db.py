"""Initialize and optionally seed the NFL database.

Usage:
  python init_db.py
  python init_db.py --sync
"""
from __future__ import annotations

import argparse

from app import app
from data_ingestion import sync_cached_data
from database import init_database


parser = argparse.ArgumentParser()
parser.add_argument("--sync", action="store_true", help="Import cached schedules and player-week stats")
args = parser.parse_args()
init_database(app)
if args.sync:
    with app.app_context():
        print(sync_cached_data(app.config.get("NFL_DATA_DIR", "data")))
else:
    print("Database initialized.")
