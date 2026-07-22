"""Database configuration and lifecycle helpers.

SQLite is the zero-configuration development default. Set DATABASE_URL to a
PostgreSQL URL in production. SQLAlchemy keeps the domain model portable.
"""
from __future__ import annotations

import os
from pathlib import Path

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from sqlalchemy import event
from sqlalchemy.engine import Engine


db = SQLAlchemy(session_options={"expire_on_commit": False})
migrate = Migrate(compare_type=True)


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record):  # noqa: ARG001
    """Enable referential integrity and better concurrency for SQLite."""
    module = dbapi_connection.__class__.__module__
    if not module.startswith("sqlite3"):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def configure_database(app: Flask) -> None:
    root = Path(app.root_path)
    default_path = root / "data" / "nfl_analytics.db"
    default_path.parent.mkdir(parents=True, exist_ok=True)
    url = os.environ.get("DATABASE_URL", f"sqlite:///{default_path}")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    engine_options = {"pool_pre_ping": True, "pool_recycle": 300}
    if url.startswith("postgresql"):
        engine_options.update({
            "pool_size": int(os.environ.get("DB_POOL_SIZE", "10")),
            "max_overflow": int(os.environ.get("DB_MAX_OVERFLOW", "20")),
            "pool_timeout": int(os.environ.get("DB_POOL_TIMEOUT", "30")),
            "connect_args": {"application_name": "nfl-analytics-hub"},
        })
    app.config.update(
        SQLALCHEMY_DATABASE_URI=url,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS=engine_options,
    )
    db.init_app(app)
    migrate.init_app(app, db, directory=str(root / "migrations"))


def init_database(app: Flask) -> None:
    with app.app_context():
        # Import registers model metadata before create_all.
        import db_models  # noqa: F401, PLC0415
        db.create_all()
