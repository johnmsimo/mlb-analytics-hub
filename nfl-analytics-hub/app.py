"""
NFL Analytics Hub — Flask entrypoint.

Deliberately thin: routes live in blueprints (routes/games.py, routes/props.py,
routes/tracker_routes.py); data loading in nfl_data.py; odds in odds_api.py;
model in projections.py; betting math in value_engine.py. Keep it that way.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import threading
import time


def _load_local_env_file() -> None:
    """Load a repo-root .env (KEY=VALUE lines) without overriding real env."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))
    except FileNotFoundError:
        pass


_load_local_env_file()

from flask import Flask, Response, jsonify, request  # noqa: E402

import nfl_data  # noqa: E402

app = Flask(__name__)
app.json.sort_keys = False

_BOOT_TS = time.time()
_HTML_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- page serving

_GZ_CACHE: dict[str, tuple[str, bytes]] = {}   # path -> (etag, gzipped body)


def _page_response(filename: str) -> Response:
    """Serve an HTML page with strong ETag + gzip. Re-read per request so
    frontend edits show up without a restart; unchanged pages 304."""
    path = os.path.join(_HTML_DIR, filename)
    try:
        with open(path, "rb") as f:
            body = f.read()
    except FileNotFoundError:
        return Response(f"<h1>{filename} not found</h1>", 404, mimetype="text/html")
    etag = '"' + hashlib.sha1(body).hexdigest()[:20] + '"'
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers={"ETag": etag,
                                             "Cache-Control": "no-cache"})
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if "gzip" in (request.headers.get("Accept-Encoding") or ""):
        hit = _GZ_CACHE.get(path)
        if not hit or hit[0] != etag:
            hit = (etag, gzip.compress(body, 6))
            _GZ_CACHE[path] = hit
        headers["Content-Encoding"] = "gzip"
        return Response(hit[1], mimetype="text/html", headers=headers)
    return Response(body, mimetype="text/html", headers=headers)


@app.after_request
def _gzip_json(resp: Response):
    if (resp.mimetype == "application/json" and resp.status_code == 200
            and not resp.direct_passthrough
            and len(resp.get_data()) > 4096
            and "gzip" in (request.headers.get("Accept-Encoding") or "")):
        resp.set_data(gzip.compress(resp.get_data(), 6))
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Content-Length"] = str(len(resp.get_data()))
    return resp


# --------------------------------------------------------------------- pages

@app.route("/")
def page_dashboard():
    return _page_response("dashboard.html")


@app.route("/props")
def page_props():
    return _page_response("props.html")


@app.route("/game/<game_id>")
def page_game(game_id):
    return _page_response("game.html")


@app.route("/tracker")
def page_tracker():
    return _page_response("tracker.html")


# ------------------------------------------------------------- health/status

@app.route("/health")
def health():
    """Fly.io readiness probe — must return 200 immediately, even cold."""
    return jsonify({"ok": True, "uptime_sec": round(time.time() - _BOOT_TS, 1)})


@app.route("/api/status")
def api_status():
    import odds_api
    season = nfl_data.default_season()
    stats_season = None
    try:
        stats_season = nfl_data.stats_season(season)
    except Exception:  # noqa: BLE001
        pass
    return jsonify({
        "app": "nfl-analytics-hub",
        "uptime_sec": round(time.time() - _BOOT_TS, 1),
        "season": season,
        "current_week": _safe(nfl_data.current_week),
        "stats_season": stats_season,
        "odds_api_configured": odds_api.is_configured(),
        "data_dir": nfl_data.DATA_DIR,
    })


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


# ---------------------------------------------------------------- blueprints

from routes.games import games_bp          # noqa: E402
from routes.props import props_bp          # noqa: E402
from routes.tracker_routes import tracker_bp  # noqa: E402

app.register_blueprint(games_bp)
app.register_blueprint(props_bp)
app.register_blueprint(tracker_bp)


# ------------------------------------------------------------------- preload

_preload_started = False


def _preload_caches() -> None:
    """Kick background warms. Called from gunicorn post_fork (after the port
    is bound) and from __main__ — never at import time."""
    global _preload_started
    if _preload_started:
        return
    _preload_started = True

    def _warm():
        nfl_data.preload()
        try:
            import tracker
            tracker.start_background_workers()
        except Exception as e:  # noqa: BLE001
            print(f"[preload] tracker workers: {e}")

    threading.Thread(target=_warm, daemon=True, name="nfl-preload").start()


if __name__ == "__main__":
    _preload_caches()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
