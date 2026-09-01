from types import SimpleNamespace

import pandas as pd
import pytest

import calibration_backfill
from scripts.production_contract_gate import (
    ContractError,
    HttpResponse,
    PageContract,
    validate_page_with_retry,
)


def _html_response(marker: str, *, elapsed: float = 0.05) -> HttpResponse:
    html = (
        '<!doctype html><html><head><meta name="viewport" '
        'content="width=device-width">'
        f"<title>{marker}</title></head><body>{marker}"
        + ("x" * 600)
        + "</body></html>"
    )
    return HttpResponse(
        status=200,
        headers={"content-type": "text/html"},
        body=html.encode("utf-8"),
        elapsed_seconds=elapsed,
    )


def test_page_contract_retries_transient_warmup_without_relaxing_budget():
    contract = PageContract("/workspace", "My Hub · MLB Analytics Hub")
    calls = []
    sleeps = []

    def fetcher(base_url, path, timeout):
        calls.append((base_url, path, timeout))
        if len(calls) == 1:
            raise ContractError("/workspace request failed: timed out")
        return _html_response(contract.marker)

    assets = validate_page_with_retry(
        base_url="https://production.example",
        contract=contract,
        fetcher=fetcher,
        attempts=2,
        retry_delay=0.25,
        sleeper=sleeps.append,
    )

    assert assets == set()
    assert len(calls) == 2
    assert sleeps == [0.25]
    assert all(call[1:] == ("/workspace", 5) for call in calls)

    with pytest.raises(ContractError, match="exceeded 2s page budget"):
        validate_page_with_retry(
            base_url="https://production.example",
            contract=contract,
            fetcher=lambda *_args: _html_response(contract.marker, elapsed=2.01),
            attempts=2,
            retry_delay=0,
            sleeper=lambda _delay: None,
        )


def test_calibration_backfill_accepts_physics_aggregate_output(monkeypatch):
    source = pd.DataFrame({"pitch": [1]})
    batters = pd.DataFrame({"batter": [101], "hits": [1]})
    pitchers = pd.DataFrame({"pitcher": [202], "ks": [7]})
    physics = pd.DataFrame({"pitcher": [202], "pitch_type": ["FF"]})

    cache = SimpleNamespace(enable=lambda: None)
    pybaseball = SimpleNamespace(
        cache=cache,
        statcast=lambda **_kwargs: source,
    )
    monkeypatch.setitem(__import__("sys").modules, "pybaseball", pybaseball)
    monkeypatch.setattr(
        calibration_backfill,
        "_season_windows",
        lambda _season: iter([("2026-03-20", "2026-04-04")]),
    )
    monkeypatch.setattr(
        calibration_backfill.rm,
        "_agg_chunk",
        lambda _source, _season: (batters, pitchers, physics),
    )

    actual_batters, actual_pitchers = calibration_backfill.fetch_season_logs(2026)

    pd.testing.assert_frame_equal(actual_batters, batters)
    pd.testing.assert_frame_equal(actual_pitchers, pitchers)
