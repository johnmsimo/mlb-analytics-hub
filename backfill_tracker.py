#!/usr/bin/env python3
"""
backfill_tracker.py — Jump-start the tracker feedback loop with historical dates
═══════════════════════════════════════════════════════════════════════════════
Drives the DEPLOYED app's capture + grade endpoints over a date range so the
tracker fills with graded (model_prob → outcome) pairs. Once enough accrue,
stacked_calibrator.train_all_markets() can fit the per-market isotonic
calibrators and eval_models.py / /api/tracker/model-record become meaningful.

Why hit the deployed app instead of importing app.py?
  • The production tracker lives on the Fly.io persistent volume (/app/data),
    which shadows the repo's data/ dir. Local writes never reach production.
  • The deployed app already has the ~150MB FG/Savant caches loaded; re-loading
    them in a one-shot script is slow and memory-heavy.
  • This reuses the exact production capture/grade pipeline (and the auth path
    used by the in-app auto-workers).

⚠ LOOK-AHEAD CAVEAT
  Backfilled picks are projected with the CURRENT season caches, not
  point-in-time stats as of the historical date, so their model probabilities
  carry mild look-ahead bias (they implicitly "know" later-season form). They
  are stamped backfilled=True (via ?backfill=1) so you can isolate or
  down-weight them. Treat backfilled calibration as a warm-start, not ground
  truth — let genuinely live-captured picks dominate over time.

USAGE
  export MLB_ADMIN_TOKEN=...            # your app's ADMIN_TOKEN (if set)
  python backfill_tracker.py --base-url https://mlb-analytics-hub.fly.dev \
                             --start 2026-05-01 [--end 2026-06-18]

  # dry run (list the dates, hit nothing):
  python backfill_tracker.py --start 2026-05-01 --dry-run

EXIT CODES
  0 success   1 usage/arg error   2 one or more dates failed
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

DEFAULT_BASE_URL = os.getenv("MLB_BASE_URL", "https://mlb-analytics-hub.fly.dev")
DEFAULT_START = "2026-05-01"
# US/Eastern is UTC-4 during the MLB season (EDT); good enough for "yesterday".
_ET = timezone(timedelta(hours=-4))


def _et_today() -> date:
    return datetime.now(_ET).date()


def _daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _request(method: str, url: str, token: str | None, timeout: int = 120) -> tuple[int, dict]:
    headers = {"Accept": "application/json"}
    if token:
        headers["X-Admin-Token"] = token
    req = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, {"raw": body[:500]}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"error": body[:500]}
    except urllib.error.URLError as e:
        return 0, {"error": f"network: {e.reason}"}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": f"{type(e).__name__}: {e}"}


def _with_retry(method: str, url: str, token: str | None, *, retries: int = 3,
                timeout: int = 120) -> tuple[int, dict]:
    backoff = 2.0
    last = (0, {"error": "no attempt"})
    for attempt in range(retries):
        status, data = _request(method, url, token, timeout=timeout)
        # Retry only on network errors / 5xx — not on 4xx (auth, no games, etc.)
        if status and status < 500:
            return status, data
        last = (status, data)
        if attempt < retries - 1:
            time.sleep(backoff)
            backoff *= 2
    return last


def backfill_date(base: str, ds: str, token: str | None, *, bg_wait: float,
                  with_odds: bool) -> dict:
    """Capture (backfill mode) then grade a single date. Returns a summary."""
    odds_q = "&odds=1" if with_odds else ""
    cap_url = f"{base}/api/tracker/capture/{ds}?backfill=1{odds_q}"
    status, cap = _with_retry("POST", cap_url, token, timeout=180)

    if status == 404:
        return {"date": ds, "ok": True, "skipped": "no_games", "captured": 0, "graded": 0}
    if status == 401:
        return {"date": ds, "ok": False, "error": "unauthorized (check MLB_ADMIN_TOKEN)"}
    if status != 200 or not cap.get("success"):
        return {"date": ds, "ok": False, "error": cap.get("error") or f"capture HTTP {status}"}

    captured = cap.get("capturedGames", 0)
    total = cap.get("totalGames", 0)
    n_entries = len(cap.get("entries", []))

    # If capture spilled into a background thread, give it time before grading
    # so all games' picks exist on disk first.
    if cap.get("backgroundStarted") or cap.get("timedOut"):
        time.sleep(bg_wait)

    grade_url = f"{base}/api/tracker/grade/{ds}"
    gstatus, gr = _with_retry("POST", grade_url, token, timeout=180)
    if gstatus != 200 or not gr.get("success"):
        return {"date": ds, "ok": False, "captured": captured, "totalGames": total,
                "entries": n_entries, "error": gr.get("error") or f"grade HTTP {gstatus}"}

    entries = gr.get("entries", [])
    graded = sum(1 for e in entries if e.get("grade") in ("win", "loss", "push"))
    wins = sum(1 for e in entries if e.get("grade") == "win")
    losses = sum(1 for e in entries if e.get("grade") == "loss")
    return {
        "date": ds, "ok": True,
        "capturedGames": captured, "totalGames": total,
        "entries": len(entries), "graded": graded,
        "wins": wins, "losses": losses,
    }


def print_model_record(base: str, token: str | None) -> None:
    status, data = _request("GET", f"{base}/api/tracker/model-record", token)
    if status != 200:
        print(f"\n[model-record] unavailable (HTTP {status})")
        return
    record = data.get("record") or data.get("markets") or {}
    print("\n═══ Raw win% per market (30-day window, /api/tracker/model-record) ═══")
    if not record:
        print("  (no graded picks yet)")
        return
    print(f"  {'MARKET':<22} {'W':>4} {'L':>4} {'GRADED':>7} {'HIT%':>7}")
    print("  " + "-" * 48)
    for mk, v in sorted(record.items()):
        if not isinstance(v, dict):
            continue
        w, l = v.get("wins", 0), v.get("losses", 0)
        g = v.get("graded", w + l)
        hr = v.get("hit_rate")
        hr_s = f"{hr:.1%}" if isinstance(hr, (int, float)) else "—"
        print(f"  {mk:<22} {w:>4} {l:>4} {g:>7} {hr_s:>7}")


def parse_args(argv) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--base-url", default=DEFAULT_BASE_URL,
                   help=f"Deployed app base URL (default: {DEFAULT_BASE_URL})")
    p.add_argument("--start", default=DEFAULT_START, help="YYYY-MM-DD (inclusive)")
    p.add_argument("--end", default=None,
                   help="YYYY-MM-DD (inclusive). Default: yesterday ET")
    p.add_argument("--token", default=os.getenv("MLB_ADMIN_TOKEN") or os.getenv("ADMIN_TOKEN"),
                   help="Admin token (or set MLB_ADMIN_TOKEN). Omit if app has no ADMIN_TOKEN.")
    p.add_argument("--sleep", type=float, default=3.0,
                   help="Seconds to pause between dates (be kind to the MLB API)")
    p.add_argument("--bg-wait", type=float, default=40.0,
                   help="Seconds to wait when a capture spills into background")
    p.add_argument("--with-odds", action="store_true",
                   help="Also request odds (off by default; historical odds unavailable)")
    p.add_argument("--dry-run", action="store_true", help="List dates, hit nothing")
    return p.parse_args(argv)


def main(argv) -> int:
    args = parse_args(argv)
    try:
        start = datetime.strptime(args.start, "%Y-%m-%d").date()
        end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else (_et_today() - timedelta(days=1))
    except ValueError as e:
        print(f"bad date: {e}", file=sys.stderr)
        return 1
    if start > end:
        print(f"start {start} is after end {end}", file=sys.stderr)
        return 1

    base = args.base_url.rstrip("/")
    dates = [d.strftime("%Y-%m-%d") for d in _daterange(start, end)]
    print(f"Backfill {base}  {dates[0]} → {dates[-1]}  ({len(dates)} dates)")
    if args.token:
        print("  auth: X-Admin-Token provided")
    else:
        print("  auth: none (assuming app has no ADMIN_TOKEN)")
    print("  ⚠ backfilled picks carry mild look-ahead bias — stamped backfilled=True\n")

    if args.dry_run:
        for ds in dates:
            print(f"  [dry-run] would capture+grade {ds}")
        return 0

    results = []
    failures = 0
    for i, ds in enumerate(dates, 1):
        res = backfill_date(base, ds, args.token, bg_wait=args.bg_wait, with_odds=args.with_odds)
        results.append(res)
        if res.get("ok"):
            if res.get("skipped"):
                print(f"  [{i}/{len(dates)}] {ds}  · skipped ({res['skipped']})")
            else:
                print(f"  [{i}/{len(dates)}] {ds}  · games {res.get('capturedGames')}/{res.get('totalGames')}"
                      f" · entries {res.get('entries')} · graded {res.get('graded')}"
                      f" (W{res.get('wins')} L{res.get('losses')})")
        else:
            failures += 1
            print(f"  [{i}/{len(dates)}] {ds}  · FAILED: {res.get('error')}")
            if "unauthorized" in (res.get("error") or ""):
                print("\nAborting — fix the admin token and re-run.", file=sys.stderr)
                return 2
        if i < len(dates):
            time.sleep(args.sleep)

    tot_graded = sum(r.get("graded", 0) for r in results)
    tot_wins = sum(r.get("wins", 0) for r in results)
    tot_entries = sum(r.get("entries", 0) for r in results)
    print(f"\nDone. dates={len(dates)} failed={failures} "
          f"entries={tot_entries} graded={tot_graded} wins={tot_wins}")

    print_model_record(base, args.token)

    if tot_graded >= 200:
        print("\n✅ ≥200 graded picks — you can now train the calibrators:")
        print("   python stacked_calibrator.py --train-all")
        print("   python eval_models.py --rolling")
    elif tot_graded:
        print(f"\nℹ {tot_graded} graded picks so far (need ~200/market to train "
              "the isotonic calibrators). Keep the live loop running.")

    return 2 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
