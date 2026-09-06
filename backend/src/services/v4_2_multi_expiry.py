"""V4.2 CHALLENGER -- bounded multi-expiry candidate construction.

V4.1 resolves ONE expiration (the nearest listed expiry strictly after the
earnings date) and never asks whether a later one would have been a better
instrument for the same view. Over the first seven natural events that index
picked an expiry that expired ON the T+1 settlement day five times, which is
where the 2026-09-04 empty-book incident came from.

The answer this module implements is the one the audit argued for: do not
legislate a minimum DTE, COMPARE expiries on the same objective. So:

    listed metadata (ONE security-definition call, no quotes)
            |
    bounded expiry ladder (at most 3 rungs)
            |
    per expiry: its OWN ATM window, its OWN implied move,
                its OWN listed strikes, its OWN geometry
            |
    dedupe the exact contracts across every expiry and every candidate
            |
    quote each unique contract ONCE
            |
    value every candidate at the SAME T+1 15:30 objective
            |
    combine survivors, then rank across expiries

Two things this deliberately does NOT do.

It does not clone the nearest expiry's candidate into later expiries. A
17-delta wing 2 days out and 30 days out are different instruments with
different premiums, different implied moves and different geometry widths;
copying one forward would fabricate economics. Every expiry derives its own
``ExpectedMoveContext`` from its own real straddle quotes -- in Python, from
observed option prices, never from a model and never from a language model.

It does not add a minimum-DTE rule. A same-day expiry stays eligible and is
allowed to win if its own T+1 economics and its own observed liquidity say
so. What the ladder adds is the alternatives to compare it against.

Every quote here is a real point-in-time observation. Nothing reads a current
chain and calls it historical evidence: the seven realized events have no
frozen metadata, so they genuinely cannot be replayed this way, and this
module is only ever run prospectively.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from analytics.decision.v4_2_expiry_ladder import (
    EXPIRY_LADDER_VERSION,
    ExpiryVariant,
    build_expiry_ladder,
)
from analytics.decision.v4_chain_coverage import ChainMetadata
from analytics.decision.v4_compatibility import evaluate_semantic_compatibility
from analytics.decision.v4_expected_move import (
    ExpectedMoveContext,
    derive_expected_move_context,
)
from analytics.decision.v4_market_view import derive_v4_market_view
from analytics.decision.v4_strategy_semantics import get_strategy_semantics
from analytics.decision.v4_strike_geometry_variants import generate_all_strategy_variant_sets
from analytics.decision.v4_t1_valuation_context import V4T1LegInput, V4T1ValuationContext
from analytics.options.strategy_candidates import StrategyCategory
from models.enums import DecisionDirection, DecisionVolatilityView
from providers.base import OptionsDataProvider
from providers.types import OptionQuote, SelectedLeg
from services.v4_shadow import ShadowCandidateInput

# Deliberately the control's OWN helpers, not copies. Contract identity,
# call/put normalization and the enum coercion at the DecisionView boundary
# must be byte-identical between control and challenger, or the two would
# eventually disagree about which contract is which -- and the comparison
# would then be measuring the helpers instead of the methodologies.
from services.v4_shadow_assembler import (
    MAX_TOTAL_CANDIDATES,
    _coerce_enum,
    _contract_key,
    _right_word,
)

log = logging.getLogger("services.v4_2_multi_expiry")

MULTI_EXPIRY_VERSION = "v4_2_multi_expiry_v1"

#: Per-expiry cap. The global MAX_TOTAL_CANDIDATES still applies across the
#: whole combined universe: three expiries must not become three times the
#: valuation work, only three times the CHOICE.
MAX_CANDIDATES_PER_EXPIRY = 24

MULTI_EXPIRY_OK = "MULTI_EXPIRY"
MULTI_EXPIRY_SINGLE = "SINGLE_EXPIRY_ONLY"
MULTI_EXPIRY_UNAVAILABLE = "LADDER_UNAVAILABLE"


@dataclass
class ExpiryCandidateSet:
    """One rung of the ladder, with the economics it actually derived."""

    variant: ExpiryVariant
    expected_move: ExpectedMoveContext | None = None
    candidates: list[ShadowCandidateInput] = field(default_factory=list)
    listed_strike_count: int = 0
    chain_quote_count: int = 0
    failure_category: str | None = None
    failure_detail: str | None = None

    @property
    def implied_move_pct(self) -> Decimal | None:
        em = self.expected_move
        return em.implied_move_pct if em is not None else None

    @property
    def implied_move_source(self) -> str:
        em = self.expected_move
        if em is None:
            return "unavailable"
        return "atm_straddle" if em.implied_move_available else "unavailable"


@dataclass
class MultiExpiryRequestBudget:
    """Measured, never estimated."""

    underlying_quotes: int = 0
    metadata_calls: int = 0
    chain_discovery_calls: int = 0
    selected_leg_quote_calls: int = 0
    unique_contracts_quoted: int = 0
    contracts_deduplicated: int = 0

    @property
    def total(self) -> int:
        return (
            self.underlying_quotes
            + self.metadata_calls
            + self.chain_discovery_calls
            + self.selected_leg_quote_calls
        )


@dataclass
class MultiExpiryResult:
    """The combined candidate universe across a bounded set of expiries."""

    status: str = MULTI_EXPIRY_UNAVAILABLE
    ladder: list[ExpiryVariant] = field(default_factory=list)
    per_expiry: list[ExpiryCandidateSet] = field(default_factory=list)
    candidates: list[ShadowCandidateInput] = field(default_factory=list)
    underlying_price: Decimal | None = None
    underlying_quote_at: datetime | None = None
    market_data_quality: str | None = None
    available_expirations: list[date] = field(default_factory=list)
    listed_strikes: list[Decimal] = field(default_factory=list)
    chain_metadata_source: str | None = None
    budget: MultiExpiryRequestBudget = field(default_factory=MultiExpiryRequestBudget)
    latency_ms: Decimal = Decimal(0)
    metadata_latency_ms: Decimal = Decimal(0)
    quote_latency_ms: Decimal = Decimal(0)
    truncation_note: str | None = None
    failure_category: str | None = None
    failure_detail: str | None = None
    #: candidate_id -> the expiry rung it was constructed on.
    ladder_position_by_candidate: dict[str, int] = field(default_factory=dict)
    expiry_context_by_candidate: dict[str, dict] = field(default_factory=dict)

    @property
    def expiries_considered(self) -> int:
        return len([s for s in self.per_expiry if s.candidates])


def build_multi_expiry_universe(
    *,
    provider: OptionsDataProvider,
    ticker: str,
    as_of: datetime,
    direction: str,
    volatility_view: str | None,
    earnings_date: date,
    settlement_date: date,
    historical_next_day_move_pcts: list[Decimal] | None = None,
    max_variants: int = 3,
) -> MultiExpiryResult:
    """Construct candidates across the bounded expiry ladder.

    Returns a result in every case, including failure, so a challenger fault
    is recorded as challenger evidence rather than propagated to the control.
    """
    started = time.monotonic()
    result = MultiExpiryResult()

    # ---- 1. underlying, once ---------------------------------------------
    try:
        underlying = provider.get_underlying_quote(ticker)
        result.budget.underlying_quotes += 1
    except Exception as exc:  # noqa: BLE001 -- provider boundary
        result.failure_category = "MARKET_DATA_UNAVAILABLE"
        result.failure_detail = f"underlying quote failed: {type(exc).__name__}: {exc}"
        return result
    if underlying is None:
        result.failure_category = "MARKET_DATA_UNAVAILABLE"
        result.failure_detail = f"no underlying quote returned for {ticker}"
        return result
    result.underlying_price = underlying.price
    result.underlying_quote_at = underlying.timestamp
    result.market_data_quality = underlying.market_data_quality

    # ---- 2. listed metadata, once ----------------------------------------
    metadata_started = time.monotonic()
    metadata: dict | None = None
    getter = getattr(provider, "get_chain_metadata", None)
    if callable(getter):
        try:
            metadata = getter(ticker)
            result.budget.metadata_calls += 1
        except Exception as exc:  # noqa: BLE001
            result.failure_category = "CHAIN_METADATA_FAILED"
            result.failure_detail = f"chain metadata failed: {type(exc).__name__}: {exc}"
            return result
    if not metadata or not metadata.get("expirations"):
        result.failure_category = "CHAIN_METADATA_FAILED"
        result.failure_detail = (
            "no listed expirations available; a multi-expiry ladder cannot be built "
            "from metadata that does not exist, and no expiration will be invented"
        )
        return result
    result.metadata_latency_ms = Decimal(str((time.monotonic() - metadata_started) * 1000))
    result.available_expirations = list(metadata["expirations"])
    result.listed_strikes = list(metadata.get("strikes") or [])
    result.chain_metadata_source = metadata.get("source_provider")

    # ---- 3. bounded ladder, from listed metadata only --------------------
    ladder = build_expiry_ladder(
        set(result.available_expirations),
        earnings_date=earnings_date,
        settlement_date=settlement_date,
        decision_date=as_of.date(),
        max_variants=max_variants,
    )
    result.ladder = ladder
    if not ladder:
        result.failure_category = "NO_ELIGIBLE_EXPIRATION"
        result.failure_detail = (
            f"no listed expiration falls after the earnings date {earnings_date.isoformat()}"
        )
        return result

    market_view = derive_v4_market_view(
        _coerce_enum(DecisionDirection, direction),
        _coerce_enum(DecisionVolatilityView, volatility_view),
    )

    # ---- 4. per expiry: its OWN chain, implied move and geometry ---------
    flat: list[tuple[str, StrategyCategory, str, tuple, ExpiryVariant, ExpectedMoveContext]] = []
    for variant in ladder:
        cset = ExpiryCandidateSet(variant=variant)
        result.per_expiry.append(cset)
        try:
            chain_quotes = provider.get_option_chain(ticker, as_of, expiration=variant.expiration)
            result.budget.chain_discovery_calls += 1
        except Exception as exc:  # noqa: BLE001 -- one expiry must not fail the others
            cset.failure_category = "MARKET_DATA_UNAVAILABLE"
            cset.failure_detail = f"chain discovery failed: {type(exc).__name__}: {exc}"
            continue
        if not chain_quotes:
            cset.failure_category = "CHAIN_METADATA_FAILED"
            cset.failure_detail = f"no quotes returned for {ticker} {variant.expiration}"
            continue
        cset.chain_quote_count = len(chain_quotes)

        # Section 26: THIS expiry's own ATM straddle, never the nearest
        # expiry's implied move copied forward.
        expected_move = derive_expected_move_context(
            spot=underlying.price,
            observed_at=underlying.timestamp,
            expiration=variant.expiration,
            quotes_for_expiration=list(chain_quotes),
            historical_next_day_move_pcts=historical_next_day_move_pcts,
        )
        cset.expected_move = expected_move

        call_strikes = tuple(sorted({q.strike for q in chain_quotes if q.option_type == "call"}))
        put_strikes = tuple(sorted({q.strike for q in chain_quotes if q.option_type == "put"}))
        cset.listed_strike_count = len(set(call_strikes) | set(put_strikes))
        chain_metadata = ChainMetadata(
            ticker=ticker.upper(),
            expiration=variant.expiration,
            observed_at=underlying.timestamp,
            call_strikes=call_strikes,
            put_strikes=put_strikes,
            source="captured_window",
            captured_window_size=len(call_strikes) or None,
        )
        try:
            variant_sets = generate_all_strategy_variant_sets(
                expected_move, list(chain_quotes), chain_metadata=chain_metadata
            )
        except Exception as exc:  # noqa: BLE001
            cset.failure_category = "NO_VALID_CANDIDATE"
            cset.failure_detail = f"geometry generation failed: {type(exc).__name__}: {exc}"
            continue

        produced = 0
        for strategy, candidate_set in variant_sets.items():
            for geometry in candidate_set.variants:
                selection = geometry.result
                if selection.status != "constructed" or not selection.legs:
                    continue
                legs = tuple(
                    (leg.action, _right_word(leg.right), leg.selected_strike, leg.quantity)
                    for leg in selection.legs
                    if leg.selected_strike is not None
                )
                if len(legs) != len(selection.legs):
                    continue
                if produced >= MAX_CANDIDATES_PER_EXPIRY:
                    break
                # The expiry is part of the identity: the same geometry on two
                # expiries is two different instruments and must never collide.
                candidate_id = f"{strategy}:{geometry.variant_id}@{variant.expiration.isoformat()}"
                flat.append(
                    (candidate_id, strategy, geometry.variant_id, legs, variant, expected_move)
                )
                produced += 1

    if not flat:
        result.failure_category = "NO_VALID_CANDIDATE"
        result.failure_detail = "no expiry on the ladder produced a constructable geometry"
        result.status = MULTI_EXPIRY_UNAVAILABLE
        return result

    if len(flat) > MAX_TOTAL_CANDIDATES:
        result.truncation_note = (
            f"generated {len(flat)} candidates across {len(ladder)} expiries; retained the "
            f"first {MAX_TOTAL_CANDIDATES} (MAX_TOTAL_CANDIDATES cap, shared with the control). "
            "Reason: bounded latency and request budget."
        )
        flat = flat[:MAX_TOTAL_CANDIDATES]

    # ---- 5. dedupe exact contracts ACROSS expiries, quote once each ------
    # A contract is identified by (expiration, strike, right): the same strike
    # on two expiries is two contracts, and the same strike used by six
    # candidates on one expiry is ONE.
    unique: dict[tuple[date, Decimal, str], SelectedLeg] = {}
    total_leg_references = 0
    for _cid, _strategy, _variant_id, legs, variant, _em in flat:
        for action, right, strike, _qty in legs:
            total_leg_references += 1
            key = (variant.expiration, *_contract_key(strike, right))
            if key not in unique:
                unique[key] = SelectedLeg(strike=strike, option_type=right, action=action)

    by_expiration: dict[date, list[SelectedLeg]] = {}
    for (expiration, _strike, _right), leg in unique.items():
        by_expiration.setdefault(expiration, []).append(leg)

    quote_started = time.monotonic()
    by_contract: dict[tuple[date, Decimal, str], OptionQuote] = {}
    for expiration, selected in by_expiration.items():
        try:
            quotes = provider.get_quotes_for_selected_legs(ticker, selected, expiration, as_of)
            result.budget.selected_leg_quote_calls += 1
        except Exception as exc:  # noqa: BLE001 -- isolated per expiration
            log.error("quote acquisition failed for %s %s", ticker, expiration, exc_info=True)
            for cset in result.per_expiry:
                if cset.variant.expiration == expiration and cset.failure_category is None:
                    cset.failure_category = "MARKET_DATA_UNAVAILABLE"
                    cset.failure_detail = (
                        f"selected-leg quote acquisition failed: {type(exc).__name__}: {exc}"
                    )
            continue
        for q in quotes:
            by_contract[(expiration, *_contract_key(q.strike, q.option_type))] = q
    result.budget.unique_contracts_quoted = len(unique)
    result.budget.contracts_deduplicated = max(0, total_leg_references - len(unique))
    result.quote_latency_ms = Decimal(str((time.monotonic() - quote_started) * 1000))

    # ---- 6. build valuation contexts, all on the SAME T+1 objective ------
    semantics_cache: dict[str, Any] = {}
    by_expiry_candidates: dict[date, list[ShadowCandidateInput]] = {}
    for candidate_id, strategy, variant_id, legs, variant, expected_move in flat:
        leg_inputs: list[V4T1LegInput] = []
        retrieved: dict[int, datetime] = {}
        contract_ids: dict[int, str] = {}
        for index, (action, right, strike, qty) in enumerate(legs):
            quote = by_contract.get((variant.expiration, *_contract_key(strike, right)))
            leg_inputs.append(
                V4T1LegInput(
                    leg_index=index,
                    action=action,  # type: ignore[arg-type]
                    right=right,  # type: ignore[arg-type]
                    strike=strike,
                    quantity=qty,
                    multiplier=Decimal("100"),
                    entry_bid=quote.bid if quote else None,
                    entry_ask=quote.ask if quote else None,
                    entry_last=quote.last_price if quote else None,
                    entry_iv=quote.implied_volatility if quote else None,
                    entry_delta=quote.delta if quote else None,
                    entry_gamma=quote.gamma if quote else None,
                    entry_theta=quote.theta if quote else None,
                    entry_vega=quote.vega if quote else None,
                    market_data_quality=quote.market_data_quality if quote else None,
                    external_contract_id=quote.external_contract_id if quote else None,
                    entry_volume=quote.volume if quote else None,
                    entry_open_interest=quote.open_interest if quote else None,
                    entry_bid_size=getattr(quote, "bid_size", None) if quote else None,
                    entry_ask_size=getattr(quote, "ask_size", None) if quote else None,
                )
            )
            if quote is not None:
                retrieved[index] = quote.retrieved_at
                if quote.external_contract_id:
                    contract_ids[index] = quote.external_contract_id

        semantics_key = str(strategy)
        if semantics_key not in semantics_cache:
            try:
                semantics_cache[semantics_key] = get_strategy_semantics(strategy)
            except Exception:  # noqa: BLE001 -- unknown strategy, honestly no semantics
                semantics_cache[semantics_key] = None
        semantics = semantics_cache[semantics_key]
        compatibility = (
            evaluate_semantic_compatibility(market_view, semantics)
            if semantics is not None
            else None
        )
        context = V4T1ValuationContext(
            ticker=ticker.upper(),
            underlying_price=underlying.price,
            observed_at=underlying.timestamp,
            entry_timestamp=as_of,
            # Section 28: every candidate on every expiry is valued at the
            # SAME T+1 objective. Nothing is ranked on its expiration payoff.
            expected_exit_timestamp=as_of,
            strategy=strategy,  # type: ignore[arg-type]
            expiration=variant.expiration,
            legs=tuple(leg_inputs),
            expected_move_context=expected_move,
        )
        candidate = ShadowCandidateInput(
            candidate_id=candidate_id,
            context=context,
            semantic_compatibility=compatibility,
            geometry_variant_id=variant_id,
            external_contract_ids=contract_ids,
            leg_retrieved_at=retrieved,
        )
        result.candidates.append(candidate)
        by_expiry_candidates.setdefault(variant.expiration, []).append(candidate)
        result.ladder_position_by_candidate[candidate_id] = variant.ladder_position
        result.expiry_context_by_candidate[candidate_id] = {
            "expiration": variant.expiration.isoformat(),
            "ladder_position": variant.ladder_position,
            "entry_dte": variant.entry_dte,
            "dte_at_settlement": variant.dte_at_settlement,
            "settlement_risk": variant.settlement_risk,
            "implied_move_pct": (
                None
                if expected_move.implied_move_pct is None
                else str(expected_move.implied_move_pct)
            ),
            "implied_move_source": (
                "atm_straddle" if expected_move.implied_move_available else "unavailable"
            ),
            "expiry_ladder_version": EXPIRY_LADDER_VERSION,
            "multi_expiry_version": MULTI_EXPIRY_VERSION,
        }

    for cset in result.per_expiry:
        cset.candidates = by_expiry_candidates.get(cset.variant.expiration, [])

    populated = result.expiries_considered
    result.status = (
        MULTI_EXPIRY_OK
        if populated > 1
        else (MULTI_EXPIRY_SINGLE if populated == 1 else MULTI_EXPIRY_UNAVAILABLE)
    )
    result.latency_ms = Decimal(str((time.monotonic() - started) * 1000))
    return result


def summarize_multi_expiry(result: MultiExpiryResult) -> dict:
    """A diagnostic view of what each rung actually produced.

    Reports per-expiry economics side by side WITHOUT declaring a winner:
    choosing between expiries is the gate's and the ranking's job, over the
    candidates' own modeled T+1 numbers, not this module's.
    """
    return {
        "status": result.status,
        "multi_expiry_version": MULTI_EXPIRY_VERSION,
        "expiry_ladder_version": EXPIRY_LADDER_VERSION,
        "expiries_considered": result.expiries_considered,
        "listed_expiration_count": len(result.available_expirations),
        "listed_strike_count": len(result.listed_strikes),
        "candidates": len(result.candidates),
        "truncation_note": result.truncation_note,
        "budget": {
            "underlying_quotes": result.budget.underlying_quotes,
            "metadata_calls": result.budget.metadata_calls,
            "chain_discovery_calls": result.budget.chain_discovery_calls,
            "selected_leg_quote_calls": result.budget.selected_leg_quote_calls,
            "unique_contracts_quoted": result.budget.unique_contracts_quoted,
            "contracts_deduplicated": result.budget.contracts_deduplicated,
            "total_requests": result.budget.total,
        },
        "latency_ms": {
            "metadata": str(result.metadata_latency_ms),
            "quotes": str(result.quote_latency_ms),
            "total": str(result.latency_ms),
        },
        "per_expiry": [
            {
                "expiration": s.variant.expiration.isoformat(),
                "ladder_position": s.variant.ladder_position,
                "entry_dte": s.variant.entry_dte,
                "dte_at_settlement": s.variant.dte_at_settlement,
                "settlement_risk": s.variant.settlement_risk,
                "expires_on_settlement_date": s.variant.expires_on_settlement_date,
                "implied_move_pct": (
                    None if s.implied_move_pct is None else str(s.implied_move_pct)
                ),
                "implied_move_source": s.implied_move_source,
                "listed_strikes_in_window": s.listed_strike_count,
                "chain_quotes": s.chain_quote_count,
                "candidates": len(s.candidates),
                "failure_category": s.failure_category,
                "failure_detail": s.failure_detail,
            }
            for s in result.per_expiry
        ],
        "failure_category": result.failure_category,
        "failure_detail": result.failure_detail,
    }


__all__ = [
    "MAX_CANDIDATES_PER_EXPIRY",
    "MULTI_EXPIRY_OK",
    "MULTI_EXPIRY_SINGLE",
    "MULTI_EXPIRY_UNAVAILABLE",
    "MULTI_EXPIRY_VERSION",
    "ExpiryCandidateSet",
    "MultiExpiryResult",
    "build_multi_expiry_universe",
    "summarize_multi_expiry",
]
