"""Read-only live validation of the NEW required-side-aware snapshot
warm-up (IBKR execution-observability hardening, 2026-08-26, Section 19)
-- exercises the real, deployed get_quotes_for_selected_legs() and
get_quotes_for_known_contracts() with real ASK/BID requirements, the
exact way official entry/settlement capture now calls them, and reports
per-attempt telemetry so the readiness behavior can be observed directly
against the real, open market.

Never creates a DecisionSnapshot, EntryCaptureAttempt, EntrySnapshot,
SettlementCaptureAttempt, or QuoteAcquisitionAttempt row -- pure
read-only diagnostic, same guarantee as every other script in this
directory. Does NOT tune warm-up parameters -- only observes.

Usage: IBKR_BASE_URL=https://localhost:5002/v1/api PYTHONPATH=src \
    python scripts/ibkr_required_side_live_check.py [TICKER ...]
"""

import json
import os
import sys
import time
from datetime import UTC, datetime

from providers.ibkr_options import IBKROptionsProvider, _month_code
from providers.types import KnownContract, SelectedLeg, SnapshotAttempt

DEFAULT_TICKERS = ["AAPL", "NVDA"]


def _check_leg(
    provider: IBKROptionsProvider,
    ticker: str,
    strike,
    option_type: str,
    action: str,
    required_label: str,
    expiration,
) -> dict:
    attempts: list[dict] = []

    def on_attempt(a: SnapshotAttempt) -> None:
        for conid, presence in a.per_conid.items():
            attempts.append(
                {
                    "conid": conid,
                    "attempt": a.attempt,
                    "elapsed_ms": round(a.elapsed_ms, 1),
                    "bid_present": presence.bid_present,
                    "ask_present": presence.ask_present,
                    "last_present": presence.last_present,
                    "quality": presence.market_data_quality,
                }
            )

    t0 = time.monotonic()
    quotes = provider.get_quotes_for_selected_legs(
        ticker,
        [SelectedLeg(strike=strike, option_type=option_type, action=action)],
        expiration=expiration,
        as_of=datetime.now(UTC),
        on_attempt=on_attempt,
    )
    total_ms = round((time.monotonic() - t0) * 1000, 1)

    required_field = "ask_present" if required_label == "ASK" else "bid_present"
    time_to_required = next((a["elapsed_ms"] for a in attempts if a[required_field]), None)
    time_to_last = next((a["elapsed_ms"] for a in attempts if a["last_present"]), None)
    last_before_required = (
        time_to_last is not None
        and time_to_required is not None
        and time_to_last < time_to_required
    )

    return {
        "ticker": ticker,
        "strike": str(strike),
        "option_type": option_type,
        "action": action,
        "required_side": required_label,
        "total_ms": total_ms,
        "attempts": attempts,
        "time_to_required_side_ms": time_to_required,
        "time_to_last_ms": time_to_last,
        "last_appeared_before_required_side": last_before_required,
        "quotes_returned": len(quotes),
        "final_bid": str(quotes[0].bid) if quotes and quotes[0].bid is not None else None,
        "final_ask": str(quotes[0].ask) if quotes and quotes[0].ask is not None else None,
        "final_quality": quotes[0].market_data_quality if quotes else None,
    }


def diagnose_ticker(provider: IBKROptionsProvider, ticker: str) -> dict:
    conid, months = provider._resolve_underlying(ticker)  # noqa: SLF001
    price, _bid, _ask, _quality = provider._underlying_quote_with_bid_ask(conid)  # noqa: SLF001
    if price is None:
        return {"ticker": ticker, "error": "no underlying price available"}

    ref = datetime.now(UTC).date()
    target_expiration, _month, strikes = provider._resolve_target_expiration(  # noqa: SLF001
        conid, months, price, ref, earnings_anchored=False
    )
    if target_expiration is None or not strikes:
        return {"ticker": ticker, "error": "no expiration/strikes resolved"}

    atm = min(strikes, key=lambda s: abs(s - price))

    long_result = _check_leg(provider, ticker, atm, "call", "buy", "ASK", target_expiration)
    short_result = _check_leg(provider, ticker, atm, "call", "sell", "BID", target_expiration)

    # Settlement-side check too: treat the same ATM call as an already-
    # entered long leg being closed (requires BID) via
    # get_quotes_for_known_contracts, exactly how real settlement re-
    # quotes a known conid -- exit_requirement_for_action("buy") = BID.
    exit_result = None
    if long_result.get("quotes_returned"):
        # Resolve the real conid for this exact contract once, the same
        # way get_quotes_for_selected_legs already did internally, so
        # this check re-quotes it as "already known" -- no rediscovery.
        month = _month_code(target_expiration)
        option_conid = provider._resolve_exact_contract(  # noqa: SLF001
            conid, month, target_expiration, atm, "C"
        )
        if option_conid is not None:
            attempts: list[dict] = []

            def on_attempt(a: SnapshotAttempt, _attempts=attempts) -> None:
                for c, presence in a.per_conid.items():
                    _attempts.append(
                        {
                            "conid": c,
                            "attempt": a.attempt,
                            "elapsed_ms": round(a.elapsed_ms, 1),
                            "bid_present": presence.bid_present,
                            "ask_present": presence.ask_present,
                        }
                    )

            t0 = time.monotonic()
            exit_quotes = provider.get_quotes_for_known_contracts(
                ticker,
                [
                    KnownContract(
                        strike=atm,
                        option_type="call",
                        external_contract_id=str(option_conid),
                        action="buy",
                    )
                ],
                target_expiration,
                datetime.now(UTC),
                on_attempt=on_attempt,
            )
            exit_result = {
                "required_side": "BID (closing a long leg)",
                "total_ms": round((time.monotonic() - t0) * 1000, 1),
                "attempts": attempts,
                "quotes_returned": len(exit_quotes),
                "final_bid": str(exit_quotes[0].bid)
                if exit_quotes and exit_quotes[0].bid is not None
                else None,
            }

    return {
        "ticker": ticker,
        "expiration": target_expiration.isoformat(),
        "long_entry_requires_ask": long_result,
        "short_entry_requires_bid": short_result,
        "settlement_exit_requires_bid": exit_result,
    }


def main() -> None:
    base_url = os.environ.get("IBKR_BASE_URL", "https://localhost:5002/v1/api")
    tickers = sys.argv[1:] or DEFAULT_TICKERS
    provider = IBKROptionsProvider(base_url=base_url)
    provider._client.ensure_authenticated()  # noqa: SLF001

    for ticker in tickers:
        print(f"--- {ticker} ---", file=sys.stderr)
        result = diagnose_ticker(provider, ticker)
        print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
