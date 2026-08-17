"""Real, READ-ONLY Interactive Brokers portfolio adapter. Fetches the
user's own real account and current positions from the local, already-
authenticated Client Portal Gateway -- never places, modifies, previews,
or cancels an order, and never will (see docs/ibkr_integration.md).

Endpoints (confirmed live during Phase 13 -- see docs/ibkr_integration.md):

    /iserver/accounts               (the accounts this session can see)
    /portfolio/{accountId}/positions/{page}  (current positions, paginated
                                     100 per page)

The real account ID is masked (see providers/ibkr_client.mask_account_id)
the moment it's read from the Gateway's response -- never held, logged, or
returned anywhere in its raw form after that point.
"""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from providers.ibkr_client import IBKRClient, mask_account_id
from providers.types import PortfolioPosition

_POSITIONS_PAGE_SIZE = 100  # IBKR's own per-page cap; more pages fetched if needed


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


class IBKRPortfolioProvider:
    def __init__(self, base_url: str, client: IBKRClient | None = None) -> None:
        self._client = client or IBKRClient(base_url=base_url)

    def get_masked_account_id(self) -> str:
        """The first account the Gateway's own session reports -- this
        project supports a single account per user, matching the real
        account setup verified live during Phase 13.
        """
        self._client.ensure_authenticated()
        data = self._client.get("/iserver/accounts").json()
        accounts = data.get("accounts") or []
        if not accounts:
            return ""
        return mask_account_id(str(accounts[0]))

    def get_positions(self) -> list[PortfolioPosition]:
        self._client.ensure_authenticated()
        accounts_data = self._client.get("/iserver/accounts").json()
        accounts = accounts_data.get("accounts") or []
        if not accounts:
            return []
        account_id = str(accounts[0])
        masked_account_id = mask_account_id(account_id)

        retrieved_at = datetime.now(UTC)
        positions: list[PortfolioPosition] = []
        page = 0
        while True:
            rows = self._client.get(f"/portfolio/{account_id}/positions/{page}").json()
            if not rows:
                break
            for row in rows:
                positions.append(
                    PortfolioPosition(
                        account_id_masked=masked_account_id,
                        conid=int(row["conid"]),
                        contract_description=str(row.get("contractDesc") or ""),
                        asset_class=str(row.get("assetClass") or "unknown"),
                        quantity=_decimal_or_none(row.get("position")) or Decimal(0),
                        currency=row.get("currency"),
                        market_price=_decimal_or_none(row.get("mktPrice")),
                        market_value=_decimal_or_none(row.get("mktValue")),
                        average_cost=_decimal_or_none(row.get("avgCost")),
                        unrealized_pnl=_decimal_or_none(row.get("unrealizedPnl")),
                        realized_pnl=_decimal_or_none(row.get("realizedPnl")),
                        option_expiry=row.get("expiry") or None,
                        option_right=row.get("putOrCall") or None,
                        option_strike=_decimal_or_none(row.get("strike")),
                        source_provider="ibkr",
                        retrieved_at=retrieved_at,
                    )
                )
            if len(rows) < _POSITIONS_PAGE_SIZE:
                break
            page += 1
        return positions
