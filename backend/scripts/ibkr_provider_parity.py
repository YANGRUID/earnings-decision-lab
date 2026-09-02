"""Read-only diagnostic: compares real Web (Client Portal Gateway) vs. real
TWS (IB Gateway/TWS socket API) output for the same tickers/legs -- IBKR
TWS Migration Phase 1, Section 47.

REQUIRES both providers to actually be reachable and authenticated to
produce a real comparison (Section 48) -- this script never fakes a
comparison when one side is unavailable; it reports that side's real
unavailability instead and skips only the affected rows. No write of any
kind: no DB session, no order, nothing persisted -- prints to stdout only.

Usage:
    python scripts/ibkr_provider_parity.py [--tickers AAPL,NVDA,CRM]

Reads connection config from the same real Settings this project always
uses (IBKR_BASE_URL, IBKR_TWS_HOST/PORT/CLIENT_ID) -- never hardcodes a
different endpoint than the one the real application would use.
"""

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.config import get_settings  # noqa: E402
from providers.ibkr_client import IBKRError  # noqa: E402
from providers.ibkr_options import IBKROptionsProvider  # noqa: E402
from providers.ibkr_tws_options import IBKRTWSProvider  # noqa: E402
from providers.types import OptionQuote  # noqa: E402

_DEFAULT_TICKERS = ["AAPL", "NVDA", "CRM"]


@dataclass
class FieldComparison:
    field: str
    web_value: object
    tws_value: object
    absolute_diff: Decimal | None
    relative_diff_pct: Decimal | None


def _diff(web: Decimal | None, tws: Decimal | None) -> tuple[Decimal | None, Decimal | None]:
    if web is None or tws is None:
        return None, None
    absolute = abs(web - tws)
    relative = (absolute / web * 100) if web != 0 else None
    return absolute, relative


def _compare_quote(web: OptionQuote, tws: OptionQuote) -> list[FieldComparison]:
    fields = ["bid", "ask", "last_price", "implied_volatility", "delta", "gamma", "theta", "vega"]
    comparisons = []
    for field in fields:
        web_value = getattr(web, field)
        tws_value = getattr(tws, field)
        absolute, relative = _diff(web_value, tws_value)
        comparisons.append(FieldComparison(field, web_value, tws_value, absolute, relative))
    return comparisons


def _print_comparison(ticker: str, web: OptionQuote, tws: OptionQuote) -> None:
    print(
        f"    {web.strike} {web.option_type} exp={web.expiration_date} "
        f"| web ts={web.retrieved_at.isoformat()} quality={web.market_data_quality} "
        f"conid={web.external_contract_id} "
        f"| tws ts={tws.retrieved_at.isoformat()} quality={tws.market_data_quality} "
        f"conid={tws.external_contract_id}"
    )
    skew = abs((web.retrieved_at - tws.retrieved_at).total_seconds())
    print(f"      timestamp skew: {skew:.1f}s")
    for c in _compare_quote(web, tws):
        if c.web_value is None and c.tws_value is None:
            continue
        rel = f"{c.relative_diff_pct:.2f}%" if c.relative_diff_pct is not None else "n/a"
        print(f"      {c.field:24s} web={c.web_value!s:>12} tws={c.tws_value!s:>12} rel_diff={rel}")


def run(tickers: list[str]) -> None:
    settings = get_settings()
    now = datetime.now(UTC)

    web: IBKROptionsProvider | None = None
    tws: IBKRTWSProvider | None = None
    try:
        web = IBKROptionsProvider(base_url=settings.ibkr_base_url)
        web._client.ensure_authenticated()  # fail fast, honestly, before any real work
        print(f"Web provider: reachable and authenticated ({settings.ibkr_base_url})")
    except IBKRError as exc:
        print(f"Web provider: UNAVAILABLE -- {exc}")
        web = None

    try:
        tws = IBKRTWSProvider(
            host=settings.ibkr_tws_host,
            port=settings.ibkr_tws_port,
            client_id=settings.ibkr_tws_client_id,
        )
        tws._connection.connect_and_start()
        print(
            f"TWS provider: reachable and ready ({settings.ibkr_tws_host}:{settings.ibkr_tws_port})"
        )
    except IBKRError as exc:
        print(f"TWS provider: UNAVAILABLE -- {exc}")
        tws = None

    if web is None or tws is None:
        print("\nCannot run a real comparison with one side unavailable -- Section 48. Exiting.")
        return

    for ticker in tickers:
        print(f"\n=== {ticker} ===")
        try:
            underlying_web = web.get_underlying_quote(ticker)
        except IBKRError as exc:
            underlying_web = None
            print(f"  web underlying quote failed: {exc}")
        try:
            underlying_tws = tws.get_underlying_quote(ticker)
        except IBKRError as exc:
            underlying_tws = None
            print(f"  tws underlying quote failed: {exc}")

        if underlying_web and underlying_tws:
            absolute, relative = _diff(underlying_web.price, underlying_tws.price)
            print(
                f"  underlying: web={underlying_web.price} tws={underlying_tws.price} "
                f"abs_diff={absolute} rel_diff={relative}"
            )
        else:
            print(f"  underlying: web={underlying_web} tws={underlying_tws}")

        try:
            web_chain = web.get_option_chain(ticker, now, reference_date=now.date())
        except IBKRError as exc:
            web_chain = []
            print(f"  web chain failed: {exc}")
        try:
            tws_chain = tws.get_option_chain(ticker, now, reference_date=now.date())
        except IBKRError as exc:
            tws_chain = []
            print(f"  tws chain failed: {exc}")

        web_by_key = {(q.strike, q.option_type, q.expiration_date): q for q in web_chain}
        tws_by_key = {(q.strike, q.option_type, q.expiration_date): q for q in tws_chain}
        common_keys = sorted(set(web_by_key) & set(tws_by_key))
        print(f"  chain legs: web={len(web_chain)} tws={len(tws_chain)} common={len(common_keys)}")
        for key in common_keys[:10]:
            _print_comparison(ticker, web_by_key[key], tws_by_key[key])

    if tws is not None:
        tws._connection.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", default=",".join(_DEFAULT_TICKERS))
    args = parser.parse_args()
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    run(tickers)


if __name__ == "__main__":
    main()
