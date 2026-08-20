from pathlib import Path

from flask import Flask

from monetization_growth import (
    CONVERSION_LEDGER_STORAGE_KEY,
    MONETIZATION_GROWTH_VERSION,
    ONBOARDING_STORAGE_KEY,
    REFERRAL_STORAGE_KEY,
    build_monetization_status,
    monetization_growth_bp,
)
from product_hub import product_hub_bp


ROOT = Path(__file__).resolve().parents[1]


def make_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(monetization_growth_bp)
    app.register_blueprint(product_hub_bp)
    return app


def test_status_fails_closed_without_customer_identity_or_billing() -> None:
    payload = build_monetization_status({})

    assert payload["version"] == MONETIZATION_GROWTH_VERSION == "5.11"
    assert payload["rolloutState"] == "identity_required"
    assert [plan["key"] for plan in payload["plans"]] == ["free", "premium"]
    assert payload["plans"][0]["availability"] == "available"
    assert payload["plans"][1]["availability"] == "preview"
    assert payload["freeUsage"] == {
        "measurement": "daily_decision_board_view",
        "configuredLimit": None,
        "enforcementMode": "shadow",
        "hardLimitEnabled": False,
        "counterPersistence": "device_private",
        "reason": (
            "A limit can be evaluated in preview, but cannot be a secure "
            "paid boundary without verified customer identity."
        ),
    }
    assert payload["premiumEntitlement"]["state"] == "unavailable"
    assert payload["premiumEntitlement"]["clientStorageCanGrant"] is False
    assert payload["premiumEntitlement"]["anonymousSessionCanGrant"] is False
    assert payload["billing"]["checkoutAvailable"] is False
    assert len(payload["billing"]["blockers"]) == 3
    assert payload["readOnly"] is True
    assert payload["serverMutation"] is False
    assert payload["rawPersonalDataIncluded"] is False
    assert payload["failClosed"] is True


def test_free_limit_is_bounded_configuration_and_never_hard_enforced() -> None:
    configured = build_monetization_status({"FREE_DAILY_BOARD_VIEW_LIMIT": "7"})
    invalid = build_monetization_status({"FREE_DAILY_BOARD_VIEW_LIMIT": "1001"})

    assert configured["freeUsage"]["configuredLimit"] == 7
    assert configured["freeUsage"]["enforcementMode"] == "shadow"
    assert configured["freeUsage"]["hardLimitEnabled"] is False
    assert invalid["freeUsage"]["configuredLimit"] is None


def test_status_and_pricing_routes_are_read_only_and_no_store() -> None:
    client = make_app().test_client()

    status = client.get("/api/monetization/status")
    pricing = client.get("/pricing")
    mutation = client.post("/api/monetization/status", json={"plan": "premium"})

    assert status.status_code == 200
    assert status.headers["Cache-Control"] == "no-store"
    assert status.get_json()["billing"]["checkoutAvailable"] is False
    assert pricing.status_code == 200
    assert pricing.headers["Cache-Control"] == "no-store"
    assert b"Plans \xc2\xb7 MLB Analytics Hub" in pricing.data
    assert mutation.status_code == 405


def test_product_journey_exposes_phase_511_paid_boundary() -> None:
    payload = make_app().test_client().get("/api/product/journey").get_json()
    contract = payload["monetizationGrowth"]

    assert contract["version"] == "5.11"
    assert contract["sourceEndpoint"] == "/api/monetization/status"
    assert contract["surface"] == "/pricing"
    assert contract["rolloutState"] == "identity_required"
    assert contract["freeUsageEnforcementMode"] == "shadow"
    assert contract["premiumEntitlementSource"] == "server_verified_subscription"
    assert contract["clientStorageCanGrantPremium"] is False
    assert contract["anonymousSessionCanGrantPremium"] is False
    assert contract["checkoutAvailable"] is False
    assert contract["onboardingStorageKey"] == ONBOARDING_STORAGE_KEY
    assert contract["referralStorageKey"] == REFERRAL_STORAGE_KEY
    assert contract["conversionLedgerStorageKey"] == CONVERSION_LEDGER_STORAGE_KEY
    assert contract["serverAnalyticsCollection"] is False
    assert contract["rawPersonalDataIncluded"] is False
    assert contract["serverMutation"] is False
    assert contract["failClosed"] is True


def test_phase_511_surfaces_have_mobile_and_privacy_contracts() -> None:
    pricing = (ROOT / "pricing.html").read_text(encoding="utf-8")
    workspace = (ROOT / "product_hub.html").read_text(encoding="utf-8")
    script = (ROOT / "static" / "monetization-growth.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "monetization-growth.css").read_text(encoding="utf-8")
    wsgi = (ROOT / "wsgi.py").read_text(encoding="utf-8")

    assert 'name="viewport"' in pricing
    assert 'data-growth-surface="pricing"' in pricing
    assert 'id="growthRolloutState"' in pricing
    assert 'id="growthOnboarding"' in workspace
    assert workspace.count('data-onboarding-step="') == 4
    assert 'id="growthPremiumInterest"' in workspace
    assert "^[A-Za-z0-9_-]{3,32}$" in script
    assert "EVENT_ALLOWLIST" in script
    assert "fetch(STATUS_URL" in script
    assert "@media(max-width:700px)" in css
    assert "min-height:44px" in css
    assert wsgi.count("app_module.app.register_blueprint(monetization_growth_bp)") == 1
