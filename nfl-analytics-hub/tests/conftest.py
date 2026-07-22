import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
import pytest
from app import app
from database import db
from data_ingestion import sync_cached_data

@pytest.fixture(scope="session", autouse=True)
def warehouse_seed():
    app.config.update(TESTING=True)
    with app.app_context():
        db.create_all()
        sync_cached_data(str(Path(app.root_path) / "data"))
    yield

@pytest.fixture()
def client():
    with app.test_client() as c:
        yield c
