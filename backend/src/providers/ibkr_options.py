"""Real Interactive Brokers Client Portal Gateway options-chain adapter.

Follows the official IBKR discovery flow (confirmed live against a real
authenticated Gateway during Phase 13 -- see docs/ibkr_integration.md for
the full verification record and endpoint citations):

    /iserver/secdef/search   (ticker -> underlying conid + available months)
    /iserver/secdef/strikes  (conid + month -> valid strikes)
    /iserver/secdef/info     (conid + month + strike + right -> exact
                               expirations + option conids)
    /iserver/marketdata/snapshot  (conids -> bid/ask/last/volume/Greeks/IV
                               + a market-data-availability flag)

Unlike Alpha Vantage's REALTIME_OPTIONS (which returns an entire chain in
one call), the Client Portal Gateway has no such bulk endpoint -- fetching
"everything" would mean walking every listed month and strike, which is
both slow and unnecessary for earnings-move analytics. This adapter
deliberately fetches a bounded window instead: an expiration near a
reference date (``reference_date``; the current moment when absent), and
``STRIKES_AROUND_ATM`` strikes on each side of the current at-the-money
strike, both calls and puts. See docs/ibkr_integration.md for the worked
example this rule was derived from (NVDA, 2026-08-17).

Two documented expiration-selection rules, chosen by ``earnings_anchored``:
when a real earnings date is known, the nearest expiration strictly *after*
it (an expiration on or before wouldn't outlive the event); when none is
known, the nearest expiration on or after *now* -- a general, current
snapshot, not pretending to be anchored to an earnings date that doesn't
exist yet. Collection never silently skips just because no earnings date is
known -- see services/research_orchestration.py.
"""

import re
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from analytics.options.implied_move import select_expiration_after, select_nearest_listed_expiration
from models.enums import QuoteRequirement
from providers.base import OptionsDataProvider
from providers.ibkr_client import IBKRClient, IBKRError, decode_market_data_quality
from providers.ibkr_historical import HistoricalBar
from providers.ibkr_historical import fetch_historical_bars as _fetch_historical_bars_web
from providers.types import (
    KnownContract,
    OptionQuote,
    SelectedLeg,
    SnapshotAttempt,
    SnapshotFieldPresence,
    UnderlyingQuote,
    entry_requirement_for_action,
    exit_requirement_for_action,
)

_MONTH_ABBR = [
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
]  # fmt: skip

# How many strikes above/below the current ATM strike to fetch -- a
# deterministic, documented research window sized for earnings-move
# analytics (an ATM straddle plus a modest surrounding band), never "the
# whole chain". Matches the exact window manually verified live for NVDA.
STRIKES_AROUND_ATM = 5

# How many consecutive listed months to try (starting from the reference
# date's own month) before giving up and returning no contracts -- bounds
# the worst case (e.g. a reference date right at a month's last listed
# expiration) to a small, fixed number of extra calls rather than walking
# every month IBKR lists.
_MAX_MONTHS_TO_TRY = 3

# Wider bound for list_available_expirations (the Expiration Engine's
# discovery entrypoint): needs more months than a single-target search
# since it must keep going until it has enough distinct real candidates
# (or exhausts listed months), not stop at the first hit.
_MAX_MONTHS_TO_TRY_FOR_LIST = 4

# Real, empirically necessary: IBKR's snapshot endpoint needs to actually
# start streaming a conid's market data server-side before a second call
# can return real values -- a "priming" call immediately followed by
# another with no pause reliably comes back with only identity fields
# (conid), no bid/ask/Greeks at all, confirmed live during Phase 13 (a
# real bug this project shipped and caught: the very first NVDA snapshot
# ingested with zero delay had null market data on every one of 22
# contracts). A single fixed 2.0s wait was sufficient in every live test
# performed at that time, for a single small batch.
#
# Post-live correction (2026-08-25): that fixed single-shot delay was
# NOT sufficient during the real Aug 25 official run. Real,
# persisted evidence (EntrySnapshot rows for INTU/HEI/ZM/SMTC/WSM/DCI):
# every failed leg had a real, resolved external_contract_id and a real
# market_data_quality code (DELAYED, sometimes degrading to UNKNOWN for
# companies processed later in the run) but bid/ask/last all empty --
# the exact "identity fields only, no market data yet" signature the
# comment above already describes, just not fully resolved by one fixed
# 2.0s wait under real cumulative session load. DY, the first company
# processed (decision generated 16s into the run), was the only one of
# 8 to get a fully populated snapshot on the first attempt.
#
# This is a real snapshot-warmup/timing issue, not genuine market
# illiquidity: a genuinely illiquid contract still reports a real last
# price or an honest "N" (not subscribed) availability code, not a
# populated-quality-but-empty-price response. Root cause understood but
# not fully isolated between (a) IBKR's own per-conid warmup simply
# taking longer under load and (b) this project's own redundant
# rediscovery -- entry capture calls get_option_chain() again from
# scratch (full ATM-window discovery, ~20+ secdef/info calls) mere
# seconds after decision generation already discovered and quoted the
# exact same contracts, so every company's real snapshot batch is
# larger and arrives later in the run than it needs to.
#
# Fix: replace the single fixed-delay shot with a bounded, VALIDATING
# retry -- poll until every requested conid's own last-price field has
# actually arrived (a strong, real signal the subscription is genuinely
# live), up to a real time bound, rather than trusting one blind sleep.
# This never fabricates a quote or extends the official capture window
# (Section 12's own "no midpoint fallback, no historical fallback"
# still applies untouched) -- it only gives a real, in-flight IBKR
# subscription more real time to actually deliver data before this
# project gives up and reports the honest "no ask/bid quote available"
# it already reported before. The exact attempt count/spacing below is
# a real starting point based on this evidence, not empirically re-
# tuned against a second live run (this correction was made outside
# market hours) -- see this project's own post-live report for that
# explicit caveat.
_SNAPSHOT_WARMUP_MAX_ATTEMPTS = 5
_SNAPSHOT_WARMUP_RETRY_DELAY_SECONDS = 1.5

# Field codes requested from /iserver/marketdata/snapshot, confirmed live
# against a real NVDA option chain during Phase 13 development -- not
# assumed from documentation alone. See docs/ibkr_integration.md.
_FIELD_LAST = "31"
_FIELD_BID = "84"
_FIELD_ASK = "86"
_FIELD_VOLUME = "87"
_FIELD_AVAILABILITY = "6509"
_FIELD_DELTA = "7308"
_FIELD_GAMMA = "7309"
_FIELD_THETA = "7310"
_FIELD_VEGA = "7311"
_FIELD_IV = "7633"
_FIELD_OPEN_INTEREST = "7638"
_SNAPSHOT_FIELDS = ",".join(
    [
        _FIELD_LAST,
        _FIELD_BID,
        _FIELD_ASK,
        _FIELD_VOLUME,
        _FIELD_AVAILABILITY,
        _FIELD_DELTA,
        _FIELD_GAMMA,
        _FIELD_THETA,
        _FIELD_VEGA,
        _FIELD_IV,
        _FIELD_OPEN_INTEREST,
    ]
)


def _quote_requirement_satisfied(row: dict, requirement: QuoteRequirement) -> bool:
    """Whether one real snapshot row already carries what ``requirement``
    needs -- the real readiness test _snapshot_with_warmup polls against,
    per conid. ``ANALYTICAL`` is the pre-existing, unchanged rule (a real
    last price) so a caller that never specifies a requirement (every
    caller before this hardening pass) sees byte-identical behavior."""
    if requirement == QuoteRequirement.ASK:
        return _decimal_or_none(row.get(_FIELD_ASK)) is not None
    if requirement == QuoteRequirement.BID:
        return _decimal_or_none(row.get(_FIELD_BID)) is not None
    if requirement == QuoteRequirement.BID_ASK:
        return (
            _decimal_or_none(row.get(_FIELD_BID)) is not None
            and _decimal_or_none(row.get(_FIELD_ASK)) is not None
        )
    return _decimal_or_none(row.get(_FIELD_LAST)) is not None  # ANALYTICAL


class IBKRContractNotFoundError(IBKRError):
    """No listed underlying with an options section was found for a
    ticker -- a real, actionable problem (typo, delisted symbol, not
    tradable at IBKR), unlike an empty expiration/strike window within an
    otherwise-valid underlying, which is handled by returning no quotes."""


def _month_code(d: date) -> str:
    return f"{_MONTH_ABBR[d.month - 1]}{d.year % 100:02d}"


def _next_month_code(code: str) -> str:
    abbr, year_suffix = code[:3], int(code[3:])
    idx = _MONTH_ABBR.index(abbr) + 1
    year_bump, idx = divmod(idx, 12)
    return f"{_MONTH_ABBR[idx]}{(year_suffix + year_bump) % 100:02d}"


# IBKR's /iserver/marketdata/snapshot prefixes a price field with a
# single status letter in some real, observed states -- confirmed live
# (2026-08-25, market closed): field 31 "Last" returned "C208.48" for a
# real, valid NVDA close price, not "208.48". Every price/Greek field
# this adapter reads (_FIELD_LAST/_FIELD_BID/_FIELD_ASK/_FIELD_DELTA/
# etc.) goes through this same snapshot endpoint and shares the same
# convention, so without stripping it, real, valid quotes were silently
# discarded as unparseable across the board -- misdiagnosed earlier as a
# market-data-entitlement gap when the data was actually present. The
# quality of what's behind the number (live/delayed/frozen) is decoded
# separately and authoritatively from field 6509, never inferred from
# this prefix -- see decode_market_data_quality's own docstring.
_LEADING_STATUS_PREFIX = re.compile(r"^[A-Za-z]+")


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    text = _LEADING_STATUS_PREFIX.sub("", str(value))
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _parse_percent(value: object) -> Decimal | None:
    """IBKR returns implied vol as e.g. "31.9%" -- converted to a plain
    fraction (0.319) to match every other provider's convention for
    OptionQuote.implied_volatility."""
    if value is None or value == "":
        return None
    text = str(value).rstrip("%")
    parsed = _decimal_or_none(text)
    return parsed / 100 if parsed is not None else None


def _parse_volume(row: dict) -> int | None:
    """Prefers the raw numeric companion field IBKR sends alongside volume
    (e.g. "87_raw": 6.55E7) over the human-formatted display string (e.g.
    "87": "65.5M"), falling back to parsing the display string only when
    the raw field is genuinely absent. Checked with ``is not None`` rather
    than truthiness throughout, since a real volume of 0 must stay 0, not
    fall through to a different field.
    """
    raw = row.get(f"{_FIELD_VOLUME}_raw")
    if raw is not None:
        parsed = _decimal_or_none(raw)
        if parsed is not None:
            return int(parsed)
    return _parse_abbreviated_int(row.get(_FIELD_VOLUME))


def _parse_abbreviated_int(value: object) -> int | None:
    """IBKR's open-interest field has no raw numeric companion (unlike
    volume's "87_raw") -- only a human-formatted string like "4.12K" or
    "791". Parses the K/M suffix convention observed live; returns None
    for anything that doesn't match rather than guessing.
    """
    if value is None or value == "":
        return None
    text = str(value).strip().upper()
    multiplier = 1
    if text.endswith("K"):
        multiplier, text = 1_000, text[:-1]
    elif text.endswith("M"):
        multiplier, text = 1_000_000, text[:-1]
    parsed = _decimal_or_none(text)
    return int(parsed * multiplier) if parsed is not None else None


class IBKROptionsProvider(OptionsDataProvider):
    def __init__(self, base_url: str, client: IBKRClient | None = None) -> None:
        self._client = client or IBKRClient(base_url=base_url)

    def get_option_chain(
        self,
        ticker: str,
        as_of: datetime,
        expiration: date | None = None,
        reference_date: date | None = None,
        earnings_anchored: bool = True,
    ) -> list[OptionQuote]:
        self._client.ensure_authenticated()

        conid, available_months = self._resolve_underlying(ticker)
        underlying_price, _underlying_quality = self._underlying_quote(conid)
        if underlying_price is None:
            return []

        ref = reference_date or as_of.date()
        target_expiration: date | None
        month: str | None
        strikes: list[Decimal] | None
        if expiration is not None:
            target_expiration = expiration
            month = _month_code(expiration)
            strikes = self._strikes_near_atm(conid, month, underlying_price)
        else:
            # _resolve_target_expiration already made the real
            # /iserver/secdef/strikes call for the winning month (it needs
            # a probe strike to find the expiration in the first place) --
            # reusing its result here avoids making the identical request
            # a second time (live market-data validation, 2026-08-26).
            target_expiration, month, strikes = self._resolve_target_expiration(
                conid, available_months, underlying_price, ref, earnings_anchored
            )
        if target_expiration is None or month is None or not strikes:
            return []

        contracts = self._resolve_contracts(conid, month, target_expiration, strikes)
        if not contracts:
            return []

        return self._fetch_snapshots(ticker, contracts, target_expiration, as_of)

    def get_quotes_for_known_contracts(
        self,
        ticker: str,
        contracts: list[KnownContract],
        expiration: date,
        as_of: datetime,
        on_attempt: Callable[[SnapshotAttempt], None] | None = None,
    ) -> list[OptionQuote]:
        """Phase 4.5 -- re-quotes contracts already identified by conid
        (``EntrySnapshot.external_contract_id``), skipping strike/ATM
        discovery entirely (see this method's own docstring on
        providers/base.py for why: the underlying may have moved since
        entry, and re-discovering strikes risks silently targeting a
        different contract than the one actually held). Malformed
        contract identity (a non-numeric external_contract_id, which
        should never happen for a contract this same provider captured
        at entry) is skipped rather than raising, matching this
        project's "real connectivity failures raise, malformed/missing
        data is honestly reported as absent" convention.
        """
        self._client.ensure_authenticated()
        right_by_type = {"call": "C", "put": "P"}
        resolved: list[tuple[Decimal, str, int]] = []
        required: dict[int, QuoteRequirement] = {}
        for contract in contracts:
            right = right_by_type.get(contract.option_type)
            if right is None:
                continue
            try:
                conid = int(contract.external_contract_id)
            except ValueError:
                continue
            resolved.append((contract.strike, right, conid))
            # Closing a BUY leg needs BID, closing a SELL leg needs ASK --
            # the inverse of the entry mapping, over the leg's ORIGINAL
            # entry action (execution-observability hardening, 2026-08-26).
            required[conid] = exit_requirement_for_action(contract.action)
        if not resolved:
            return []
        return self._fetch_snapshots(
            ticker, resolved, expiration, as_of, required=required, on_attempt=on_attempt
        )

    def get_quotes_for_selected_legs(
        self,
        ticker: str,
        legs: list[SelectedLeg],
        expiration: date,
        as_of: datetime,
        on_attempt: Callable[[SnapshotAttempt], None] | None = None,
    ) -> list[OptionQuote]:
        """Live market-data validation (2026-08-26), Section 7 -- resolves
        only the exact (strike, right) pairs already selected by strategy
        generation, one targeted ``/iserver/secdef/info`` call per unique
        pair, never the ATM-window ``/iserver/secdef/strikes`` sweep or
        the up-to-``2 * (2*STRIKES_AROUND_ATM+1)`` ``secdef/info`` calls
        get_option_chain()'s own discovery makes to find strikes nobody
        asked for. This is what benchmark entry capture now calls instead
        of get_option_chain() (see services/benchmark_entry_capture.py) --
        the exact fix Section 7 of the live market-data validation task
        asked for: entry capture quoting the exact legs Decision Engine
        already picked, not rediscovering a whole chain around them.
        """
        self._client.ensure_authenticated()
        conid, _available_months = self._resolve_underlying(ticker)
        month = _month_code(expiration)
        right_by_type = {"call": "C", "put": "P"}

        contracts: list[tuple[Decimal, str, int]] = []
        required: dict[int, QuoteRequirement] = {}
        seen: set[tuple[Decimal, str]] = set()
        for leg in legs:
            key = (leg.strike, leg.option_type)
            if key in seen:
                continue
            seen.add(key)
            right = right_by_type.get(leg.option_type)
            if right is None:
                continue
            option_conid = self._resolve_exact_contract(conid, month, expiration, leg.strike, right)
            if option_conid is not None:
                contracts.append((leg.strike, right, option_conid))
                # A BUY leg needs ASK, a SELL leg needs BID (execution-
                # observability hardening, 2026-08-26) -- each leg's own
                # real action, never a guess or a blanket requirement
                # applied to every leg alike.
                required[option_conid] = entry_requirement_for_action(leg.action)

        if not contracts:
            return []
        return self._fetch_snapshots(
            ticker, contracts, expiration, as_of, required=required, on_attempt=on_attempt
        )

    def resolve_expiration_for_reconstruction(
        self, ticker: str, reference_date: date, earnings_date: date | None
    ) -> date | None:
        """Determines which expiration a historical-close reconstruction
        should target, reusing the exact same rule live collection uses
        (select_expiration_after / select_nearest_listed_expiration) --
        never a separate, possibly-disagreeing rule.

        Real, documented limitation: IBKR's Client Portal API has no
        "what was listed as of a past date" endpoint, so this necessarily
        queries *today's currently listed* expirations/strikes, not
        target_session_date's own -- an honest approximation, accurate for
        reconstructing yesterday's (or a few sessions ago) close, since
        listed expirations don't change day to day except by rolling off
        after they themselves expire.
        """
        self._client.ensure_authenticated()
        conid, available_months = self._resolve_underlying(ticker)
        underlying_price, _quality = self._underlying_quote(conid)
        if underlying_price is None:
            return None
        # _resolve_target_expiration's own reference_date argument doubles
        # as "the earnings date" when earnings_anchored=True (it's fed
        # straight into select_expiration_after) -- so the actual earnings
        # date must be passed there, not the reconstruction's
        # target-session date, when one is known.
        select_against = earnings_date if earnings_date is not None else reference_date
        target, _month, _strikes = self._resolve_target_expiration(
            conid,
            available_months,
            underlying_price,
            select_against,
            earnings_anchored=earnings_date is not None,
        )
        return target

    def list_available_expirations(
        self, ticker: str, after: date, max_candidates: int = 5
    ) -> list[date]:
        """Real listed expirations strictly after ``after``, walking up to
        ``_MAX_MONTHS_TO_TRY_FOR_LIST`` consecutive listed months starting
        at ``after``'s own month and taking the union of every expiration
        found via one ATM-strike probe per month (same secdef/info flow as
        _resolve_target_expiration, just not stopping at the first hit) --
        this naturally surfaces both near-term weeklies (dense within a
        single month) and a further-out monthly, without a separate code
        path for either. Bounded and real: never invents a date, and never
        walks every month IBKR lists.
        """
        self._client.ensure_authenticated()
        conid, available_months = self._resolve_underlying(ticker)
        underlying_price, _quality = self._underlying_quote(conid)
        if underlying_price is None:
            return []

        found: set[date] = set()
        candidate_month = _month_code(after)
        for _ in range(_MAX_MONTHS_TO_TRY_FOR_LIST):
            if candidate_month in available_months:
                strikes = self._strikes_near_atm(conid, candidate_month, underlying_price)
                if strikes:
                    probe_strike = min(strikes, key=lambda k: abs(k - underlying_price))
                    found |= self._expirations_for_strike(conid, candidate_month, probe_strike)
            if len({d for d in found if d > after}) >= max_candidates:
                break
            candidate_month = _next_month_code(candidate_month)

        return sorted(d for d in found if d > after)[:max_candidates]

    def discover_contracts_for_expiration(
        self, ticker: str, target_expiration: date
    ) -> tuple[int, Decimal | None, list[tuple[Decimal, str, int]]]:
        """Public entrypoint for callers that already know which expiration
        they want (e.g. services/options_reconstruction.py, rebuilding a
        past session's close where the live-quote-driven expiration
        selection above doesn't apply) -- reuses the exact same
        underlying/strikes/contract-resolution flow as get_option_chain
        rather than duplicating it. Returns (underlying_conid,
        current_underlying_price_or_None, [(strike, right, option_conid)]).
        The underlying price returned here is IBKR's *current* live quote
        (used only to center the strike window) -- never confused with a
        reconstructed historical underlying price, which callers compute
        separately from historical bars.
        """
        self._client.ensure_authenticated()
        conid, _available_months = self._resolve_underlying(ticker)
        underlying_price, _quality = self._underlying_quote(conid)
        if underlying_price is None:
            return conid, None, []
        month = _month_code(target_expiration)
        strikes = self._strikes_near_atm(conid, month, underlying_price)
        if not strikes:
            return conid, underlying_price, []
        contracts = self._resolve_contracts(conid, month, target_expiration, strikes)
        return conid, underlying_price, contracts

    def get_historical_bars(
        self,
        conid: int,
        *,
        bar: str = "1min",
        period: str = "1d",
        end_time: datetime | None = None,
        outside_rth: bool = False,
    ) -> list[HistoricalBar]:
        """Thin delegation to this module's own, pre-existing free
        function -- ``bar``/``period`` are already this ABC method's
        canonical vocabulary (the Web Gateway's own native syntax), so no
        translation is needed here, unlike providers/ibkr_tws_options.py's
        own override."""
        return _fetch_historical_bars_web(
            self._client, conid, bar=bar, period=period, end_time=end_time, outside_rth=outside_rth
        )

    def get_underlying_quote(self, ticker: str) -> UnderlyingQuote | None:
        """Phase 4.4 hardening: a real, live quote for the underlying
        itself -- last, bid, and ask all requested together (unlike the
        price-only ``_underlying_quote`` helper below, used internally
        just to center an ATM strike window), since here the underlying's
        own market context is the actual deliverable. Used exclusively by
        the official benchmark entry capture path (services/benchmark_
        entry_capture.py) -- ordinary V3 research/reconstruction quotes
        never call this.
        """
        self._client.ensure_authenticated()
        conid, _available_months = self._resolve_underlying(ticker)
        price, bid, ask, quality = self._underlying_quote_with_bid_ask(conid)
        if price is None:
            return None
        now = datetime.now(UTC)
        # IBKR TWS Migration, Phase 3 readiness (Section 8) -- "ibkr_web",
        # not the old bare "ibkr", so a persisted row honestly records
        # which real transport served it once TWS is also writing rows
        # (as "ibkr_tws" -- see providers/ibkr_tws_options.py). Existing
        # rows written before this change keep their real, historical
        # "ibkr" value forever -- never rewritten (see services/
        # provider_status.py's own matching update for why a "last
        # successful ibkr use" query must still recognize all three).
        return UnderlyingQuote(
            ticker=ticker.upper(),
            price=price,
            bid=bid,
            ask=ask,
            timestamp=now,
            market_data_quality=quality,
            source_provider="ibkr_web",
            retrieved_at=now,
        )

    def _resolve_underlying(self, ticker: str) -> tuple[int, list[str]]:
        """Deterministic rule, verified live for NVDA during Phase 13: the
        first secdef/search result whose symbol matches exactly and that
        lists both an STK and an OPT section. See docs/ibkr_integration.md.
        """
        results = self._client.get(
            "/iserver/secdef/search", params={"symbol": ticker.upper()}
        ).json()
        for result in results:
            if str(result.get("symbol", "")).upper() != ticker.upper():
                continue
            sections = {s.get("secType"): s for s in result.get("sections", [])}
            if "STK" in sections and "OPT" in sections:
                months = str(sections["OPT"].get("months") or "")
                month_list = [m for m in months.split(";") if m]
                return int(result["conid"]), month_list
        raise IBKRContractNotFoundError(
            f"no listed underlying with an options section found for {ticker!r}"
        )

    def _underlying_quote(self, conid: int) -> tuple[Decimal | None, str]:
        fields = f"{_FIELD_LAST},{_FIELD_AVAILABILITY}"
        # A conid queried for the first time in a session needs real time
        # before a snapshot call returns real values, not just identity
        # fields -- see _snapshot_with_warmup's own docstring for the
        # real Aug 25 evidence behind the bounded, validating retry.
        data = self._snapshot_with_warmup([conid], fields=fields)
        row = next((r for r in data if r.get("conid") == conid), None)
        if row is None or _decimal_or_none(row.get(_FIELD_LAST)) is None:
            return None, "unknown"
        price = _decimal_or_none(row.get(_FIELD_LAST))
        quality = decode_market_data_quality(row.get(_FIELD_AVAILABILITY))
        return price, quality

    def _underlying_quote_with_bid_ask(
        self, conid: int
    ) -> tuple[Decimal | None, Decimal | None, Decimal | None, str]:
        """Same live-snapshot mechanism as ``_underlying_quote`` above,
        extended to also request bid/ask -- kept as a separate helper
        rather than widening ``_underlying_quote`` itself so that
        method's existing callers (strike-window centering only, which
        needs just a price) stay untouched."""
        fields = f"{_FIELD_LAST},{_FIELD_BID},{_FIELD_ASK},{_FIELD_AVAILABILITY}"
        data = self._snapshot_with_warmup([conid], fields=fields)
        row = next((r for r in data if r.get("conid") == conid), None)
        if row is None or _decimal_or_none(row.get(_FIELD_LAST)) is None:
            return None, None, None, "unknown"
        price = _decimal_or_none(row.get(_FIELD_LAST))
        bid = _decimal_or_none(row.get(_FIELD_BID))
        ask = _decimal_or_none(row.get(_FIELD_ASK))
        quality = decode_market_data_quality(row.get(_FIELD_AVAILABILITY))
        return price, bid, ask, quality

    def _resolve_target_expiration(
        self,
        conid: int,
        available_months: list[str],
        underlying_price: Decimal,
        reference_date: date,
        earnings_anchored: bool = True,
    ) -> tuple[date | None, str | None, list[Decimal] | None]:
        """``earnings_anchored=True`` (a real earnings date in
        ``reference_date``) requires the expiration strictly *after* it, so
        the event falls inside the contract's remaining life --
        ``select_expiration_after``. ``earnings_anchored=False`` (no known
        earnings date -- ``reference_date`` is just "now") allows an
        expiration on the reference date itself, since there's no event it
        needs to outlive -- ``select_nearest_listed_expiration``. See
        docs/ibkr_integration.md and analytics/options/implied_move.py.

        Also returns the real ATM-window strikes already fetched for the
        winning month (live market-data validation, 2026-08-26) -- the
        matching month's own ``/iserver/secdef/strikes`` call happens here
        regardless (needed to probe an expiration), so a caller that also
        needs that same month's strikes (get_option_chain's own AUTO-mode
        path) can reuse this result instead of making the identical real
        request a second time.
        """
        select = select_expiration_after if earnings_anchored else select_nearest_listed_expiration
        candidate_month = _month_code(reference_date)
        for _ in range(_MAX_MONTHS_TO_TRY):
            if candidate_month in available_months:
                strikes = self._strikes_near_atm(conid, candidate_month, underlying_price)
                if strikes:
                    probe_strike = min(strikes, key=lambda k: abs(k - underlying_price))
                    expirations = self._expirations_for_strike(conid, candidate_month, probe_strike)
                    target = select(expirations, reference_date)
                    if target is not None:
                        return target, candidate_month, strikes
            candidate_month = _next_month_code(candidate_month)
        return None, None, None

    def _strikes_near_atm(self, conid: int, month: str, underlying_price: Decimal) -> list[Decimal]:
        data = self._client.get(
            "/iserver/secdef/strikes",
            params={"conid": conid, "sectype": "OPT", "month": month, "exchange": "SMART"},
        ).json()
        raw_strikes = {_decimal_or_none(s) for s in data.get("call", []) + data.get("put", [])}
        all_strikes = sorted(s for s in raw_strikes if s is not None)
        if not all_strikes:
            return []
        atm = min(all_strikes, key=lambda k: abs(k - underlying_price))
        atm_index = all_strikes.index(atm)
        low = max(0, atm_index - STRIKES_AROUND_ATM)
        high = min(len(all_strikes), atm_index + STRIKES_AROUND_ATM + 1)
        return all_strikes[low:high]

    def _expirations_for_strike(self, conid: int, month: str, strike: Decimal) -> set[date]:
        data = self._client.get(
            "/iserver/secdef/info",
            params={
                "conid": conid,
                "sectype": "OPT",
                "month": month,
                "strike": str(strike),
                "right": "C",
                "exchange": "SMART",
            },
        ).json()
        expirations = set()
        for entry in data:
            maturity = entry.get("maturityDate")
            if maturity:
                expirations.add(date(int(maturity[:4]), int(maturity[4:6]), int(maturity[6:8])))
        return expirations

    def _resolve_exact_contract(
        self, conid: int, month: str, target_expiration: date, strike: Decimal, right: str
    ) -> int | None:
        """One real ``/iserver/secdef/info`` call for exactly one
        (strike, right) pair -- the same lookup ``_resolve_contracts``
        makes inside its own strike/right loop, factored out so
        ``get_quotes_for_selected_legs`` (live market-data validation,
        2026-08-26) can resolve only the specific contracts it needs
        without walking an ATM window of strikes nobody selected."""
        data = self._client.get(
            "/iserver/secdef/info",
            params={
                "conid": conid,
                "sectype": "OPT",
                "month": month,
                "strike": str(strike),
                "right": right,
                "exchange": "SMART",
            },
        ).json()
        match = next(
            (
                entry
                for entry in data
                if entry.get("maturityDate") == target_expiration.strftime("%Y%m%d")
            ),
            None,
        )
        return int(match["conid"]) if match is not None else None

    def _resolve_contracts(
        self, conid: int, month: str, target_expiration: date, strikes: list[Decimal]
    ) -> list[tuple[Decimal, str, int]]:
        """Returns (strike, right, option_conid) triples for every
        strike/right pair that actually has a contract at
        ``target_expiration`` -- silently skips any that don't (a real,
        occasional gap, not an error) rather than failing the whole batch.
        """
        contracts: list[tuple[Decimal, str, int]] = []
        for strike in strikes:
            for right in ("C", "P"):
                option_conid = self._resolve_exact_contract(
                    conid, month, target_expiration, strike, right
                )
                if option_conid is not None:
                    contracts.append((strike, right, option_conid))
        return contracts

    def _fetch_snapshots(
        self,
        ticker: str,
        contracts: list[tuple[Decimal, str, int]],
        expiration_date: date,
        as_of: datetime,
        required: dict[int, QuoteRequirement] | None = None,
        on_attempt: Callable[[SnapshotAttempt], None] | None = None,
    ) -> list[OptionQuote]:
        conids = [c[2] for c in contracts]
        # Priming call, then a bounded, validating wait for real values
        # to actually arrive -- see _snapshot_with_warmup's own docstring
        # for the real Aug 25 evidence this exists to fix (a fixed,
        # single 2.0s wait was not reliably enough for a real batch this
        # size under real cumulative session load), and for the real
        # required-side readiness rule (execution-observability
        # hardening, 2026-08-26).
        data = self._snapshot_with_warmup(
            conids, fields=_SNAPSHOT_FIELDS, required=required, on_attempt=on_attempt
        )
        by_conid = {row.get("conid"): row for row in data}

        retrieved_at = datetime.now(UTC)
        quotes: list[OptionQuote] = []
        for strike, right, option_conid in contracts:
            row = by_conid.get(option_conid)
            if row is None:
                continue
            quotes.append(
                OptionQuote(
                    ticker=ticker.upper(),
                    snapshot_timestamp=as_of,
                    expiration_date=expiration_date,
                    strike=strike,
                    option_type="call" if right == "C" else "put",
                    bid=_decimal_or_none(row.get(_FIELD_BID)),
                    ask=_decimal_or_none(row.get(_FIELD_ASK)),
                    last_price=_decimal_or_none(row.get(_FIELD_LAST)),
                    volume=_parse_volume(row),
                    open_interest=_parse_abbreviated_int(row.get(_FIELD_OPEN_INTEREST)),
                    implied_volatility=_parse_percent(row.get(_FIELD_IV)),
                    delta=_decimal_or_none(row.get(_FIELD_DELTA)),
                    gamma=_decimal_or_none(row.get(_FIELD_GAMMA)),
                    theta=_decimal_or_none(row.get(_FIELD_THETA)),
                    vega=_decimal_or_none(row.get(_FIELD_VEGA)),
                    market_data_quality=decode_market_data_quality(row.get(_FIELD_AVAILABILITY)),
                    external_contract_id=str(option_conid),
                    source_provider="ibkr_web",
                    retrieved_at=retrieved_at,
                )
            )
        return quotes

    def _snapshot(self, conids: list[int], fields: str) -> list[dict]:
        conid_str = ",".join(str(c) for c in conids)
        return self._client.get(
            "/iserver/marketdata/snapshot", params={"conids": conid_str, "fields": fields}
        ).json()

    def _snapshot_with_warmup(
        self,
        conids: list[int],
        fields: str,
        required: dict[int, QuoteRequirement] | None = None,
        on_attempt: Callable[[SnapshotAttempt], None] | None = None,
    ) -> list[dict]:
        """Primes the subscription, then polls (bounded, validating --
        see _SNAPSHOT_WARMUP_MAX_ATTEMPTS's own docstring for the real
        Aug 25 evidence this replaces a single fixed-delay shot) until
        every requested conid satisfies its own real readiness
        requirement, or the bound is exhausted. Never fabricates a
        value -- returns whatever the most recent real response was; a
        still-missing required field on an otherwise-populated row is
        reported by the caller exactly as honestly as before (real
        market data, just genuinely one-sided or unavailable).

        ``required``, keyed by conid, is IBKR execution-observability
        hardening (2026-08-26): a conid with no entry (or when
        ``required`` itself is ``None``, every real caller before this
        hardening pass) defaults to ``QuoteRequirement.ANALYTICAL`` --
        the exact same last-price-only condition this method always
        used, so behavior is byte-identical unless a caller actually
        opts into a specific executable side. This is the real fix for
        the readiness bug this task's own audit confirmed from source:
        a LONG entry's ASK could still be missing on a poll this method
        would already have accepted, because readiness only ever checked
        LAST -- see QuoteRequirement's own docstring for the full
        ASK/BID/BID_ASK/ANALYTICAL rule.

        ``on_attempt``, when given, is called after every real poll (not
        the priming call) with a real ``SnapshotAttempt`` -- purely an
        observation hook; it never influences which attempt the loop
        stops on or what this method returns."""
        start = time.monotonic()
        self._snapshot(conids, fields=fields)  # priming call, unchanged
        data: list[dict] = []
        for attempt_num in range(1, _SNAPSHOT_WARMUP_MAX_ATTEMPTS + 1):
            time.sleep(_SNAPSHOT_WARMUP_RETRY_DELAY_SECONDS)
            data = self._snapshot(conids, fields=fields)
            by_conid = {row.get("conid"): row for row in data}
            if on_attempt is not None:
                per_conid: dict[int, SnapshotFieldPresence] = {}
                for c in conids:
                    row = by_conid.get(c, {})
                    bid = _decimal_or_none(row.get(_FIELD_BID))
                    ask = _decimal_or_none(row.get(_FIELD_ASK))
                    last = _decimal_or_none(row.get(_FIELD_LAST))
                    per_conid[c] = SnapshotFieldPresence(
                        bid_present=bid is not None,
                        ask_present=ask is not None,
                        last_present=last is not None,
                        market_data_quality=decode_market_data_quality(
                            row.get(_FIELD_AVAILABILITY)
                        ),
                        bid=bid,
                        ask=ask,
                        last_price=last,
                    )
                on_attempt(
                    SnapshotAttempt(
                        attempt=attempt_num,
                        elapsed_ms=(time.monotonic() - start) * 1000,
                        per_conid=per_conid,
                    )
                )
            if len(by_conid) >= len(conids) and all(
                _quote_requirement_satisfied(
                    by_conid.get(c, {}),
                    (required or {}).get(c, QuoteRequirement.ANALYTICAL),
                )
                for c in conids
            ):
                break
        return data
