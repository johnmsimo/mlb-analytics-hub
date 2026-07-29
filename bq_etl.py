"""BigQuery ETL for MLB Analytics Hub.

Phase 0 of the Vertex AI + BigQuery integration. Standalone script that
designs and populates the `mlb` dataset consumed by the Vertex-backed
endpoints in app.py (`/api/matchup`, `/api/cheatsheet`, `/api/insights`,
`/api/projected-boxscore`).

Tables:
  - mlb.batters             — season hitting stats keyed by player_id
  - mlb.pitchers            — season pitching stats keyed by player_id
  - mlb.bvp_situational     — historical batter-vs-pitcher aggregates
                              (partitioned by DATE(updated_at))

View:
  - mlb.daily_slate_view    — joins today's schedule with batters/pitchers

Run as one-off:
    python bq_etl.py --refresh all
    python bq_etl.py --refresh batters
    python bq_etl.py --refresh pitchers
    python bq_etl.py --refresh bvp
    python bq_etl.py --refresh view   # re-apply view DDL only

Auth: ADC via GOOGLE_APPLICATION_CREDENTIALS. Requires GOOGLE_CLOUD_PROJECT.

This script does NOT import app.py — importing app.py would trigger Flask
boot + 150 MB cache preload, which is wrong for a batch job. It fetches the
same upstream sources (MLB Stats API bulk endpoint, local Savant CSVs) and
relies on the BvP CSV files (`data/mlb_matchups_*.csv`) that app.py already
maintains on disk.
"""
from __future__ import annotations

import argparse
import csv as csvmod
import glob as _glob
import logging
import os
import sys
import time
from datetime import datetime, timezone

import pandas as pd

from clients.mlb_client import mlb_client
from config import settings

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
log = logging.getLogger("bq_etl")

DATA_DIR = settings.data_dir
DATASET = settings.bq_dataset


# ── Schemas ───────────────────────────────────────────────────────────────────
def _bq():
    from google.cloud import bigquery
    return bigquery


def batters_schema():
    bq = _bq()
    return [
        bq.SchemaField("player_id",     "INT64",   mode="REQUIRED"),
        bq.SchemaField("name",          "STRING"),
        bq.SchemaField("team",          "STRING"),
        bq.SchemaField("bats",          "STRING"),
        bq.SchemaField("pa",            "INT64"),
        bq.SchemaField("ab",            "INT64"),
        bq.SchemaField("h",             "INT64"),
        bq.SchemaField("hr",            "INT64"),
        bq.SchemaField("bb",            "INT64"),
        bq.SchemaField("k",             "INT64"),
        bq.SchemaField("avg",           "FLOAT64"),
        bq.SchemaField("obp",           "FLOAT64"),
        bq.SchemaField("slg",           "FLOAT64"),
        bq.SchemaField("woba",          "FLOAT64"),
        bq.SchemaField("xwoba",         "FLOAT64"),
        bq.SchemaField("iso",           "FLOAT64"),
        bq.SchemaField("k_pct",         "FLOAT64"),
        bq.SchemaField("bb_pct",        "FLOAT64"),
        bq.SchemaField("barrel_pct",    "FLOAT64"),
        bq.SchemaField("hard_hit_pct",  "FLOAT64"),
        bq.SchemaField("avg_ev",        "FLOAT64"),
        bq.SchemaField("updated_at",    "TIMESTAMP", mode="REQUIRED"),
    ]


def pitchers_schema():
    bq = _bq()
    return [
        bq.SchemaField("player_id",  "INT64",   mode="REQUIRED"),
        bq.SchemaField("name",       "STRING"),
        bq.SchemaField("team",       "STRING"),
        bq.SchemaField("throws",     "STRING"),
        bq.SchemaField("ip",         "FLOAT64"),
        bq.SchemaField("era",        "FLOAT64"),
        bq.SchemaField("fip",        "FLOAT64"),
        bq.SchemaField("xera",       "FLOAT64"),
        bq.SchemaField("whip",       "FLOAT64"),
        bq.SchemaField("k9",         "FLOAT64"),
        bq.SchemaField("bb9",        "FLOAT64"),
        bq.SchemaField("hr9",        "FLOAT64"),
        bq.SchemaField("gb_pct",     "FLOAT64"),
        bq.SchemaField("csw_pct",    "FLOAT64"),
        bq.SchemaField("avg_velo",   "FLOAT64"),
        bq.SchemaField("pitch_mix",  "RECORD", mode="REPEATED", fields=[
            bq.SchemaField("pitch",        "STRING"),
            bq.SchemaField("usage_pct",    "FLOAT64"),
            bq.SchemaField("run_value",    "FLOAT64"),
        ]),
        bq.SchemaField("updated_at", "TIMESTAMP", mode="REQUIRED"),
    ]


def bvp_schema():
    bq = _bq()
    return [
        bq.SchemaField("batter_id",      "INT64",   mode="REQUIRED"),
        bq.SchemaField("pitcher_id",     "INT64",   mode="REQUIRED"),
        bq.SchemaField("pa",             "INT64"),
        bq.SchemaField("ab",             "INT64"),
        bq.SchemaField("h",              "INT64"),
        bq.SchemaField("hr",             "INT64"),
        bq.SchemaField("bb",             "INT64"),
        bq.SchemaField("k",              "INT64"),
        bq.SchemaField("avg",            "FLOAT64"),
        bq.SchemaField("slg",            "FLOAT64"),
        bq.SchemaField("woba",           "FLOAT64"),
        bq.SchemaField("xwoba",          "FLOAT64"),
        bq.SchemaField("last_faced",     "DATE"),
        bq.SchemaField("platoon_split",  "STRING"),
        bq.SchemaField("updated_at",     "TIMESTAMP", mode="REQUIRED"),
    ]


SLATE_VIEW_SQL_TMPL = """\
CREATE OR REPLACE VIEW `{project}.{dataset}.daily_slate_view` AS
SELECT
  CURRENT_DATE('America/New_York') AS slate_date,
  b.player_id   AS batter_id,
  b.name        AS batter_name,
  b.team        AS batter_team,
  b.bats        AS bats,
  b.avg         AS batter_avg,
  b.xwoba       AS batter_xwoba,
  b.hr          AS batter_hr,
  p.player_id   AS pitcher_id,
  p.name        AS pitcher_name,
  p.team        AS pitcher_team,
  p.throws      AS throws,
  p.era         AS pitcher_era,
  p.k9          AS pitcher_k9,
  p.xera        AS pitcher_xera
FROM `{project}.{dataset}.batters`   AS b
CROSS JOIN `{project}.{dataset}.pitchers` AS p
WHERE b.team != p.team
LIMIT 100000
"""


def _fetch_stats_bulk(group, season=None):
    season = season or datetime.now().year
    params = {
        "stats": "season",
        "group": group,
        "season": season,
        "sportIds": 1,
        "playerPool": "all",
        "limit": 2000,
    }
    log.info("[fetch] MLB stats group=%s season=%s", group, season)
    body = mlb_client.stats(timeout=settings.mlb_bulk_http_timeout, **params)
    splits = []
    for s in body.get("stats") or []:
        splits.extend(s.get("splits") or [])
    log.info("[fetch] %s splits=%d", group, len(splits))
    return splits


def _safe_f(v, default=None):
    try:
        if v in (None, "", "-.--", ".---"):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _safe_i(v, default=None):
    try:
        if v in (None, "", "-.--"):
            return default
        return int(float(v))
    except (TypeError, ValueError):
        return default


def build_batters_df():
    splits = _fetch_stats_bulk("hitting")
    now = datetime.now(timezone.utc)
    rows = []
    for sp in splits:
        player = sp.get("player") or {}
        team   = sp.get("team") or {}
        stat   = sp.get("stat") or {}
        pid    = player.get("id")
        if not pid:
            continue
        pa = _safe_i(stat.get("plateAppearances"), 0)
        ab = _safe_i(stat.get("atBats"),           0)
        h  = _safe_i(stat.get("hits"),             0)
        hr = _safe_i(stat.get("homeRuns"),         0)
        bb = _safe_i(stat.get("baseOnBalls"),      0)
        k  = _safe_i(stat.get("strikeOuts"),       0)
        rows.append({
            "player_id": pid,
            "name": player.get("fullName"),
            "team": team.get("abbreviation") or team.get("name"),
            "bats": (player.get("batSide") or {}).get("code"),
            "pa": pa, "ab": ab, "h": h, "hr": hr, "bb": bb, "k": k,
            "avg": _safe_f(stat.get("avg")),
            "obp": _safe_f(stat.get("obp")),
            "slg": _safe_f(stat.get("slg")),
            "woba": None,
            "xwoba": None,
            "iso": (lambda s, a: (s - a) if (s is not None and a is not None) else None)(
                _safe_f(stat.get("slg")), _safe_f(stat.get("avg"))
            ),
            "k_pct": (k / pa * 100.0) if pa else None,
            "bb_pct": (bb / pa * 100.0) if pa else None,
            "barrel_pct": None,
            "hard_hit_pct": None,
            "avg_ev": None,
            "updated_at": now,
        })
    df = pd.DataFrame(rows)
    log.info("[build] batters rows=%d", len(df))
    return df


def build_pitchers_df():
    splits = _fetch_stats_bulk("pitching")
    now = datetime.now(timezone.utc)
    rows = []
    for sp in splits:
        player = sp.get("player") or {}
        team   = sp.get("team") or {}
        stat   = sp.get("stat") or {}
        pid    = player.get("id")
        if not pid:
            continue
        ip = _safe_f(stat.get("inningsPitched"), 0.0)
        k  = _safe_i(stat.get("strikeOuts"),     0)
        bb = _safe_i(stat.get("baseOnBalls"),    0)
        hr = _safe_i(stat.get("homeRuns"),       0)
        rows.append({
            "player_id": pid,
            "name": player.get("fullName"),
            "team": team.get("abbreviation") or team.get("name"),
            "throws": (player.get("pitchHand") or {}).get("code"),
            "ip": ip,
            "era": _safe_f(stat.get("era")),
            "fip": None,
            "xera": None,
            "whip": _safe_f(stat.get("whip")),
            "k9": (9.0 * k / ip) if ip else None,
            "bb9": (9.0 * bb / ip) if ip else None,
            "hr9": (9.0 * hr / ip) if ip else None,
            "gb_pct": _safe_f(stat.get("groundOutsToAirouts")),
            "csw_pct": None,
            "avg_velo": None,
            "pitch_mix": [],
            "updated_at": now,
        })
    df = pd.DataFrame(rows)
    log.info("[build] pitchers rows=%d", len(df))
    return df


def build_bvp_df():
    paths = sorted(_glob.glob(os.path.join(DATA_DIR, "mlb_matchups_*.csv")))
    if not paths:
        log.warning("[bvp] no mlb_matchups_*.csv files in %s", DATA_DIR)
        return pd.DataFrame()
    now = datetime.now(timezone.utc)
    rows = []
    seen = set()
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                reader = csvmod.DictReader(f)
                for r in reader:
                    bid = _safe_i(r.get("batter_id") or r.get("batterId"))
                    pid = _safe_i(r.get("pitcher_id") or r.get("pitcherId"))
                    if not bid or not pid:
                        continue
                    key = (bid, pid)
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append({
                        "batter_id": bid,
                        "pitcher_id": pid,
                        "pa": _safe_i(r.get("pa"),  0),
                        "ab": _safe_i(r.get("ab"),  0),
                        "h": _safe_i(r.get("h"),   0),
                        "hr": _safe_i(r.get("hr"),  0),
                        "bb": _safe_i(r.get("bb"),  0),
                        "k": _safe_i(r.get("k") or r.get("so"), 0),
                        "avg": _safe_f(r.get("avg")),
                        "slg": _safe_f(r.get("slg")),
                        "woba": _safe_f(r.get("woba")),
                        "xwoba": _safe_f(r.get("xwoba")),
                        "last_faced": None,
                        "platoon_split": r.get("platoon") or r.get("split"),
                        "updated_at": now,
                    })
        except Exception as ex:
            log.warning("[bvp] read failed %s: %s", path, ex)
    df = pd.DataFrame(rows)
    log.info("[build] bvp rows=%d", len(df))
    return df


def load_df(client, df, table_name, schema, write_disposition, partition_field=None):
    if df is None or df.empty:
        log.warning("[load] %s skipped (empty df)", table_name)
        return
    bq = _bq()
    job_config = bq.LoadJobConfig(
        schema=schema,
        write_disposition=write_disposition,
        source_format=bq.SourceFormat.PARQUET,
    )
    if partition_field:
        job_config.time_partitioning = bq.TimePartitioning(
            type_=bq.TimePartitioningType.DAY, field=partition_field
        )
    table_id = f"{client.project}.{table_name}"
    log.info("[load] %s rows=%d disp=%s", table_id, len(df), write_disposition)
    job = client.load_table_from_dataframe(df, table_id, job_config=job_config)
    job.result()
    log.info("[load] %s OK", table_id)


def ensure_dataset(client):
    bq = _bq()
    ds_id = f"{client.project}.{DATASET}"
    try:
        client.get_dataset(ds_id)
    except Exception:
        log.info("[ensure] creating dataset %s", ds_id)
        ds = bq.Dataset(ds_id)
        ds.location = settings.bq_location
        client.create_dataset(ds, exists_ok=True)


def apply_slate_view(client):
    sql = SLATE_VIEW_SQL_TMPL.format(project=client.project, dataset=DATASET)
    log.info("[view] applying daily_slate_view")
    client.query(sql).result()
    log.info("[view] OK")


def run_all(refresh="all"):
    project = settings.google_cloud_project
    if not project:
        return {"ok": False, "reason": "GOOGLE_CLOUD_PROJECT_unset"}

    bq = _bq()
    client = bq.Client(project=project)
    ensure_dataset(client)

    started = time.time()
    summary = {"ok": True, "project": project, "refresh": refresh, "tables": {}}
    try:
        if refresh in ("batters", "all"):
            df = build_batters_df()
            load_df(client, df, f"{DATASET}.batters",
                    batters_schema(), bq.WriteDisposition.WRITE_TRUNCATE)
            summary["tables"]["batters"] = len(df) if df is not None else 0
        if refresh in ("pitchers", "all"):
            df = build_pitchers_df()
            load_df(client, df, f"{DATASET}.pitchers",
                    pitchers_schema(), bq.WriteDisposition.WRITE_TRUNCATE)
            summary["tables"]["pitchers"] = len(df) if df is not None else 0
        if refresh in ("bvp", "all"):
            df = build_bvp_df()
            load_df(client, df, f"{DATASET}.bvp_situational",
                    bvp_schema(), bq.WriteDisposition.WRITE_APPEND,
                    partition_field="updated_at")
            summary["tables"]["bvp_situational"] = len(df) if df is not None else 0
        if refresh in ("view", "all"):
            apply_slate_view(client)
            summary["tables"]["daily_slate_view"] = "applied"
    except Exception as ex:
        summary["ok"] = False
        summary["error"] = str(ex)
        log.error("[run_all] failed: %s", ex)
    summary["elapsed_sec"] = round(time.time() - started, 2)
    return summary


import threading as _threading

_bq_scheduler_start_lock = _threading.Lock()
_bq_scheduler_started = False

_SCHED_RUN_HOUR_ET   = settings.bq_etl_hour_et
_SCHED_RUN_MINUTE_ET = settings.bq_etl_minute_et


def start_bq_scheduler():
    """Start the BigQuery ETL scheduler once per process.

    Runs one boot refresh and then refreshes daily at the configured ET time.
    Repeated calls are safe and return without creating duplicate threads.
    """
    global _bq_scheduler_started

    if not settings.google_cloud_project:
        log.info("[bq_etl] GOOGLE_CLOUD_PROJECT unset — scheduler not armed")
        return False

    with _bq_scheduler_start_lock:
        if _bq_scheduler_started:
            log.info("[bq_etl] scheduler already armed; skipping duplicate start")
            return False
        _bq_scheduler_started = True

    def _runner():
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZI
        ET_TZ = _ZI("America/New_York")

        try:
            log.info("[bq_etl] boot refresh starting")
            summary = run_all("all")
            log.info("[bq_etl] boot refresh complete: %s", summary)
        except Exception as ex:
            log.warning("[bq_etl] boot refresh failed: %s", ex)

        last_run_date = None
        while True:
            try:
                now = _dt.now(ET_TZ)
                if (now.hour == _SCHED_RUN_HOUR_ET
                        and now.minute == _SCHED_RUN_MINUTE_ET
                        and now.date() != last_run_date):
                    last_run_date = now.date()
                    log.info("[bq_etl] daily refresh starting")
                    summary = run_all("all")
                    log.info("[bq_etl] daily refresh complete: %s", summary)
            except Exception as ex:
                log.warning("[bq_etl] scheduler tick failed: %s", ex)
            time.sleep(30)

    t = _threading.Thread(target=_runner, daemon=True, name="bq_etl_scheduler")
    t.start()
    log.info("[bq_etl] scheduler armed — daily refresh at %02d:%02d ET",
             _SCHED_RUN_HOUR_ET, _SCHED_RUN_MINUTE_ET)
    return True


def main():
    ap = argparse.ArgumentParser(description="MLB Analytics Hub — BigQuery ETL")
    ap.add_argument("--refresh", default="all",
                    choices=["batters", "pitchers", "bvp", "view", "all"],
                    help="which slice to refresh")
    args = ap.parse_args()
    summary = run_all(args.refresh)
    if not summary.get("ok"):
        log.error("[main] failed: %s", summary)
        sys.exit(1)
    log.info("[done] %s", summary)


if __name__ == "__main__":
    main()
