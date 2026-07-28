"""Operational cache status and invalidation endpoints."""
from __future__ import annotations

import hmac
import os

from flask import Blueprint, jsonify, request

from cache_service import cache_status, invalidate_namespace, reset_cache_metrics

cache_ops_bp = Blueprint("cache_ops", __name__, url_prefix="/api/cache")


def _authorized() -> bool:
    expected = os.getenv("CACHE_ADMIN_TOKEN", "")
    supplied = request.headers.get("X-Cache-Admin-Token", "")
    return bool(expected) and hmac.compare_digest(expected, supplied)


@cache_ops_bp.get("/status")
def status():
    """Read-only cache health and metrics snapshot."""
    return jsonify(cache_status())


@cache_ops_bp.post("/invalidate/<namespace>")
def invalidate_cache_namespace(namespace: str):
    """Safely invalidate registered keys in one namespace."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    deleted = invalidate_namespace(namespace)
    return jsonify({"namespace": namespace, "deleted": deleted})


@cache_ops_bp.post("/metrics/reset")
def reset_metrics():
    """Reset process-local cache metrics without deleting cache entries."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    reset_cache_metrics()
    return jsonify({"reset": True})
