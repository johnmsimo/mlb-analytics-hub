"""Backfill multiple NFL seasons into the Version 2.0 warehouse."""
from __future__ import annotations
import argparse, json
from app import app
from database import db
from external_providers import sync_external
from commercial_integrations import sync_commercial
from analytics_warehouse import rebuild_analytics
from play_by_play import rebuild_advanced_team_stats
from data_quality import run_quality_checks

PUBLIC_DATASETS=["pbp","rosters","injuries","depth_charts","snap_counts"]

def run(start:int,end:int,datasets:list[str],commercial:list[str],continue_on_error:bool=True):
    report={"seasons":{},"errors":[]}
    with app.app_context():
        db.create_all()
        for season in range(start,end+1):
            entry={}
            try:
                if datasets: entry["public"]=sync_external(season,datasets)
                if commercial: entry["commercial"]=sync_commercial(season,commercial)
                entry["aggregates"]=rebuild_analytics(season)
                entry["advanced"]=rebuild_advanced_team_stats(season)
                entry["quality"]=run_quality_checks()
            except Exception as exc:
                db.session.rollback(); report["errors"].append({"season":season,"error":str(exc)})
                if not continue_on_error: raise
            report["seasons"][str(season)]=entry
    return report

if __name__=="__main__":
    p=argparse.ArgumentParser(); p.add_argument("--start",type=int,default=2016); p.add_argument("--end",type=int,default=2025)
    p.add_argument("--datasets",default=",".join(PUBLIC_DATASETS)); p.add_argument("--commercial",default="")
    p.add_argument("--fail-fast",action="store_true"); a=p.parse_args()
    result=run(a.start,a.end,[x for x in a.datasets.split(",") if x],[x for x in a.commercial.split(",") if x],not a.fail_fast)
    print(json.dumps(result,indent=2,default=str))
