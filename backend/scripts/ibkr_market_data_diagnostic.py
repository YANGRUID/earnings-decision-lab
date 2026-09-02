"""Read-only live IBKR market-data diagnostic (live market-data validation,
2026-08-26). Exists to answer this task's evidence sections (option-chain
discovery correctness, snapshot warm-up timing, request budget, the Aug 25
root-cause question) from real data gathered while the US options market
is open, instead of guessing.

Never creates a DecisionSnapshot, EntryCaptureAttempt, EntrySnapshot,
SettlementCaptureAttempt, or ExitSnapshot, and never places, modifies, or
cancels any order -- it only calls the exact same real, read-only IBKR
Client Portal Gateway endpoints providers/ibkr_options.py already calls
for real research/entry/settlement work (GET-only: secdef/search,
secdef/strikes, secdef/info, marketdata/snapshot), with per-attempt
telemetry attached via the on_attempt hook _snapshot_with_warmup already
supports, and a call-count wrapper around IBKRClient.get so real request
costs (Section 6/17) can be reported precisely rather than estimated.

Reaches into a few of IBKROptionsProvider's own "private" (underscore)
methods deliberately -- the smallest possible interface footprint for a
one-off diagnostic script, matching this task's own Section 2 instruction
("use the smallest provider-interface change necessary") rather than
growing the provider's public surface for a need only this script has.

Usage (from backend/, with .venv activated; talks to the Gateway on its
host-published port, so no container rebuild/restart is needed):

    IBKR_BASE_URL=https://localhost:5002/v1/api PYTHONPATH=src \\
        python scripts/ibkr_market_data_diagnostic.py [TICKER ...]

Defaults to a representative set (Section 3):
    AAPL  extremely liquid large-cap
    NVDA  liquid, already-covered earnings company in this project
    WSM   moderately-liquid earnings candidate already in this project
    ZM    a ticker whose Aug 25 official run showed quote-population failures
"""

import json
import os
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal

from providers.ibkr_client import (
    IBKRClient,
    IBKRError,
    IBKRNotAuthenticatedError,
    IBKRRateLimitedError,
)
from providers.ibkr_options import IBKROptionsProvider, SnapshotAttempt
from providers.types import KnownContract

DEFAULT_TICKERS = ["AAPL", "NVDA", "WSM", "ZM"]


class _CallCounter:
    """Wraps one IBKRClient instance's real .get() to count real requests
    by endpoint path, and real rate-limit/permission-error occurrences --
    read-only observation, never changes what's requested or how."""

    def __init__(self, client: IBKRClient) -> None:
        self.by_path: dict[str, int] = {}
        self.rate_limited_count = 0
        self.permission_error_count = 0
        self._original_get = client.get
        client.get = self._counting_get  # type: ignore[method-assign]

    def _counting_get(self, path: str, params: dict | None = None):
        self.by_path[path] = self.by_path.get(path, 0) + 1
        try:
            return self._original_get(path, params=params)
        except IBKRRateLimitedError:
            self.rate_limited_count += 1
            raise
        except IBKRNotAuthenticatedError:
            self.permission_error_count += 1
            raise

    @property
    def total(self) -> int:
        return sum(self.by_path.values())


def diagnose_ticker(provider: IBKROptionsProvider, ticker: str) -> dict:
    out: dict = {"ticker": ticker, "started_at": datetime.now(UTC).isoformat()}

    try:
        provider._client.ensure_authenticated()
    except IBKRError as exc:
        out["error"] = f"auth check failed: {exc}"
        return out

    t0 = time.monotonic()
    try:
        conid, months = provider._resolve_underlying(ticker)
    except IBKRError as exc:
        out["error"] = f"underlying resolution failed: {exc}"
        return out
    out["underlying_conid"] = conid
    out["available_months"] = months
    out["secdef_search_ms"] = round((time.monotonic() - t0) * 1000, 1)

    t0 = time.monotonic()
    price, bid, ask, quality = provider._underlying_quote_with_bid_ask(conid)
    out["underlying_quote_ms"] = round((time.monotonic() - t0) * 1000, 1)
    out["underlying_price"] = str(price) if price is not None else None
    out["underlying_bid"] = str(bid) if bid is not None else None
    out["underlying_ask"] = str(ask) if ask is not None else None
    out["underlying_quality"] = quality
    if price is None:
        out["error"] = "no underlying price available"
        return out

    # General/current mode (earnings_anchored=False) -- this is a
    # read-only probe, not tied to any real earnings date; the nearest
    # expiration on or after today is a real, usable data point either way.
    ref = datetime.now(UTC).date()
    t0 = time.monotonic()
    target_expiration, month, strikes = provider._resolve_target_expiration(
        conid, months, price, ref, earnings_anchored=False
    )
    out["expiration_resolution_ms"] = round((time.monotonic() - t0) * 1000, 1)
    if target_expiration is None or month is None or not strikes:
        out["error"] = "no expiration/strikes resolved"
        return out
    out["selected_expiration"] = target_expiration.isoformat()
    out["strikes_in_atm_window"] = [str(s) for s in strikes]

    atm_strike = min(strikes, key=lambda s: abs(s - price))
    atm_idx = strikes.index(atm_strike)
    otm_idx = min(atm_idx + 2, len(strikes) - 1)  # "a modestly OTM contract"

    to_test: list[tuple[str, Decimal, str]] = [
        ("near_atm_call", atm_strike, "C"),
        ("near_atm_put", atm_strike, "P"),
    ]
    if otm_idx != atm_idx:
        to_test.append(("modest_otm_call", strikes[otm_idx], "C"))

    out["contracts"] = []
    known_for_requote: list[tuple[Decimal, str, int]] = []
    for label, strike, right in to_test:
        contract_out: dict = {"label": label, "strike": str(strike), "right": right}
        t0 = time.monotonic()
        try:
            option_conid = provider._resolve_exact_contract(
                conid, month, target_expiration, strike, right
            )
        except IBKRError as exc:
            contract_out["resolve_error"] = str(exc)
            out["contracts"].append(contract_out)
            continue
        contract_out["resolve_ms"] = round((time.monotonic() - t0) * 1000, 1)
        contract_out["resolved"] = option_conid is not None
        if option_conid is None:
            out["contracts"].append(contract_out)
            continue
        contract_out["option_conid"] = option_conid

        attempts: list[dict] = []

        def on_attempt(a: SnapshotAttempt, _attempts=attempts, _oc=option_conid) -> None:
            presence = a.per_conid.get(_oc)
            _attempts.append(
                {
                    "attempt": a.attempt,
                    "elapsed_ms": round(a.elapsed_ms, 1),
                    "bid_present": presence.bid_present if presence else None,
                    "ask_present": presence.ask_present if presence else None,
                    "last_present": presence.last_present if presence else None,
                    "market_data_quality": presence.market_data_quality if presence else None,
                }
            )

        try:
            quotes = provider._fetch_snapshots(
                ticker,
                [(strike, right, option_conid)],
                target_expiration,
                datetime.now(UTC),
                on_attempt=on_attempt,
            )
        except IBKRError as exc:
            contract_out["snapshot_error"] = str(exc)
            out["contracts"].append(contract_out)
            continue

        contract_out["attempts"] = attempts
        contract_out["time_to_first_bid_ms"] = next(
            (a["elapsed_ms"] for a in attempts if a["bid_present"]), None
        )
        contract_out["time_to_first_ask_ms"] = next(
            (a["elapsed_ms"] for a in attempts if a["ask_present"]), None
        )
        contract_out["time_to_first_bid_and_ask_ms"] = next(
            (a["elapsed_ms"] for a in attempts if a["bid_present"] and a["ask_present"]), None
        )
        contract_out["num_attempts"] = len(attempts)

        if quotes:
            q = quotes[0]
            contract_out["final_bid"] = str(q.bid) if q.bid is not None else None
            contract_out["final_ask"] = str(q.ask) if q.ask is not None else None
            contract_out["final_last"] = str(q.last_price) if q.last_price is not None else None
            contract_out["final_quality"] = q.market_data_quality
            contract_out["volume"] = q.volume
            contract_out["open_interest"] = q.open_interest
            contract_out["implied_volatility"] = (
                str(q.implied_volatility) if q.implied_volatility is not None else None
            )
            known_for_requote.append((strike, right, option_conid))
        else:
            contract_out["final_quote"] = None

        out["contracts"].append(contract_out)

    # Section 23: exit/settlement-style re-quote of already-known
    # contracts (KnownContract, the same shape EntrySnapshot's own
    # external_contract_id feeds get_quotes_for_known_contracts at real
    # settlement) -- proves the exit quote path works read-only, no full
    # chain rediscovery, using conids just resolved above.
    if known_for_requote:
        right_to_type = {"C": "call", "P": "put"}
        known_contracts = [
            KnownContract(
                strike=strike, option_type=right_to_type[right], external_contract_id=str(oc)
            )
            for strike, right, oc in known_for_requote
        ]
        t0 = time.monotonic()
        requoted = provider.get_quotes_for_known_contracts(
            ticker, known_contracts, target_expiration, datetime.now(UTC)
        )
        out["exit_requote_ms"] = round((time.monotonic() - t0) * 1000, 1)
        out["exit_requote_count"] = len(requoted)
        out["exit_requote_all_have_bid_or_ask"] = all(
            (q.bid is not None or q.ask is not None) for q in requoted
        )

    out["finished_at"] = datetime.now(UTC).isoformat()
    return out


def aggregate(results: list[dict], counter: _CallCounter, total_duration_s: float) -> dict:
    all_contracts = [c for r in results for c in r.get("contracts", [])]
    resolved = [c for c in all_contracts if c.get("resolved")]
    with_bid = [c for c in resolved if c.get("time_to_first_bid_ms") is not None]
    with_ask = [c for c in resolved if c.get("time_to_first_ask_ms") is not None]
    with_both = [c for c in resolved if c.get("time_to_first_bid_and_ask_ms") is not None]

    warmups = sorted(
        c["time_to_first_bid_and_ask_ms"] for c in with_both if c["time_to_first_bid_and_ask_ms"]
    )

    def percentile(sorted_vals: list[float], p: float) -> float | None:
        if not sorted_vals:
            return None
        idx = min(len(sorted_vals) - 1, int(round(p * (len(sorted_vals) - 1))))
        return sorted_vals[idx]

    return {
        "tickers_tested": len(results),
        "tickers_with_error": sum(1 for r in results if "error" in r),
        "contracts_tested": len(all_contracts),
        "contracts_resolved": len(resolved),
        "bid_available_pct": round(100 * len(with_bid) / len(resolved), 1) if resolved else None,
        "ask_available_pct": round(100 * len(with_ask) / len(resolved), 1) if resolved else None,
        "both_available_pct": round(100 * len(with_both) / len(resolved), 1) if resolved else None,
        "median_warmup_ms": percentile(warmups, 0.5),
        "p90_warmup_ms": percentile(warmups, 0.9),
        "p95_warmup_ms": percentile(warmups, 0.95),
        "total_duration_s": round(total_duration_s, 1),
        "request_counts_by_path": counter.by_path,
        "total_requests": counter.total,
        "rate_limited_count": counter.rate_limited_count,
        "permission_error_count": counter.permission_error_count,
    }


def main() -> None:
    base_url = os.environ.get("IBKR_BASE_URL", "https://localhost:5002/v1/api")
    tickers = sys.argv[1:] or DEFAULT_TICKERS

    provider = IBKROptionsProvider(base_url=base_url)
    counter = _CallCounter(provider._client)

    results = []
    start = time.monotonic()
    for ticker in tickers:
        print(f"--- diagnosing {ticker} ---", file=sys.stderr)
        result = diagnose_ticker(provider, ticker)
        results.append(result)
        print(json.dumps(result, indent=2, default=str))
    duration = time.monotonic() - start

    print("--- aggregate ---", file=sys.stderr)
    print(json.dumps(aggregate(results, counter, duration), indent=2, default=str))


if __name__ == "__main__":
    main()
