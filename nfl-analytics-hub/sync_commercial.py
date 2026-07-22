from __future__ import annotations
import argparse
from app import create_app
from commercial_integrations import sync_commercial

if __name__ == "__main__":
    parser=argparse.ArgumentParser(description="Sync credentialed NFL integrations")
    parser.add_argument("--season",type=int,required=True)
    parser.add_argument("--week",type=int)
    parser.add_argument("--datasets",default="weather,odds,coaches,transactions")
    args=parser.parse_args()
    app=create_app()
    with app.app_context():
        print(sync_commercial(args.season,[x.strip() for x in args.datasets.split(",") if x.strip()],args.week))
