"""Phase 5.11 monetization and growth readiness contracts.

This module intentionally exposes no checkout or entitlement mutation. Customer
accounts were deferred, so Premium access must remain server-owned and fail
closed until a verified customer identity and billing adapter are implemented.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, make_response


MONETIZATION_GROWTH_VERSION = "5.11"
MONETIZATION_STATUS_ENDPOINT = "/api/monetization/status"
ONBOARDING_STORAGE_KEY = "mlb_growth_onboarding_v511"
REFERRAL_STORAGE_KEY = "mlb_growth_referral_v511"
CONVERSION_LEDGER_STORAGE_KEY = "mlb_growth_events_v511"

_ROOT = Path(__file__).resolve().parent
_PRICING_PATH = _ROOT / "pricing.html"

monetization_growth_bp = Blueprint("monetization_growth", __name__)


def _configured_free_limit(environment: Mapping[str, str]) -> int | None:
    raw = str(environment.get("FREE_DAILY_BOARD_VIEW_LIMIT", "")).strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if 1 <= value <= 1000 else None


def build_monetization_status(
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return the public, secret-free Phase 5.11 rollout receipt."""

    source = os.environ if environment is None else environment
    free_limit = _configured_free_limit(source)

    # A browser cookie or localStorage flag is not customer identity. Keep the
    # paid boundary closed until the deferred accounts phase provides a verified
    # server-side principal and the billing adapter binds subscriptions to it.
    blockers = [
        "Verified customer identity is not implemented.",
        "A server-side subscription billing adapter is not implemented.",
        "Webhook-backed entitlement reconciliation is not implemented.",
    ]

    return {
        "success": True,
        "version": MONETIZATION_GROWTH_VERSION,
        "rolloutState": "identity_required",
        "plans": [
            {
                "key": "free",
                "label": "Free",
                "availability": "available",
                "price": None,
                "features": [
                    "Daily Decision Board",
                    "Public Verification Ledger",
                    "Production multi-book shopping",
                    "Guided parlay risk explanations",
                ],
            },
            {
                "key": "premium",
                "label": "Premium",
                "availability": "preview",
                "price": None,
                "features": [
                    "Higher daily decision-board allowance",
                    "Cloud preferences and tracked decisions",
                    "Delivered alert channels",
                    "Server-verified subscription entitlement",
                ],
            },
        ],
        "freeUsage": {
            "measurement": "daily_decision_board_view",
            "configuredLimit": free_limit,
            "enforcementMode": "shadow",
            "hardLimitEnabled": False,
            "counterPersistence": "device_private",
            "reason": (
                "A limit can be evaluated in preview, but cannot be a secure "
                "paid boundary without verified customer identity."
            ),
        },
        "premiumEntitlement": {
            "state": "unavailable",
            "source": "server_verified_subscription",
            "clientStorageCanGrant": False,
            "anonymousSessionCanGrant": False,
            "failClosed": True,
        },
        "billing": {
            "state": "identity_required",
            "checkoutAvailable": False,
            "provider": None,
            "priceDecisionRecorded": False,
            "webhookReconciliationRequired": True,
            "blockers": blockers,
        },
        "onboarding": {
            "state": "available",
            "persistence": "device_private",
            "storageKey": ONBOARDING_STORAGE_KEY,
            "steps": [
                "review_daily_board",
                "save_player",
                "inspect_evidence",
                "open_tracker",
            ],
        },
        "referrals": {
            "state": "device_attribution",
            "queryParameter": "ref",
            "acceptedPattern": "^[A-Za-z0-9_-]{3,32}$",
            "persistence": "device_private",
            "storageKey": REFERRAL_STORAGE_KEY,
            "rawPersonalDataIncluded": False,
        },
        "conversionAnalytics": {
            "state": "device_receipts",
            "persistence": "device_private",
            "storageKey": CONVERSION_LEDGER_STORAGE_KEY,
            "maximumReceipts": 100,
            "serverCollection": False,
            "rawPersonalDataIncluded": False,
            "events": [
                "pricing_viewed",
                "premium_interest",
                "onboarding_step_completed",
                "referral_landed",
            ],
        },
        "readOnly": True,
        "serverMutation": False,
        "rawPersonalDataIncluded": False,
        "failClosed": True,
    }


@monetization_growth_bp.get("/pricing")
def pricing_page():
    response = make_response(_PRICING_PATH.read_text(encoding="utf-8"))
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    response.headers["Cache-Control"] = "no-store"
    return response


@monetization_growth_bp.get(MONETIZATION_STATUS_ENDPOINT)
def monetization_status():
    response = jsonify(build_monetization_status())
    response.headers["Cache-Control"] = "no-store"
    return response
