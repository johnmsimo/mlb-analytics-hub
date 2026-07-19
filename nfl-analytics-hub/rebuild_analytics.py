import argparse
from app import app
from database import db
from analytics_warehouse import rebuild_analytics

parser = argparse.ArgumentParser(description="Rebuild NFL season aggregate tables")
parser.add_argument("--season", type=int)
args = parser.parse_args()
with app.app_context():
    db.create_all()
    print(rebuild_analytics(args.season))
