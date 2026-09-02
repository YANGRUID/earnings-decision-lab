"""One-off, read-only live proof for Section 7/27 of the live market-data
validation task: exercises the NEW production method
(IBKROptionsProvider.get_quotes_for_selected_legs, now what
services/benchmark_entry_capture.py calls) exactly the way entry capture
actually calls it -- one real batched request for every leg of a real
multi-leg strategy at once, built from real, already-resolved strikes
(never invented) -- and reports the real request count and warm-up
telemetry for that single batched call, so it can be compared against the
diagnostic script's per-contract (unbatched) numbers.

Never creates a DecisionSnapshot or any capture/settlement row. Read-only.

Usage: IBKR_BASE_URL=https://localhost:5002/v1/api PYTHONPATH=src \
    python scripts/ibkr_selected_legs_live_check.py TICKER
"""

import json
import os
import sys
import time
from datetime import UTC, datetime

from providers.ibkr_client import IBKRClient
from providers.ibkr_options import IBKROptionsProvider, SnapshotAttempt, _month_code
from providers.types import SelectedLeg


class _CallCounter:
    """Same real-request counter as ibkr_market_data_diagnostic.py's own
    (duplicated rather than imported cross-script to avoid depending on
    ``scripts`` being an importable package) -- wraps one IBKRClient
    instance's real .get() to count real requests by endpoint path."""

    def __init__(self, client: IBKRClient) -> None:
        self.by_path: dict[str, int] = {}
        self._original_get = client.get
        client.get = self._counting_get  # type: ignore[method-assign]

    def _counting_get(self, path: str, params: dict | None = None):
        self.by_path[path] = self.by_path.get(path, 0) + 1
        return self._original_get(path, params=params)


def main() -> None:
    base_url = os.environ.get("IBKR_BASE_URL", "https://localhost:5002/v1/api")
    ticker = sys.argv[1] if len(sys.argv) > 1 else "AAPL"

    provider = IBKROptionsProvider(base_url=base_url)
    counter = _CallCounter(provider._client)

    conid, months = provider._resolve_underlying(ticker)
    price, _bid, _ask, _quality = provider._underlying_quote_with_bid_ask(conid)
    assert price is not None, f"no underlying price for {ticker}"

    ref = datetime.now(UTC).date()
    target_expiration, _month, strikes = provider._resolve_target_expiration(
        conid, months, price, ref, earnings_anchored=False
    )
    assert target_expiration is not None and strikes, f"no expiration/strikes for {ticker}"

    atm = min(strikes, key=lambda s: abs(s - price))
    idx = strikes.index(atm)
    short_idx = min(idx + 1, len(strikes) - 1)  # a real 1-wide bull call spread

    legs = [
        SelectedLeg(strike=atm, option_type="call"),
        SelectedLeg(strike=strikes[short_idx], option_type="call"),
    ]

    attempts: list[dict] = []

    def on_attempt(a: SnapshotAttempt) -> None:
        attempts.append(
            {
                "attempt": a.attempt,
                "elapsed_ms": round(a.elapsed_ms, 1),
                "per_conid": {
                    str(c): {
                        "bid_present": p.bid_present,
                        "ask_present": p.ask_present,
                        "last_present": p.last_present,
                        "quality": p.market_data_quality,
                    }
                    for c, p in a.per_conid.items()
                },
            }
        )

    # Same private hook the diagnostic script uses -- get_quotes_for_
    # selected_legs itself doesn't expose on_attempt publicly (it's a
    # production method, not a diagnostic one), so this reaches into
    # _resolve_exact_contract + _fetch_snapshots directly to attach
    # telemetry to the exact same call sequence that method makes.
    right_by_type = {"call": "C", "put": "P"}
    month = _month_code(target_expiration)
    contracts = []
    for leg in legs:
        right = right_by_type[leg.option_type]
        oc = provider._resolve_exact_contract(conid, month, target_expiration, leg.strike, right)
        assert oc is not None, f"could not resolve {leg}"
        contracts.append((leg.strike, right, oc))

    t0 = time.monotonic()
    quotes = provider._fetch_snapshots(
        ticker, contracts, target_expiration, datetime.now(UTC), on_attempt=on_attempt
    )
    batch_ms = round((time.monotonic() - t0) * 1000, 1)

    result = {
        "ticker": ticker,
        "expiration": target_expiration.isoformat(),
        "legs": [{"strike": str(s), "right": r, "conid": c} for s, r, c in contracts],
        "batch_fetch_ms": batch_ms,
        "attempts": attempts,
        "quotes": [
            {
                "strike": str(q.strike),
                "option_type": q.option_type,
                "bid": str(q.bid) if q.bid is not None else None,
                "ask": str(q.ask) if q.ask is not None else None,
                "quality": q.market_data_quality,
            }
            for q in quotes
        ],
        "request_counts_for_this_script_run": counter.by_path,
    }
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
