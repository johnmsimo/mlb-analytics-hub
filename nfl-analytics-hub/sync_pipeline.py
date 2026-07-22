#!/usr/bin/env python3
"""Run the complete idempotent warehouse pipeline.

Usage:
    python sync_pipeline.py
    python sync_pipeline.py --season 2025 --skip-ingest
"""
from __future__ import annotations

import argparse
import os

from app import app
from analytics_warehouse import rebuild_analytics
from data_ingestion import sync_cached_data
from data_quality import run_quality_checks
from database import db
from db_models import SchemaVersion


SCHEMA_VERSION = "phase-9-data-platform"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", type=int)
    parser.add_argument("--data-dir", default=os.environ.get("NFL_DATA_DIR"))
    parser.add_argument("--skip-ingest", action="store_true")
    args = parser.parse_args()

    with app.app_context():
        db.create_all()
        version = db.session.get(SchemaVersion, SCHEMA_VERSION)
        if not version:
            db.session.add(SchemaVersion(
                version=SCHEMA_VERSION,
                description="Source registry, raw provenance, quality checks, and entity profile APIs.",
            ))
            db.session.commit()

        result = {}
        if not args.skip_ingest:
            data_dir = args.data_dir or os.path.join(app.root_path, "data")
            result["ingestion"] = sync_cached_data(data_dir)
        result["analytics"] = rebuild_analytics(args.season)
        result["quality"] = run_quality_checks()
        print(result)


if __name__ == "__main__":
    main()
