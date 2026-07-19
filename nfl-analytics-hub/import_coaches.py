import argparse
from app import app
from database import db
from data_ingestion import import_coaches
from analytics_warehouse import rebuild_coach_seasons

parser = argparse.ArgumentParser(description="Import coach assignments CSV")
parser.add_argument("path", nargs="?", default="data/coaches.csv")
args = parser.parse_args()
with app.app_context():
    db.create_all()
    result = import_coaches(args.path)
    result["coach_seasons"] = rebuild_coach_seasons()
    print(result)
