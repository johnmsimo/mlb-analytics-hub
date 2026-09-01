#!/usr/bin/env python3
"""Non-mutating live production contract gate for the MLB Analytics Hub."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any


PAGE_BUDGET_SECONDS = 2.0
ASSET_BUDGET_SECONDS = 2.0
RECOMMENDATION_EVIDENCE_VERSION = "4.69"
MAX_EVIDENCE_ODDS_AGE_SECONDS = 900
INVALID_BOOKS = {
    "model",
    "n/a",
    "na",
    "none",
    "projection",
    "research",
    "sim",
    "simulation",
    "unknown",
    "unpriced",
}


class ContractError(RuntimeError):
    """Raised when a live production contract fails closed."""


@dataclass(frozen=True)
class PageContract:
    path: str
    marker: str


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    elapsed_seconds: float

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.text())
        except json.JSONDecodeError as exc:
            raise ContractError(f"invalid JSON response: {exc}") from exc


PUBLIC_PAGE_CONTRACTS = (
    PageContract("/", "MLB Analytics Hub"),
    PageContract("/workspace", "My Hub · MLB Analytics Hub"),
    PageContract("/props", "Prop Projections"),
    PageContract("/cheatsheets", "MLB Analytics Hub - Cheatsheets"),
    PageContract("/tracker", "MLB Analytics Hub · Tracker"),
    PageContract("/consistency", "MLB Analytics Hub - Consistency Sheets"),
    PageContract("/edge-lab", "MLB Analytics Hub - Edge Lab"),
    PageContract("/batter-vs-pitcher", "MLB Analytics Hub - Batter vs Pitcher"),
    PageContract("/value-bets", "MLB Analytics Hub - Value Bets"),
    PageContract("/picks", "MLB Picks"),
    PageContract("/nrfi", "MLB Analytics Hub - NRFI"),
    PageContract("/streak", "100% Club"),
    PageContract("/tools", "MLB Analytics Hub - Tools"),
    PageContract("/pitcher-deep-dive", "Pitcher Deep Dive"),
    PageContract("/breakout-detector", "MLB Breakout Detector"),
    PageContract("/hr-analytics", "HR Analytics Hub"),
    PageContract("/deep-dive/1", "Deep Dive"),
    PageContract("/gameside-deepdive/1", "Gameside Prop View"),
    PageContract("/player/1", "Player Profile"),
)

PHASE_56_PAGE_CONTRACTS = (
    PageContract("/verification", "Public Verification Ledger · MLB Analytics Hub"),
)

PHASE_511_PAGE_CONTRACTS = (
    PageContract("/pricing", "Plans · MLB Analytics Hub"),
)

ADMIN_READ_PATHS = (
    "/settings",
    "/api/app-settings",
    "/api/admin/settings",
    "/api/brain-data/list",
    "/api/cache/status",
    "/api/memory/status",
    "/api/model-actual/daily-summary/stored",
    "/api/tracker/settings",
)


class _StaticAssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.paths: set[str] = set()

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag not in {"link", "script", "img", "source"}:
            return
        for name, value in attrs:
            if name not in {"href", "src"} or not value:
                continue
            parsed = urllib.parse.urlsplit(value)
            if parsed.path.startswith("/static/"):
                suffix = f"?{parsed.query}" if parsed.query else ""
                self.paths.add(parsed.path + suffix)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def fetch_url(base_url: str, path: str, timeout: float) -> HttpResponse:
    url = urllib.parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "User-Agent": "mlb-analytics-hub-production-contract/4.69",
        },
        method="GET",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            status = response.status
            headers = {key.lower(): value for key, value in response.headers.items()}
    except urllib.error.HTTPError as exc:
        body = exc.read()
        status = exc.code
        headers = {key.lower(): value for key, value in exc.headers.items()}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ContractError(f"{path} request failed: {exc}") from exc
    return HttpResponse(
        status=status,
        headers=headers,
        body=body,
        elapsed_seconds=time.perf_counter() - started,
    )


def validate_page(contract: PageContract, response: HttpResponse) -> set[str]:
    _require(response.status == 200, f"{contract.path} returned {response.status}")
    _require(
        response.elapsed_seconds <= PAGE_BUDGET_SECONDS,
        f"{contract.path} exceeded {PAGE_BUDGET_SECONDS:g}s page budget "
        f"({response.elapsed_seconds:.2f}s)",
    )
    content_type = response.headers.get("content-type", "").lower()
    _require("text/html" in content_type, f"{contract.path} is not HTML")
    html = response.text()
    _require(len(html) >= 500, f"{contract.path} returned an incomplete page shell")
    _require(
        contract.marker.lower() in html.lower(),
        f"{contract.path} is missing marker {contract.marker!r}",
    )
    _require(
        re.search(r"""name\s*=\s*["']viewport["']""", html, re.IGNORECASE)
        is not None,
        f"{contract.path} is missing the mobile viewport contract",
    )
    parser = _StaticAssetParser()
    parser.feed(html)
    return parser.paths


def validate_page_with_retry(
    *,
    base_url: str,
    contract: PageContract,
    fetcher: Callable[[str, str, float], HttpResponse],
    attempts: int,
    retry_delay: float,
    sleeper: Callable[[float], None],
) -> set[str]:
    """Require one fast, valid page response while tolerating transient warm-up."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = fetcher(base_url, contract.path, 5)
            assets = validate_page(contract, response)
            print(
                f"PASS page {contract.path} ({response.elapsed_seconds:.2f}s)",
                flush=True,
            )
            return assets
        except ContractError as exc:
            last_error = exc
            if attempt == attempts:
                break
            print(
                f"WAIT page {contract.path}: {exc}; retrying "
                f"({attempt + 1}/{attempts})",
                flush=True,
            )
            sleeper(retry_delay)
    raise ContractError(
        f"page {contract.path} failed after {attempts} attempts: {last_error}"
    )


def validate_recommendation_evidence(row: dict[str, Any], index: int) -> None:
    receipt = row.get("evidenceReceipt")
    _require(isinstance(receipt, dict), f"edge {index} lacks evidence receipt")
    _require(
        receipt.get("contractVersion") == RECOMMENDATION_EVIDENCE_VERSION,
        f"edge {index} has an unexpected evidence contract version",
    )
    _require(
        receipt.get("candidateId") == row.get("canonicalCandidateId"),
        f"edge {index} evidence candidate identity mismatch",
    )
    _require(
        receipt.get("fingerprint") == row.get("canonicalFingerprint"),
        f"edge {index} evidence fingerprint mismatch",
    )

    selection = receipt.get("selection")
    quoted = receipt.get("price")
    model = receipt.get("model")
    market_evidence = receipt.get("market")
    validation = receipt.get("validation")
    for name, value in (
        ("selection", selection),
        ("price", quoted),
        ("model", model),
        ("market", market_evidence),
        ("validation", validation),
    ):
        _require(isinstance(value, dict), f"edge {index} evidence lacks {name}")

    market_key = row.get("canonicalMarketKey") or row.get("marketKey")
    _require(
        selection.get("marketKey") == market_key,
        f"edge {index} evidence market identity mismatch",
    )
    _require(
        str(selection.get("side") or "").lower()
        == str(row.get("canonicalSide") or row.get("side") or "").lower(),
        f"edge {index} evidence side mismatch",
    )
    _require(
        selection.get("line") == row.get("line"),
        f"edge {index} evidence line mismatch",
    )

    price = row.get("canonicalPrice")
    book = row.get("canonicalBook")
    age = quoted.get("ageSeconds")
    _require(quoted.get("american") == price, f"edge {index} evidence price mismatch")
    _require(
        str(quoted.get("book") or "").lower() == str(book or "").lower(),
        f"edge {index} evidence book mismatch",
    )
    _require(
        isinstance(age, (int, float))
        and not isinstance(age, bool)
        and 0 <= age <= MAX_EVIDENCE_ODDS_AGE_SECONDS,
        f"edge {index} evidence odds age is invalid",
    )
    _require(
        quoted.get("maximumAgeSeconds") == MAX_EVIDENCE_ODDS_AGE_SECONDS
        and quoted.get("fresh") is True,
        f"edge {index} evidence freshness policy mismatch",
    )
    observed_at = str(quoted.get("observedAt") or "")
    _require(
        observed_at == str(row.get("oddsUpdatedAt") or ""),
        f"edge {index} evidence timestamp mismatch",
    )
    try:
        datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(
            f"edge {index} evidence timestamp is invalid"
        ) from exc

    model_probability = model.get("probability")
    implied_probability = market_evidence.get("impliedProbability")
    fair_probability = market_evidence.get("fairProbability")
    evidence_edge = market_evidence.get("edge")
    for name, value in (
        ("model probability", model_probability),
        ("implied probability", implied_probability),
        ("fair probability", fair_probability),
    ):
        _require(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and 0 < value < 1,
            f"edge {index} evidence {name} is invalid",
        )
    _require(bool(model.get("version")), f"edge {index} evidence lacks model version")
    _require(
        isinstance(evidence_edge, (int, float))
        and not isinstance(evidence_edge, bool)
        and evidence_edge > 0,
        f"edge {index} evidence edge is invalid",
    )
    canonical_edge = row.get("canonicalEdge")
    _require(
        isinstance(canonical_edge, (int, float))
        and abs(evidence_edge - canonical_edge) < 0.000001,
        f"edge {index} evidence edge mismatch",
    )

    _require(
        validation.get("actionable") is True
        and str(validation.get("actionabilityStage") or "").lower() == "actionable",
        f"edge {index} evidence is not actionable",
    )
    _require(
        str(validation.get("calibrationStatus") or "").lower() == "passed"
        and str(validation.get("marketGateStatus") or "").lower() == "promoted",
        f"edge {index} evidence lacks promoted calibration",
    )
    _require(
        bool(validation.get("candidateIntegrityVersion"))
        and bool(validation.get("marketValidationVersion")),
        f"edge {index} evidence lacks validation versions",
    )
    _require(
        bool(str(receipt.get("explanation") or "").strip()),
        f"edge {index} evidence lacks explanation",
    )


def _validate_multi_book_shopping(row: dict[str, Any], index: int) -> None:
    shopping = row.get("multiBookShopping")
    _require(isinstance(shopping, dict), f"edge {index} lacks multi-book shopping")
    _require(shopping.get("version") == "5.9", f"edge {index} shopping version changed")
    _require(
        shopping.get("sourceDecisionVersion") == "5.3.0",
        f"edge {index} is not backed by Phase 5.3",
    )
    state = str(shopping.get("state") or "").lower()
    _require(
        state in {"ready", "computing", "partial", "stale", "failed", "unavailable"},
        f"edge {index} has invalid shopping state {state!r}",
    )
    _require(
        shopping.get("reviewRequired") is True
        and shopping.get("changesRecommendation") is False,
        f"edge {index} shopping changed the recommendation boundary",
    )
    provider = shopping.get("providerHealth")
    provider_fields = {
        "provider", "state", "configured", "capturedAt", "eventCount",
        "fetchedEventCount", "degradedEventCount", "message",
    }
    _require(
        isinstance(provider, dict) and set(provider) == provider_fields,
        f"edge {index} provider health changed its privacy allowlist",
    )
    _require(
        provider.get("state") in {
            "ready", "computing", "partial", "stale", "failed", "unavailable",
        },
        f"edge {index} has invalid provider state",
    )
    consensus = shopping.get("consensus")
    price = shopping.get("priceShopping")
    decision = shopping.get("decision")
    _require(isinstance(consensus, dict), f"edge {index} lacks shopping consensus")
    _require(isinstance(price, dict), f"edge {index} lacks price shopping")
    _require(isinstance(decision, dict), f"edge {index} lacks shopping decision")
    _require(decision.get("approved") is False, f"edge {index} was auto-approved")
    required = consensus.get("requiredBooks")
    accepted = consensus.get("acceptedBookCount")
    rejected = consensus.get("rejectedQuoteCount")
    _require(
        isinstance(required, int) and required >= 2,
        f"edge {index} has invalid required book count",
    )
    _require(
        isinstance(accepted, int) and accepted >= 0
        and isinstance(rejected, int) and rejected >= 0,
        f"edge {index} has invalid quote denominators",
    )
    quotes = price.get("quotes")
    _require(isinstance(quotes, list), f"edge {index} quotes must be a list")
    _require(len(quotes) == accepted, f"edge {index} accepted quote count changed")
    quote_fields = {
        "book", "source", "capturedAt", "ageSeconds", "line", "overPrice",
        "underPrice", "selectedPrice", "fairProbability",
    }
    for quote_index, quote in enumerate(quotes):
        _require(
            isinstance(quote, dict) and set(quote) == quote_fields,
            f"edge {index} quote {quote_index} changed its allowlist",
        )
        _require(bool(quote.get("book")), f"edge {index} quote {quote_index} lacks book")
        _require(bool(quote.get("source")), f"edge {index} quote {quote_index} lacks source")
        _require(bool(quote.get("capturedAt")), f"edge {index} quote {quote_index} lacks timestamp")
        _require(
            isinstance(quote.get("ageSeconds"), int)
            and 0 <= quote.get("ageSeconds") <= 300,
            f"edge {index} quote {quote_index} is stale",
        )
        for field in ("overPrice", "underPrice", "selectedPrice"):
            value = quote.get(field)
            _require(
                isinstance(value, (int, float)) and value != 0 and abs(value) >= 100,
                f"edge {index} quote {quote_index} has invalid {field}",
            )
    if state == "ready":
        _require(accepted >= required, f"edge {index} ready consensus lacks books")
        _require(
            provider.get("state") == "ready",
            f"edge {index} ready consensus has degraded provider",
        )
    if state in {"stale", "failed", "unavailable"}:
        _require(not quotes, f"edge {index} exposed quotes in {state} state")
    encoded = json.dumps(shopping, sort_keys=True).lower()
    for forbidden in (
        '"bankroll"', '"stakedollars"', '"stakepreview"', '"rejectedquotes"',
    ):
        _require(forbidden not in encoded, f"edge {index} shopping exposed {forbidden}")


def validate_actionable_edges(payload: Any) -> None:
    _require(isinstance(payload, dict), "edges payload must be an object")
    _require(payload.get("success") is True, "edges payload is not successful")
    edges = payload.get("edges")
    _require(isinstance(edges, list), "edges payload must include an edges list")
    _require(payload.get("count") == len(edges), "edges count does not match rows")
    state = str(payload.get("computationState") or "").lower()
    _require(
        state in {"ready", "computing", "failed", "unavailable"},
        f"edges payload has invalid computation state {state!r}",
    )
    if state in {"failed", "unavailable"}:
        _require(not edges, f"{state} edges payload must fail closed with zero rows")
        _require(bool(payload.get("message")), f"{state} edges payload needs a state message")
        raise ContractError(f"edges computation is {state}: {payload.get('message')}")
    if state == "computing":
        _require(payload.get("computing") is True, "computing state flag is inconsistent")
        _require(not edges, "computing edges payload must fail closed with zero rows")
        _require(bool(payload.get("message")), "computing edges payload needs a state message")
        job = payload.get("scanJob")
        _require(isinstance(job, dict), "computing edges payload needs durable job state")
        _require(bool(job.get("id")), "computing edge job is missing identity")
        _require(
            job.get("status") in {"queued", "running"},
            f"computing edge job has terminal status {job.get('status')!r}",
        )
        _require(
            isinstance(job.get("elapsedSeconds"), int)
            and job.get("elapsedSeconds") >= 0,
            "computing edge job needs bounded elapsed time",
        )
        _require(
            job.get("timeoutSeconds") == 600,
            "computing edge job completion window changed unexpectedly",
        )
        return

    _require(payload.get("computing") is not True, "ready edges cannot be computing")
    _require(
        payload.get("multiBookShoppingVersion") == "5.9",
        "edges multi-book shopping version changed",
    )
    provider = payload.get("oddsProviderHealth")
    _require(isinstance(provider, dict), "edges lack odds provider health")
    for index, row in enumerate(edges):
        _require(isinstance(row, dict), f"edge {index} is not an object")
        stage = str(row.get("actionabilityStage") or "").lower()
        _require(row.get("actionable") is True, f"edge {index} is not actionable")
        _require(not stage or stage == "actionable", f"edge {index} has stage {stage!r}")
        for field in (
            "player",
            "playerId",
            "canonicalCandidateId",
            "canonicalFingerprint",
        ):
            _require(bool(row.get(field)), f"edge {index} is missing {field}")
        market = row.get("canonicalMarketKey") or row.get("marketKey")
        _require(bool(market), f"edge {index} is missing canonical market identity")
        price = row.get("canonicalPrice")
        if price is None:
            price = row.get("bestPrice", row.get("bestAvailablePrice"))
        _require(
            isinstance(price, (int, float))
            and price != 0
            and abs(price) >= 100,
            f"edge {index} has invalid sportsbook price",
        )
        book = (
            row.get("canonicalBook")
            or row.get("bestBook")
            or row.get("bestAvailableBook")
            or row.get("bookmaker")
        )
        normalized_book = str(book or "").strip().lower()
        _require(
            bool(normalized_book) and normalized_book not in INVALID_BOOKS,
            f"edge {index} has invalid sportsbook identity",
        )
        edge = row.get("edgePct")
        if edge is None:
            edge = row.get("canonicalEdge", row.get("edge"))
        _require(
            isinstance(edge, (int, float)) and edge > 0,
            f"edge {index} has non-positive edge",
        )
        validate_recommendation_evidence(row, index)
        _validate_multi_book_shopping(row, index)


def _validate_health(payload: Any, expected_sha: str | None) -> None:
    _require(isinstance(payload, dict), "health payload must be an object")
    _require(payload.get("status") == "ok", f"unexpected health payload: {payload}")
    if expected_sha:
        _require(
            payload.get("version") == expected_sha,
            f"expected deployed commit {expected_sha}, got {payload.get('version')}",
        )


def _validate_ready(payload: Any) -> None:
    _require(isinstance(payload, dict), "ready payload must be an object")
    _require(payload.get("status") == "ready", f"unexpected ready payload: {payload}")
    jobs = payload.get("jobs")
    _require(isinstance(jobs, dict), "ready payload must include durable worker health")
    _require(jobs.get("connected") is True, "ready payload reports Redis disconnected")
    _require(jobs.get("workerReady") is True, "ready payload reports worker unavailable")


def _validate_daily_decision_board(board: Any) -> None:
    _require(isinstance(board, dict), "daily decision board must be an object")
    _require(board.get("version") == "5.5", "daily decision board version changed")
    _require(board.get("failClosed") is True, "daily decision board must fail closed")
    _require(board.get("rawRejectedRowsIncluded") is False, "daily board exposed rejected rows")
    _require(board.get("noBetIsValidDecision") is True, "daily board must preserve no-bet")
    _require(board.get("maximumCards") == 8, "daily board card limit changed")


def _validate_multi_book_contract(contract: Any) -> None:
    _require(isinstance(contract, dict), "multi-book shopping must be an object")
    _require(contract.get("version") == "5.9", "multi-book shopping version changed")
    _require(
        contract.get("sourceDecisionEngineVersion") == "5.3.0",
        "multi-book shopping lost Phase 5.3 provenance",
    )
    _require(contract.get("minimumFreshBooks") == 2, "multi-book minimum changed")
    _require(contract.get("maximumQuoteAgeSeconds") == 300, "shopping freshness changed")
    _require(
        contract.get("visibleOnCards") == [
            "daily_decision_board",
            "personalized_signal",
            "saved_player_opportunity",
            "eligible_alert",
        ],
        "multi-book card coverage changed",
    )
    _require(contract.get("rawRejectedQuotesIncluded") is False, "raw quotes leaked")
    _require(contract.get("bankrollIncluded") is False, "bankroll leaked")
    _require(contract.get("stakeDollarsIncluded") is False, "stake dollars leaked")
    _require(contract.get("changesRecommendation") is False, "shopping changed picks")
    _require(contract.get("serverMutation") is False, "shopping must be read only")
    _require(contract.get("failClosed") is True, "shopping must fail closed")


def _validate_guided_parlay_contract(contract: Any) -> None:
    _require(isinstance(contract, dict), "guided parlays must be an object")
    _require(contract.get("version") == "5.10", "guided parlay version changed")
    _require(contract.get("minimumVerifiedLegs") == 2, "guided minimum changed")
    _require(contract.get("maximumGuidedLegs") == 4, "guided maximum changed")
    _require(
        contract.get("requiresEvidenceReceiptVersion") == "4.69",
        "guided parlays lost recommendation evidence",
    )
    _require(
        contract.get("requiresMultiBookShoppingVersion") == "5.9",
        "guided parlays lost multi-book evidence",
    )
    _require(
        contract.get("requiresReadyMultiBookConsensus") is True,
        "guided parlays admitted degraded consensus",
    )
    _require(
        contract.get("correlationWarningsRequired") is True,
        "guided parlays lost correlation warnings",
    )
    _require(
        contract.get("unresolvedSameGameCorrelationTrackable") is False,
        "unresolved same-game correlation became trackable",
    )
    _require(
        contract.get("combinedRiskExplanationRequired") is True,
        "guided parlays lost combined-risk explanations",
    )
    _require(
        contract.get("referencePriceIsBookOffer") is False,
        "reference odds were misrepresented as a book offer",
    )
    _require(contract.get("reviewRequired") is True, "guided review gate changed")
    _require(contract.get("approved") is False, "guided parlays cannot auto-approve")
    _require(contract.get("readOnly") is True, "guided suggestions must be read only")
    _require(contract.get("serverMutation") is False, "guided read must not mutate")
    _require(contract.get("failClosed") is True, "guided parlays must fail closed")


def _validate_monetization_growth_contract(contract: Any) -> None:
    _require(isinstance(contract, dict), "monetization growth must be an object")
    _require(contract.get("version") == "5.11", "monetization version changed")
    _require(
        contract.get("sourceEndpoint") == "/api/monetization/status",
        "monetization status endpoint changed",
    )
    _require(
        contract.get("rolloutState") == "identity_required",
        "paid rollout bypassed verified identity",
    )
    _require(
        contract.get("freeUsageEnforcementMode") == "shadow",
        "anonymous usage unexpectedly became a paid boundary",
    )
    _require(
        contract.get("premiumEntitlementSource")
        == "server_verified_subscription",
        "Premium entitlement source changed",
    )
    _require(
        contract.get("clientStorageCanGrantPremium") is False,
        "client storage can grant Premium",
    )
    _require(
        contract.get("anonymousSessionCanGrantPremium") is False,
        "anonymous session can grant Premium",
    )
    _require(contract.get("checkoutAvailable") is False, "checkout opened early")
    _require(
        contract.get("requiresVerifiedCustomerIdentity") is True,
        "customer identity gate was removed",
    )
    _require(
        contract.get("requiresWebhookReconciliation") is True,
        "billing reconciliation gate was removed",
    )
    _require(
        contract.get("growthPersistence") == "device_private",
        "growth persistence boundary changed",
    )
    _require(
        contract.get("serverAnalyticsCollection") is False,
        "growth analytics unexpectedly reached the server",
    )
    _require(
        contract.get("rawPersonalDataIncluded") is False,
        "monetization contract exposed personal data",
    )
    _require(contract.get("serverMutation") is False, "monetization read mutates")
    _require(contract.get("failClosed") is True, "monetization must fail closed")


def _validate_accuracy_control_plane_journey(contract: Any) -> None:
    _require(isinstance(contract, dict), "accuracy control plane must be an object")
    _require(contract.get("version") == "6.0", "accuracy control plane version changed")
    _require(
        contract.get("sourceEndpoint") == "/api/accuracy/control-plane?window=90",
        "accuracy control-plane endpoint changed",
    )
    _require(
        contract.get("benchmarkType") == "side_correct_two_way_power_devig_close",
        "accuracy closing benchmark changed",
    )
    _require(contract.get("minimumPairedSample") == 500, "accuracy sample gate changed")
    _require(contract.get("minimumClvSample") == 500, "accuracy CLV gate changed")
    _require(contract.get("beatCloseTarget") == 0.524, "Beat Close target changed")
    for field in (
        "requiresImmutablePredictionReceipt",
        "requiresClosingBenchmarkReceipt",
        "requiresExactLine",
        "requiresAcceptedClosingIntegrity",
        "requiresBrierConfidence",
        "requiresBeatCloseConfidence",
        "industryClaimDefaultsToFalse",
        "failClosed",
    ):
        _require(contract.get(field) is True, f"accuracy contract lost {field}")
    for field in (
        "privateTrackerFieldsIncluded",
        "automaticModelChange",
        "automaticThresholdChange",
        "serverMutation",
    ):
        _require(contract.get(field) is False, f"accuracy contract opened {field}")


def _validate_accuracy_intelligence_journey(contract: Any) -> None:
    _require(isinstance(contract, dict), "accuracy intelligence program must be an object")
    _require(contract.get("version") == "6.5", "accuracy intelligence version changed")
    _require(
        contract.get("sourceEndpoint") == "/api/accuracy/intelligence?window=120",
        "accuracy intelligence endpoint changed",
    )
    _require(
        contract.get("phaseVersions") == {
            "errorAtlas": "6.1",
            "championChallenger": "6.2",
            "driftControl": "6.3",
            "simulationCalibration": "6.4",
            "policyLab": "6.5",
        },
        "accuracy intelligence phase versions changed",
    )
    _require(
        contract.get("requiresPredictionReceiptVersion") == "5.4.0"
        and contract.get("requiresClosingBenchmarkReceiptVersion") == "6.0"
        and contract.get("requiresIntelligenceEvidenceReceiptVersion") == "6.5.0",
        "accuracy intelligence receipt chain changed",
    )
    for field, expected in (
        ("minimumContextSample", 30),
        ("minimumChallengerTotalSample", 300),
        ("minimumSimulationSample", 100),
        ("minimumCorrelationPairs", 50),
    ):
        _require(contract.get(field) == expected, f"accuracy intelligence changed {field}")
    for field in (
        "driftMayDowngradeOrSuppress",
        "humanReviewRequired",
        "failClosed",
    ):
        _require(contract.get(field) is True, f"accuracy intelligence lost {field}")
    for field in (
        "unverifiedCorrelationTrackable",
        "rawRowsIncluded",
        "automaticModelPromotion",
        "automaticRetraining",
        "automaticProbabilityChange",
        "automaticThresholdChange",
        "automaticStakingChange",
        "serverMutation",
    ):
        _require(contract.get(field) is False, f"accuracy intelligence opened {field}")


def _validate_journey(
    payload: Any,
    *,
    require_daily_board: bool = True,
    require_multi_book: bool = True,
    require_guided_parlays: bool = True,
    require_monetization_growth: bool = True,
    require_accuracy_control_plane: bool = True,
    require_accuracy_intelligence: bool = True,
) -> None:
    _require(isinstance(payload, dict), "journey payload must be an object")
    stages = [stage.get("key") for stage in payload.get("stages", [])]
    alerts = payload.get("alerts", {})
    board = payload.get("dailyDecisionBoard", {})
    multi_book = payload.get("productionMultiBookShopping", {})
    guided = payload.get("guidedParlays", {})
    monetization = payload.get("monetizationGrowth", {})
    accuracy = payload.get("accuracyControlPlane", {})
    intelligence = payload.get("accuracyIntelligenceProgram", {})
    _require(payload.get("success") is True, "journey payload is not successful")
    _require(payload.get("version") == "4.64", "journey version changed unexpectedly")
    _require(
        stages == ["discover", "validate", "track", "learn"],
        f"journey stages changed unexpectedly: {stages}",
    )
    _require(alerts.get("failClosed") is True, "alerts must fail closed")
    _require(alerts.get("serverPersistence") is False, "alerts must remain device-private")
    _require(
        alerts.get("freshness", {}).get("maximumOddsAgeSeconds") == 900,
        "alert freshness contract changed unexpectedly",
    )
    if require_daily_board or board:
        _validate_daily_decision_board(board)
    if require_multi_book or multi_book:
        _validate_multi_book_contract(multi_book)
    if require_guided_parlays or guided:
        _validate_guided_parlay_contract(guided)
    if require_monetization_growth or monetization:
        _validate_monetization_growth_contract(monetization)
    if require_accuracy_control_plane or accuracy:
        _validate_accuracy_control_plane_journey(accuracy)
    if require_accuracy_intelligence or intelligence:
        _validate_accuracy_intelligence_journey(intelligence)


def _validate_journey_baseline(payload: Any) -> None:
    # Pull requests validate the currently deployed Main release, which cannot
    # expose a new contract before merge. Post-deploy validation remains strict.
    _validate_journey(
        payload,
        require_daily_board=False,
        require_multi_book=False,
        require_guided_parlays=False,
        require_monetization_growth=False,
        require_accuracy_control_plane=False,
        require_accuracy_intelligence=False,
    )


def _validate_monetization_status(payload: Any) -> None:
    _require(isinstance(payload, dict), "monetization status must be an object")
    _require(payload.get("success") is True, "monetization status is not successful")
    _require(payload.get("version") == "5.11", "monetization status version changed")
    _require(
        payload.get("rolloutState") == "identity_required",
        "monetization status bypassed identity",
    )
    plans = payload.get("plans")
    _require(isinstance(plans, list) and len(plans) == 2, "plan catalog changed")
    _require(
        [plan.get("key") for plan in plans] == ["free", "premium"],
        "plan order or identity changed",
    )
    _require(plans[0].get("availability") == "available", "Free became unavailable")
    _require(plans[1].get("availability") == "preview", "Premium opened early")
    usage = payload.get("freeUsage")
    _require(isinstance(usage, dict), "free usage contract is missing")
    _require(usage.get("enforcementMode") == "shadow", "usage enforcement opened early")
    _require(usage.get("hardLimitEnabled") is False, "anonymous hard limit opened early")
    entitlement = payload.get("premiumEntitlement")
    _require(isinstance(entitlement, dict), "Premium entitlement contract is missing")
    _require(entitlement.get("state") == "unavailable", "Premium entitlement opened")
    _require(entitlement.get("clientStorageCanGrant") is False, "client can grant Premium")
    _require(entitlement.get("failClosed") is True, "Premium does not fail closed")
    billing = payload.get("billing")
    _require(isinstance(billing, dict), "billing receipt is missing")
    _require(billing.get("state") == "identity_required", "billing state changed")
    _require(billing.get("checkoutAvailable") is False, "checkout opened early")
    _require(
        isinstance(billing.get("blockers"), list) and len(billing.get("blockers")) == 3,
        "billing blockers changed",
    )
    referrals = payload.get("referrals")
    analytics = payload.get("conversionAnalytics")
    _require(
        isinstance(referrals, dict)
        and referrals.get("persistence") == "device_private"
        and referrals.get("rawPersonalDataIncluded") is False,
        "referral privacy boundary changed",
    )
    _require(
        isinstance(analytics, dict)
        and analytics.get("persistence") == "device_private"
        and analytics.get("serverCollection") is False
        and analytics.get("rawPersonalDataIncluded") is False,
        "conversion analytics privacy boundary changed",
    )
    _require(payload.get("readOnly") is True, "monetization status is not read only")
    _require(payload.get("serverMutation") is False, "monetization status mutates")
    _require(payload.get("rawPersonalDataIncluded") is False, "status exposed PII")
    _require(payload.get("failClosed") is True, "monetization status is not fail closed")


def _validate_games(payload: Any) -> None:
    _require(isinstance(payload, dict), "games payload must be an object")
    _require(payload.get("success") is True, "games payload is not successful")
    games = payload.get("games")
    _require(isinstance(games, list), "games payload must include a games list")
    _require(payload.get("count") == len(games), "games count does not match rows")


def _validate_guided_parlays(payload: Any) -> None:
    _require(isinstance(payload, dict), "guided parlay payload must be an object")
    _require(payload.get("success") is True, "guided parlay payload is not successful")
    _require(payload.get("version") == "5.10", "guided parlay payload version changed")
    state = payload.get("state")
    _require(
        state in {
            "ready", "no_verified_combinations", "computing", "failed", "unavailable",
        },
        f"guided parlay payload has invalid state {state!r}",
    )
    _require(payload.get("minimumVerifiedLegs") == 2, "guided minimum changed")
    _require(payload.get("maximumGuidedLegs") == 4, "guided maximum changed")
    _require(
        payload.get("requiresEvidenceReceiptVersion") == "4.69",
        "guided payload lost evidence lineage",
    )
    _require(
        payload.get("requiresMultiBookShoppingVersion") == "5.9",
        "guided payload lost shopping lineage",
    )
    _require(payload.get("reviewRequired") is True, "guided payload bypassed review")
    _require(payload.get("approved") is False, "guided payload auto-approved")
    _require(payload.get("readOnly") is True, "guided payload is not read only")
    _require(payload.get("failClosed") is True, "guided payload is not fail closed")
    for field in ("candidateCount", "verifiedCandidateCount", "withheldCandidateCount"):
        value = payload.get(field)
        _require(isinstance(value, int) and value >= 0, f"guided {field} is invalid")
    _require(
        payload.get("candidateCount")
        == payload.get("verifiedCandidateCount") + payload.get("withheldCandidateCount"),
        "guided candidate denominators do not reconcile",
    )
    parlays = payload.get("parlays")
    _require(isinstance(parlays, list), "guided parlays must be a list")
    if state == "ready":
        _require(bool(parlays), "ready guided payload has no combinations")
    if state in {"no_verified_combinations", "computing", "failed", "unavailable"}:
        _require(not parlays, f"guided {state} payload exposed combinations")

    leg_fields = {
        "canonicalCandidateId", "canonicalFingerprint", "gamePk", "player",
        "playerId", "team", "opp", "marketKey", "marketLabel", "side", "line",
        "modelProbability", "bestPrice", "bestBook", "quoteCapturedAt",
        "quoteExpiresAt", "acceptedBookCount", "evidenceReceiptVersion",
        "multiBookShoppingVersion", "verified",
    }
    for index, parlay in enumerate(parlays):
        _require(isinstance(parlay, dict), f"guided parlay {index} is not an object")
        _require(parlay.get("version") == "5.10", f"guided parlay {index} version changed")
        _require(parlay.get("state") == "ready", f"guided parlay {index} is not ready")
        _require(parlay.get("readOnly") is True, f"guided parlay {index} is not read only")
        _require(parlay.get("serverMutation") is False, f"guided parlay {index} mutates")
        legs = parlay.get("legs")
        _require(isinstance(legs, list), f"guided parlay {index} legs changed")
        _require(2 <= len(legs) <= 4, f"guided parlay {index} leg count changed")
        _require(
            parlay.get("verifiedLegCount") == len(legs),
            f"guided parlay {index} verification denominator changed",
        )
        candidate_ids = set()
        for leg_index, leg in enumerate(legs):
            _require(
                isinstance(leg, dict) and set(leg) == leg_fields,
                f"guided parlay {index} leg {leg_index} changed its allowlist",
            )
            _require(leg.get("verified") is True, f"guided leg {leg_index} is unverified")
            _require(
                leg.get("evidenceReceiptVersion") == "4.69",
                f"guided leg {leg_index} lost evidence lineage",
            )
            _require(
                leg.get("multiBookShoppingVersion") == "5.9",
                f"guided leg {leg_index} lost shopping lineage",
            )
            _require(
                isinstance(leg.get("acceptedBookCount"), int)
                and leg.get("acceptedBookCount") >= 2,
                f"guided leg {leg_index} lacks two books",
            )
            _require(bool(leg.get("bestBook")), f"guided leg {leg_index} lacks book")
            _require(
                isinstance(leg.get("bestPrice"), (int, float))
                and leg.get("bestPrice") != 0
                and abs(leg.get("bestPrice")) >= 100,
                f"guided leg {leg_index} has invalid price",
            )
            probability = leg.get("modelProbability")
            _require(
                isinstance(probability, (int, float)) and 0 < probability < 1,
                f"guided leg {leg_index} has invalid probability",
            )
            candidate_id = leg.get("canonicalCandidateId")
            _require(bool(candidate_id), f"guided leg {leg_index} lacks identity")
            _require(candidate_id not in candidate_ids, "guided parlay duplicated a leg")
            candidate_ids.add(candidate_id)

        correlation = parlay.get("correlation")
        _require(isinstance(correlation, dict), f"guided parlay {index} lacks correlation")
        _require(
            correlation.get("state") in {"clear", "measured"},
            f"guided parlay {index} exposed unresolved correlation",
        )
        if correlation.get("state") == "measured":
            _require(
                isinstance(correlation.get("sameGamePairCount"), int)
                and correlation.get("sameGamePairCount") > 0
                and correlation.get("measuredPairCount")
                == correlation.get("sameGamePairCount")
                and correlation.get("unresolvedPairCount") == 0,
                f"guided parlay {index} has incomplete measured correlation",
            )
            _require(
                correlation.get("method") == "pairwise_adjusted"
                and isinstance(correlation.get("adjustmentFactor"), (int, float))
                and 0.5 <= correlation.get("adjustmentFactor") <= 1.5,
                f"guided parlay {index} has invalid measured adjustment",
            )
        _require(
            isinstance(correlation.get("warnings"), list)
            and correlation.get("warnings"),
            f"guided parlay {index} lacks correlation warning",
        )
        risk = parlay.get("combinedRisk")
        _require(isinstance(risk, dict), f"guided parlay {index} lacks combined risk")
        combined = risk.get("combinedProbability")
        miss = risk.get("atLeastOneLegMissProbability")
        _require(
            isinstance(combined, (int, float)) and 0 < combined < 1,
            f"guided parlay {index} has invalid combined probability",
        )
        _require(
            isinstance(miss, (int, float)) and abs(combined + miss - 1.0) < 1e-5,
            f"guided parlay {index} risk probabilities do not reconcile",
        )
        _require(
            isinstance(risk.get("explanations"), list)
            and len(risk.get("explanations")) >= 3,
            f"guided parlay {index} lacks risk explanations",
        )
        reference = parlay.get("referencePrice")
        _require(isinstance(reference, dict), f"guided parlay {index} lacks reference price")
        _require(
            reference.get("bookOfferVerified") is False,
            f"guided parlay {index} misrepresented a book offer",
        )
        decision = parlay.get("decision")
        _require(isinstance(decision, dict), f"guided parlay {index} lacks decision")
        _require(decision.get("reviewRequired") is True, "guided decision bypassed review")
        _require(decision.get("approved") is False, "guided decision auto-approved")
        _require(decision.get("trackable") is True, "ready guided decision is not trackable")
    encoded = json.dumps(payload, sort_keys=True).lower()
    for forbidden in ('"bankroll"', '"stakedollars"', '"payoutper100"'):
        _require(forbidden not in encoded, f"guided payload exposed {forbidden}")


def validate_completion_receipt(
    payload: Any,
    *,
    expected_sha: str | None,
    probe_date: str,
) -> None:
    _require(
        payload.get("computationState") == "ready",
        "durable scan has not converged to ready",
    )
    receipt = payload.get("completionReceipt")
    _require(isinstance(receipt, dict), "ready scan is missing completion receipt")
    _require(
        receipt.get("contractVersion") == "4.68",
        "completion receipt contract version changed",
    )
    _require(
        receipt.get("source") == "durable-worker",
        "scan completion was not attested by the durable worker",
    )
    _require(receipt.get("date") == probe_date, "completion receipt date mismatch")
    _require(bool(receipt.get("completedAt")), "completion receipt lacks timestamp")
    if expected_sha:
        _require(
            receipt.get("release") == expected_sha,
            f"completion receipt release mismatch: {receipt.get('release')}",
        )
    publication = payload.get("publicVerificationRelease")
    _require(
        isinstance(publication, dict),
        "ready scan is missing its Phase 5.6 publication receipt",
    )
    _require(
        publication.get("version") == "5.6",
        "public verification publication version changed",
    )
    _require(
        publication.get("persisted") is True,
        "public verification cohort was not persisted before publication",
    )
    for field in ("selectedCount", "newReleaseCount", "existingReleaseCount"):
        _require(
            isinstance(publication.get(field), int)
            and publication.get(field) >= 0,
            f"public verification publication field {field} is invalid",
        )
    _require(
        publication.get("privateTrackerFieldsIncluded") is False,
        "public verification publication receipt crossed the privacy boundary",
    )


def convergence_probe_date(
    expected_sha: str | None,
    *,
    today: date | None = None,
) -> str:
    base = today or datetime.now(timezone.utc).date()
    token = str(expected_sha or "deployment")
    offset = 1 + (sum(ord(char) for char in token) % 7)
    return (base + timedelta(days=offset)).isoformat()


def _validate_calibration(payload: Any) -> None:
    _require(isinstance(payload, dict), "calibration payload must be an object")
    _require(payload.get("success") is True, "calibration payload is not successful")
    _require(isinstance(payload.get("markets"), list), "calibration markets must be a list")


def _validate_tracker(payload: Any) -> None:
    _require(isinstance(payload, dict), "tracker payload must be an object")
    _require(payload.get("success") is True, "tracker payload is not successful")


def _validate_public_verification(payload: Any) -> None:
    _require(isinstance(payload, dict), "verification ledger must be an object")
    _require(payload.get("success") is True, "verification ledger is not successful")
    _require(payload.get("version") == "5.6", "verification version changed")
    _require(payload.get("readOnly") is True, "verification ledger must be read only")
    _require(payload.get("failClosed") is True, "verification ledger must fail closed")
    _require(payload.get("lossesOmitted") is False, "verification ledger may not omit losses")
    _require(
        payload.get("privateTrackerFieldsIncluded") is False,
        "verification ledger exposed private Tracker fields",
    )
    ledger = payload.get("ledger")
    metrics = payload.get("metrics")
    withheld = payload.get("withheld")
    _require(isinstance(ledger, list), "verification ledger rows must be a list")
    _require(isinstance(metrics, dict), "verification metrics must be an object")
    _require(
        metrics.get("releasedCount") == len(ledger),
        "verification released count does not match rows",
    )
    for field in (
        "gradedCount", "wins", "losses", "pending", "clvGradedCount",
        "roiEligibleCount",
    ):
        _require(
            isinstance(metrics.get(field), int) and metrics.get(field) >= 0,
            f"verification metric {field} is invalid",
        )
    _require(
        metrics.get("gradedCount") == metrics.get("wins") + metrics.get("losses"),
        "verification graded denominator is inconsistent",
    )
    _require(
        metrics.get("clvGradedCount") <= metrics.get("gradedCount"),
        "verification CLV denominator exceeds graded sample",
    )
    _require(
        isinstance(withheld, dict) and withheld.get("rawRowsIncluded") is False,
        "verification withheld rows crossed the public boundary",
    )
    allowed = {
        "publicId", "receiptFingerprint", "receiptVersion", "receiptVerified",
        "predictionFingerprint", "predictionReceiptVersion", "releasedAt", "gradedAt", "gamePk", "player", "marketKey", "side",
        "line", "probability", "sportsbook", "openingPrice", "closingPrice",
        "clvEdge", "result",
    }
    for index, row in enumerate(ledger):
        _require(isinstance(row, dict), f"verification row {index} is invalid")
        _require(set(row) == allowed, f"verification row {index} changed its allowlist")
        _require(row.get("receiptVerified") is True, f"verification row {index} is unverified")
        _require(row.get("receiptVersion") == "5.6", f"verification row {index} has an invalid publication receipt version")
        _require(row.get("predictionReceiptVersion") == "5.4.0", f"verification row {index} has an invalid prediction receipt version")
        _require(
            row.get("result") in {"pending", "win", "loss", "push"},
            f"verification row {index} has an invalid result",
        )
        _require(bool(row.get("receiptFingerprint")), f"verification row {index} lacks receipt")
        _require(bool(row.get("sportsbook")), f"verification row {index} lacks sportsbook")
        _require(
            isinstance(row.get("openingPrice"), int)
            and abs(row.get("openingPrice")) >= 100,
            f"verification row {index} lacks release price",
        )


def _validate_accuracy_control_plane(payload: Any) -> None:
    _require(isinstance(payload, dict), "accuracy control plane must be an object")
    _require(payload.get("success") is True, "accuracy control plane is not successful")
    _require(payload.get("version") == "6.0", "accuracy control-plane version changed")
    _require(payload.get("readOnly") is True, "accuracy control plane must be read only")
    _require(payload.get("failClosed") is True, "accuracy control plane must fail closed")
    _require(payload.get("privateTrackerFieldsIncluded") is False, "accuracy control plane exposed Tracker fields")
    _require(payload.get("automaticModelChange") is False, "accuracy read changed the model")
    _require(payload.get("automaticThresholdChange") is False, "accuracy read changed thresholds")
    _require(payload.get("serverMutation") is False, "accuracy read mutated server state")
    _require(
        payload.get("state") in {
            "insufficient_sample",
            "insufficient_clv_sample",
            "not_market_leading",
            "market_leading",
        },
        "accuracy control plane has an invalid state",
    )
    overall = payload.get("overall")
    coverage = payload.get("coverage")
    benchmark = payload.get("benchmark")
    policy = payload.get("claimPolicy")
    _require(isinstance(overall, dict), "accuracy overall scorecard is missing")
    _require(isinstance(coverage, dict), "accuracy coverage is missing")
    _require(isinstance(benchmark, dict), "accuracy benchmark is missing")
    _require(isinstance(policy, dict), "accuracy claim policy is missing")
    _require(overall.get("state") == payload.get("state"), "accuracy states disagree")
    paired = overall.get("pairedSampleSize")
    clv_count = overall.get("clvGradedCount")
    _require(isinstance(paired, int) and paired >= 0, "accuracy paired sample is invalid")
    _require(isinstance(clv_count, int) and 0 <= clv_count <= paired, "accuracy CLV sample is invalid")
    _require(coverage.get("pairedEligibleCount") == paired, "accuracy coverage denominator changed")
    _require(coverage.get("rawRowsIncluded") is False, "accuracy coverage exposed raw rows")
    _require(benchmark.get("version") == "6.0", "closing benchmark version changed")
    _require(
        benchmark.get("type") == "side_correct_two_way_power_devig_close",
        "closing benchmark type changed",
    )
    _require(benchmark.get("requiresExactLine") is True, "closing benchmark lost exact-line matching")
    _require(benchmark.get("requiresAcceptedClosingIntegrity") is True, "closing integrity gate changed")
    _require(policy.get("minimumPairedSample") == 500, "paired claim sample changed")
    _require(policy.get("minimumClvSample") == 500, "CLV claim sample changed")
    _require(policy.get("beatCloseTarget") == 0.524, "Beat Close claim target changed")
    claim = payload.get("industryClaimMade")
    _require(isinstance(claim, bool), "accuracy claim flag is invalid")
    _require(claim == (payload.get("state") == "market_leading"), "accuracy claim escaped its gate")
    _require(overall.get("claimEligible") is claim, "accuracy overall claim disagrees")


def _validate_accuracy_intelligence(payload: Any) -> None:
    _require(isinstance(payload, dict), "accuracy intelligence must be an object")
    _require(payload.get("success") is True, "accuracy intelligence is not successful")
    _require(payload.get("version") == "6.5", "accuracy intelligence version changed")
    _require(
        payload.get("state") in {"ready", "insufficient_sample"},
        "accuracy intelligence has an invalid state",
    )
    _require(payload.get("serverMutation") is False, "accuracy intelligence read mutated state")
    coverage = payload.get("coverage")
    phases = payload.get("phases")
    safety = payload.get("safety")
    _require(isinstance(coverage, dict), "accuracy intelligence coverage is missing")
    _require(isinstance(phases, dict), "accuracy intelligence phases are missing")
    _require(isinstance(safety, dict), "accuracy intelligence safety receipt is missing")
    _require(
        isinstance(coverage.get("verifiedObservationCount"), int)
        and coverage.get("verifiedObservationCount") >= 0,
        "accuracy intelligence verified denominator is invalid",
    )
    _require(
        isinstance(coverage.get("rejectedObservationCount"), int)
        and coverage.get("rejectedObservationCount") >= 0,
        "accuracy intelligence rejected denominator is invalid",
    )
    _require(coverage.get("rawRowsIncluded") is False, "accuracy intelligence exposed raw rows")
    expected_versions = {
        "errorAtlas": "6.1",
        "championChallenger": "6.2",
        "driftControl": "6.3",
        "simulationCalibration": "6.4",
        "policyLab": "6.5",
    }
    for name, version in expected_versions.items():
        phase = phases.get(name)
        _require(isinstance(phase, dict), f"accuracy intelligence lacks {name}")
        _require(phase.get("version") == version, f"accuracy intelligence changed {name}")
    atlas = phases["errorAtlas"]
    challenger = phases["championChallenger"]
    drift = phases["driftControl"]
    simulation = phases["simulationCalibration"]
    policy = phases["policyLab"]
    _require(atlas.get("minimumVisibleSample") == 30, "error atlas sample gate changed")
    _require(atlas.get("rawRowsIncluded") is False, "error atlas exposed raw rows")
    _require(challenger.get("automaticPromotion") is False, "challenger auto-promoted")
    _require(challenger.get("humanReviewRequired") is True, "challenger bypassed review")
    _require(drift.get("mayRetrainModel") is False, "drift auto-retrained")
    _require(drift.get("mayPromoteModel") is False, "drift auto-promoted")
    _require(simulation.get("unverifiedCorrelationTrackable") is False, "unverified correlation became trackable")
    _require(simulation.get("rawRowsIncluded") is False, "simulation exposed raw rows")
    _require(policy.get("automaticThresholdChange") is False, "policy auto-changed threshold")
    _require(policy.get("automaticStakingChange") is False, "policy auto-changed staking")
    for cohort in atlas.get("cohorts") or []:
        _require(
            isinstance(cohort, dict) and cohort.get("sampleSize", 0) >= 30,
            "error atlas exposed a sub-threshold cohort",
        )
    for report in challenger.get("challengers") or []:
        _require(isinstance(report, dict), "challenger report is invalid")
        if report.get("promotionEligible") is True:
            _require(
                report.get("state") == "review_candidate"
                and report.get("totalSampleSize", 0) >= 300
                and report.get("holdoutSampleSize", 0) >= 100
                and report.get("humanReviewRequired") is True,
                "challenger escaped its evidence gate",
            )
    markets = drift.get("markets")
    _require(isinstance(markets, dict), "drift markets are invalid")
    for market, report in markets.items():
        _require(isinstance(report, dict), f"drift market {market} is invalid")
        _require(
            report.get("recommendedAction")
            in {"none", "downgrade_confidence", "research_only", "no_bet"},
            f"drift market {market} has an invalid intervention",
        )
        _require(
            isinstance(report.get("featureDrift"), dict)
            and isinstance(report.get("providerHealth"), dict),
            f"drift market {market} lost feature or provider evidence",
        )
    for pair in simulation.get("correlationPairs") or []:
        _require(isinstance(pair, dict), "correlation pair is invalid")
        if pair.get("verified") is True:
            _require(
                pair.get("sampleSize", 0) >= 50
                and 0.5 <= pair.get("factor", 0) <= 1.5,
                "correlation pair escaped its sample or factor gate",
            )
    for proposal in policy.get("proposals") or []:
        _require(isinstance(proposal, dict), "policy proposal is invalid")
        _require(
            proposal.get("automaticApplication") is False
            and proposal.get("humanReviewRequired") is True
            and "currentMinimumExpectedValue" in proposal
            and "proposedMinimumExpectedValue" in proposal,
            "policy proposal escaped review or lost EV thresholds",
        )
    for field in (
        "readOnly",
        "failClosed",
        "humanReviewRequired",
    ):
        _require(safety.get(field) is True, f"accuracy intelligence safety lost {field}")
    for field in (
        "privateTrackerFieldsIncluded",
        "automaticModelPromotion",
        "automaticRetraining",
        "automaticProbabilityChange",
        "automaticThresholdChange",
        "automaticStakingChange",
    ):
        _require(safety.get(field) is False, f"accuracy intelligence safety opened {field}")


def _json_contract_with_retry(
    *,
    base_url: str,
    path: str,
    label: str,
    timeout: float,
    budget: float,
    validator: Callable[[Any], None],
    fetcher: Callable[[str, str, float], HttpResponse],
    attempts: int,
    retry_delay: float,
    sleeper: Callable[[float], None],
) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = fetcher(base_url, path, timeout)
            _require(response.status == 200, f"{label} returned {response.status}")
            _require(
                response.elapsed_seconds <= budget,
                f"{label} exceeded {budget:g}s budget ({response.elapsed_seconds:.2f}s)",
            )
            payload = response.json()
            validator(payload)
            print(f"PASS api {path} ({response.elapsed_seconds:.2f}s)", flush=True)
            return payload
        except ContractError as exc:
            last_error = exc
            if attempt == attempts:
                break
            print(
                f"WAIT api {path}: {exc}; retrying "
                f"({attempt + 1}/{attempts})",
                flush=True,
            )
            sleeper(retry_delay)
    raise ContractError(f"{label} failed after {attempts} attempts: {last_error}")


def wait_for_edge_convergence(
    *,
    base_url: str,
    expected_sha: str | None,
    probe_date: str,
    fetcher: Callable[[str, str, float], HttpResponse],
    attempts: int,
    retry_delay: float,
    sleeper: Callable[[float], None],
) -> Any:
    path = (
        f"/api/edges/today?date={urllib.parse.quote(probe_date)}"
        "&minEdge=0.03&limit=5"
    )
    if expected_sha:
        path += f"&requiredRelease={urllib.parse.quote(expected_sha)}"
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = fetcher(base_url, path, 10)
            _require(response.status == 200, f"convergence probe returned {response.status}")
            _require(
                response.elapsed_seconds <= 8,
                f"convergence probe exceeded 8s budget ({response.elapsed_seconds:.2f}s)",
            )
            payload = response.json()
            validate_actionable_edges(payload)
            if payload.get("computationState") == "ready":
                validate_completion_receipt(
                    payload,
                    expected_sha=expected_sha,
                    probe_date=probe_date,
                )
                print(
                    f"PASS durable worker convergence {probe_date} "
                    f"({response.elapsed_seconds:.2f}s)",
                    flush=True,
                )
                return payload
            wait_for_release(
                base_url=base_url,
                expected_sha=expected_sha,
                fetcher=fetcher,
                attempts=1,
                retry_delay=0,
                sleeper=sleeper,
            )
            last_error = ContractError(
                f"scan remains computing with job {payload.get('scanJob')}"
            )
        except ContractError as exc:
            last_error = exc
        if attempt == attempts:
            break
        print(
            f"WAIT durable worker convergence: {last_error}; "
            f"retrying ({attempt + 1}/{attempts})",
            flush=True,
        )
        sleeper(retry_delay)
    raise ContractError(
        f"durable worker failed to converge after {attempts} attempts: {last_error}"
    )


def wait_for_release(
    *,
    base_url: str,
    expected_sha: str | None,
    fetcher: Callable[[str, str, float], HttpResponse],
    attempts: int,
    retry_delay: float,
    sleeper: Callable[[float], None],
) -> None:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            health = fetcher(base_url, "/health", 3)
            _require(health.status == 200, f"/health returned {health.status}")
            _require(
                health.elapsed_seconds <= 2,
                f"/health exceeded 2s budget ({health.elapsed_seconds:.2f}s)",
            )
            _validate_health(health.json(), expected_sha)
            ready = fetcher(base_url, "/ready", 5)
            _require(ready.status == 200, f"/ready returned {ready.status}")
            _validate_ready(ready.json())
            print("PASS release identity and readiness", flush=True)
            return
        except ContractError as exc:
            last_error = exc
            if attempt == attempts:
                break
            print(
                f"WAIT release: {exc}; retrying ({attempt + 1}/{attempts})",
                flush=True,
            )
            sleeper(retry_delay)
    raise ContractError(f"release failed readiness gate: {last_error}")


def run_gate(
    *,
    base_url: str,
    expected_sha: str | None = None,
    fetcher: Callable[[str, str, float], HttpResponse] = fetch_url,
    release_attempts: int = 24,
    contract_attempts: int = 6,
    retry_delay: float = 5,
    sleeper: Callable[[float], None] = time.sleep,
    baseline_only: bool = False,
    settle_attempts: int = 61,
    settle_delay: float = 10,
) -> dict[str, int]:
    wait_for_release(
        base_url=base_url,
        expected_sha=expected_sha,
        fetcher=fetcher,
        attempts=release_attempts,
        retry_delay=retry_delay,
        sleeper=sleeper,
    )

    assets: set[str] = set()
    page_contracts = (
        PUBLIC_PAGE_CONTRACTS
        if baseline_only
        else PUBLIC_PAGE_CONTRACTS + PHASE_56_PAGE_CONTRACTS + PHASE_511_PAGE_CONTRACTS
    )
    for contract in page_contracts:
        assets.update(
            validate_page_with_retry(
                base_url=base_url,
                contract=contract,
                fetcher=fetcher,
                attempts=contract_attempts,
                retry_delay=retry_delay,
                sleeper=sleeper,
            )
        )

    for path in sorted(assets):
        response = fetcher(base_url, path, 5)
        _require(response.status == 200, f"asset {path} returned {response.status}")
        _require(bool(response.body), f"asset {path} returned an empty body")
        _require(
            response.elapsed_seconds <= ASSET_BUDGET_SECONDS,
            f"asset {path} exceeded {ASSET_BUDGET_SECONDS:g}s budget "
            f"({response.elapsed_seconds:.2f}s)",
        )
        print(f"PASS asset {path} ({response.elapsed_seconds:.2f}s)", flush=True)

    for path in ADMIN_READ_PATHS:
        response = fetcher(base_url, path, 5)
        _require(
            response.status in {401, 503},
            f"admin read {path} failed closed boundary with status {response.status}",
        )
        print(f"PASS admin boundary {path} ({response.status})", flush=True)

    journey_validator = (
        _validate_journey_baseline if baseline_only else _validate_journey
    )
    baseline_contracts = (
        ("/api/product/journey", "product journey", 5.0, journey_validator),
        ("/api/games/today", "today games", 8.0, _validate_games),
    )
    deployed_contracts = (
        (
            "/api/edges/today?minEdge=0.03&limit=5",
            "actionable edges",
            8.0,
            validate_actionable_edges,
        ),
        (
            "/api/parlay/auto",
            "guided parlays",
            8.0,
            _validate_guided_parlays,
        ),
        (
            "/api/calibration/markets?window=60",
            "market calibration",
            5.0,
            _validate_calibration,
        ),
        (
            "/api/tracker/performance?window=30",
            "tracker performance",
            5.0,
            _validate_tracker,
        ),
        (
            "/api/verification/ledger?window=90",
            "public verification ledger",
            5.0,
            _validate_public_verification,
        ),
        (
            "/api/accuracy/control-plane?window=90",
            "accuracy control plane",
            5.0,
            _validate_accuracy_control_plane,
        ),
        (
            "/api/accuracy/intelligence?window=120",
            "accuracy intelligence program",
            5.0,
            _validate_accuracy_intelligence,
        ),
        (
            "/api/monetization/status",
            "monetization readiness",
            5.0,
            _validate_monetization_status,
        ),
    )
    json_contracts = (
        baseline_contracts
        if baseline_only
        else baseline_contracts + deployed_contracts
    )
    for path, label, budget, validator in json_contracts:
        _json_contract_with_retry(
            base_url=base_url,
            path=path,
            label=label,
            timeout=budget + 2,
            budget=budget,
            validator=validator,
            fetcher=fetcher,
            attempts=contract_attempts,
            retry_delay=retry_delay,
            sleeper=sleeper,
        )

    convergence_checks = 0
    if not baseline_only:
        probe_date = convergence_probe_date(expected_sha)
        wait_for_edge_convergence(
            base_url=base_url,
            expected_sha=expected_sha,
            probe_date=probe_date,
            fetcher=fetcher,
            attempts=settle_attempts,
            retry_delay=settle_delay,
            sleeper=sleeper,
        )
        convergence_checks = 1

        # Prove the web tier is still healthy after the durable worker has
        # completed the deployment-scoped cold scan.
        wait_for_release(
            base_url=base_url,
            expected_sha=expected_sha,
            fetcher=fetcher,
            attempts=min(3, release_attempts),
            retry_delay=retry_delay,
            sleeper=sleeper,
        )
        print("PASS post-convergence web isolation", flush=True)

    summary = {
        "pages": len(page_contracts),
        "assets": len(assets),
        "admin_boundaries": len(ADMIN_READ_PATHS),
        "api_contracts": len(json_contracts) + 2,
        "worker_convergence": convergence_checks,
    }
    print(f"Production contract passed: {json.dumps(summary, sort_keys=True)}")
    return summary


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="https://mlb-analytics-hub.fly.dev",
    )
    parser.add_argument("--expected-sha")
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="Run contracts that can be proven against the currently deployed baseline.",
    )
    parser.add_argument("--release-attempts", type=int, default=24)
    parser.add_argument("--contract-attempts", type=int, default=6)
    parser.add_argument("--retry-delay", type=float, default=5)
    parser.add_argument("--settle-attempts", type=int, default=61)
    parser.add_argument("--settle-delay", type=float, default=10)
    args = parser.parse_args(argv)
    if (
        args.release_attempts < 1
        or args.contract_attempts < 1
        or args.settle_attempts < 1
    ):
        parser.error("attempt counts must be at least 1")
    if args.retry_delay < 0 or args.settle_delay < 0:
        parser.error("retry delays must be non-negative")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run_gate(
            base_url=args.base_url,
            expected_sha=args.expected_sha,
            release_attempts=args.release_attempts,
            contract_attempts=args.contract_attempts,
            retry_delay=args.retry_delay,
            baseline_only=args.baseline,
            settle_attempts=args.settle_attempts,
            settle_delay=args.settle_delay,
        )
    except ContractError as exc:
        print(f"Production contract failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
