from datetime import datetime, timezone
from pathlib import Path

from multi_book_shopping import build_multi_book_shopping
from product_hub import product_hub_bp


ROOT = Path(__file__).resolve().parents[1]


NOW = datetime(2026, 8, 20, 17, 0, tzinfo=timezone.utc)


def _candidate():
    return {
        "canonicalCandidateId": "judge:hits:over:0.5",
        "canonicalMarketKey": "batter_hits",
        "canonicalSide": "over",
        "line": 0.5,
        "canonicalProbability": 0.66,
        "marketGatePromoted": True,
        "marketGateStatus": "promoted",
        "marketSideGateStatus": "promoted",
    }


def _quote(book, over, under, captured="2026-08-20T16:59:00+00:00"):
    return {
        "book": book,
        "source": "the-odds-api",
        "capturedAt": captured,
        "line": 0.5,
        "overPrice": over,
        "underPrice": under,
    }


def _health(state="ready", degraded=0):
    return {
        "provider": "The Odds API",
        "state": state,
        "configured": True,
        "capturedAt": "2026-08-20T16:59:00+00:00",
        "eventCount": 15,
        "fetchedEventCount": 15 - degraded,
        "degradedEventCount": degraded,
    }


def test_ready_receipt_exposes_consensus_and_best_price_without_stake():
    result = build_multi_book_shopping(
        _candidate(),
        [_quote("Book A", -115, -105), _quote("Book B", -105, -115)],
        provider_health=_health(),
        now=NOW,
    )

    assert result["version"] == "5.9"
    assert result["sourceDecisionVersion"] == "5.3.0"
    assert result["state"] == "ready"
    assert result["consensus"]["acceptedBookCount"] == 2
    assert result["priceShopping"]["bestAvailableBook"] == "Book B"
    assert result["priceShopping"]["bestAvailablePrice"] == -105
    assert len(result["priceShopping"]["quotes"]) == 2
    assert "stakePreview" not in result
    assert result["changesRecommendation"] is False


def test_one_fresh_book_is_partial_and_never_fabricates_consensus():
    result = build_multi_book_shopping(
        _candidate(),
        [_quote("Book A", -110, -110)],
        provider_health=_health("partial", degraded=4),
        now=NOW,
    )

    assert result["state"] == "partial"
    assert result["consensus"]["acceptedBookCount"] == 1
    assert result["consensus"]["fairProbability"] is None
    assert result["decision"]["expectedValue"] is None
    assert result["decision"]["qualifiedForReview"] is False
    assert "consensus requires at least 2 fresh books" in result["decision"]["reasons"]


def test_stale_quotes_fail_closed_without_returning_rejected_rows():
    result = build_multi_book_shopping(
        _candidate(),
        [
            _quote("Book A", -110, -110, "2026-08-20T15:00:00+00:00"),
            _quote("Book B", -105, -115, "2026-08-20T15:00:00+00:00"),
        ],
        provider_health=_health("stale", degraded=15),
        now=NOW,
    )

    assert result["state"] == "stale"
    assert result["consensus"]["acceptedBookCount"] == 0
    assert result["consensus"]["rejectedQuoteCount"] == 2
    assert result["priceShopping"]["quotes"] == []
    assert "rejectedQuotes" not in result["consensus"]


def test_missing_provider_is_unavailable_not_a_single_book_recommendation():
    result = build_multi_book_shopping(
        _candidate(), [], provider_health=None, now=NOW
    )

    assert result["state"] == "unavailable"
    assert result["providerHealth"]["configured"] is False
    assert result["decision"]["approved"] is False


def test_product_contract_requires_phase_59_on_every_recommendation_card():
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(product_hub_bp)
    payload = app.test_client().get("/api/product/journey").get_json()
    contract = payload["productionMultiBookShopping"]

    assert contract["version"] == "5.9"
    assert contract["sourceDecisionEngineVersion"] == "5.3.0"
    assert contract["minimumFreshBooks"] == 2
    assert contract["maximumQuoteAgeSeconds"] == 300
    assert contract["visibleOnCards"] == [
        "daily_decision_board",
        "personalized_signal",
        "saved_player_opportunity",
        "eligible_alert",
    ]
    assert contract["rawRejectedQuotesIncluded"] is False
    assert contract["bankrollIncluded"] is False
    assert contract["stakeDollarsIncluded"] is False
    assert contract["serverMutation"] is False
    assert contract["failClosed"] is True


def test_edge_producer_preserves_quotes_and_uses_provider_timestamps():
    source = (ROOT / "app.py").read_text(encoding="utf-8")

    assert "'sportsbookQuotes': msum.get('quotes') or []" in source
    assert "'oddsObservedAt': msum.get('best_over_captured_at')" in source
    assert "'capturedAt': item.get('captured_at')" in source
    assert "build_multi_book_shopping(" in source
    assert "'multiBookShopping': multi_book_shopping" in source
    assert "'oddsProviderHealth': provider_health" in source
    assert "older than five minutes" in source


def test_all_recommendation_card_renderers_include_the_phase_59_panel():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "product-hub.css").read_text(encoding="utf-8")

    assert "var MULTI_BOOK_SHOPPING_VERSION = '5.9'" in source
    assert source.count("multiBookShoppingHtml(row, true)") == 3
    assert source.count("multiBookShoppingHtml(row, false)") == 1
    assert "data-shopping-state" in source
    assert "Accepted sportsbook prices" in source
    assert ".multi-book-shopping" in css
    assert ".saved-opportunity>.multi-book-shopping{grid-column:1/-1" in css
    assert ".alert-card>div:first-child .multi-book-shopping{grid-column:1/3" in css
    assert "@media(max-width:480px)" in css
