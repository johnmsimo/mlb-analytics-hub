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
            "User-Agent": "mlb-analytics-hub-production-contract/4.68",
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


def _validate_journey(payload: Any) -> None:
    _require(isinstance(payload, dict), "journey payload must be an object")
    stages = [stage.get("key") for stage in payload.get("stages", [])]
    alerts = payload.get("alerts", {})
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


def _validate_games(payload: Any) -> None:
    _require(isinstance(payload, dict), "games payload must be an object")
    _require(payload.get("success") is True, "games payload is not successful")
    games = payload.get("games")
    _require(isinstance(games, list), "games payload must include a games list")
    _require(payload.get("count") == len(games), "games count does not match rows")


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
    for contract in PUBLIC_PAGE_CONTRACTS:
        response = fetcher(base_url, contract.path, 5)
        assets.update(validate_page(contract, response))
        print(
            f"PASS page {contract.path} ({response.elapsed_seconds:.2f}s)",
            flush=True,
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

    baseline_contracts = (
        ("/api/product/journey", "product journey", 5.0, _validate_journey),
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
        "pages": len(PUBLIC_PAGE_CONTRACTS),
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
