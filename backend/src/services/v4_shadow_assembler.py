"""V4.5 -- live V4 shadow candidate assembly.

THE MISSING PIECE. V4.4C built the evidence model (freeze, immutability,
isolation) but nothing that produces candidates from a live chain. This
module is that path: authoritative event + point-in-time market state ->
a complete, ranked, executable candidate set ready to freeze.

REQUEST DISCIPLINE (Sections 9, 12, 39). The V4.3.1 principle is
preserved exactly:

    broad strike METADATA (one reqSecDefOptParams-equivalent call)
        -> bounded geometry generation
        -> exact contract selection
        -> DEDUPE across every candidate
        -> quote ONLY the unique contracts actually required

There is no full-chain market-data sweep. A contract needed by five
candidates is quoted once, and the same quote object (with its own real
timestamp) is shared by all five -- which is also what makes cross-leg
skew meaningful rather than an artifact of re-requesting.

CONNECTION OWNERSHIP (Section 13). Market data is obtained through the
provider handed in by the caller, which in production is the ONE shared
lifespan-owned IBKRTWSProvider. This module never constructs a provider,
never opens a connection, and never picks a client id.

NO LOOK-AHEAD (Section 7). Every input is a decision-time observation.
This module imports no settlement, exit, price-reaction, or realized-
outcome module -- asserted structurally in the V4.5 isolation tests.

NO FABRICATION (Section 16). Entry economics use the executable
convention only: BUY pays ASK, SELL receives BID. A missing required side
makes a candidate QUOTE_INCOMPLETE. There is no midpoint, no last-price
substitution, no previous close, and no historical fallback anywhere in
this file.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from analytics.decision.v4_chain_coverage import ChainMetadata
from analytics.decision.v4_compatibility import evaluate_semantic_compatibility
from analytics.decision.v4_expected_move import (
    ExpectedMoveContext,
    derive_expected_move_context,
)
from analytics.decision.v4_market_view import derive_v4_market_view
from analytics.decision.v4_strategy_semantics import get_strategy_semantics
from analytics.decision.v4_strike_geometry_variants import (
    generate_all_strategy_variant_sets,
)
from analytics.decision.v4_t1_valuation_context import V4T1LegInput, V4T1ValuationContext
from analytics.options.strategy_candidates import StrategyCategory
from models.enums import DecisionDirection, DecisionVolatilityView
from providers.base import OptionsDataProvider
from providers.types import OptionQuote, SelectedLeg
from services.v4_shadow import ShadowCandidateInput

#: Section 11 -- a global safety cap on top of V4.3.1's own
#: MAX_VARIANTS_PER_STRATEGY. Truncation is always reported, never
#: silent (see AssemblyResult.truncation_note).
MAX_TOTAL_CANDIDATES = 60

#: Section 39 -- every IBKR interaction this module performs is counted,
#: because "estimate loosely" is exactly how a request budget becomes a
#: surprise at scale.


@dataclass
class RequestBudget:
    """Real measured IBKR interaction counts -- never an estimate."""

    underlying_quotes: int = 0
    metadata_calls: int = 0
    chain_discovery_calls: int = 0
    selected_leg_quote_calls: int = 0
    unique_contracts_quoted: int = 0

    @property
    def total(self) -> int:
        return (
            self.underlying_quotes
            + self.metadata_calls
            + self.chain_discovery_calls
            + self.selected_leg_quote_calls
        )


@dataclass
class StageLatency:
    """Section 37 -- per-stage timing, so a budget overrun can be
    attributed rather than guessed at."""

    expected_move_ms: Decimal = Decimal(0)
    metadata_ms: Decimal = Decimal(0)
    geometry_ms: Decimal = Decimal(0)
    quote_acquisition_ms: Decimal = Decimal(0)
    total_ms: Decimal = Decimal(0)


@dataclass
class AssemblyResult:
    """Everything the orchestrator needs to freeze -- or an honest
    reason why nothing could be assembled."""

    candidates: list[ShadowCandidateInput] = field(default_factory=list)
    expiration: date | None = None
    underlying_price: Decimal | None = None
    underlying_quote_at: datetime | None = None
    market_data_quality: str | None = None
    expected_move_context: ExpectedMoveContext | None = None
    chain_metadata_source: str | None = None
    budget: RequestBudget = field(default_factory=RequestBudget)
    latency: StageLatency = field(default_factory=StageLatency)
    failure_category: str | None = None
    failure_detail: str | None = None
    generated_candidate_count: int = 0
    truncation_note: str | None = None


def _contract_key(strike: Decimal, right: str) -> tuple[Decimal, str]:
    """Section 12 -- economic identity for dedupe. conId is preferable
    where already resolved, but at geometry time the exact contract has
    not been resolved yet, so (strike, right) within one ticker+
    expiration context is the honest equivalent."""
    return (strike, right.lower())


def _right_word(right: str) -> str:
    """V4 uses Literal["call","put"]; TWS wire form is C/P. Normalizing
    here prevents the class of bug that turned every scenario into
    PRICING_ERROR during the V4.4B replay."""
    r = right.lower()
    if r in ("c", "call"):
        return "call"
    if r in ("p", "put"):
        return "put"
    return r


def _coerce_enum(enum_cls, value):
    """String -> enum member (by value or name), enum/None passthrough."""
    if value is None or isinstance(value, enum_cls):
        return value
    text = str(value).strip()
    try:
        return enum_cls(text.lower())
    except ValueError:
        try:
            return enum_cls[text.upper()]
        except KeyError:
            return value


def assemble_shadow_candidates(
    *,
    provider: OptionsDataProvider,
    ticker: str,
    as_of: datetime,
    direction: str,
    volatility_view: str | None,
    historical_next_day_move_pcts: list[Decimal] | None = None,
    earnings_date: date | None = None,
) -> AssemblyResult:
    """Builds the complete candidate set from live point-in-time data.

    Returns an ``AssemblyResult`` in every case -- including failure --
    so the orchestrator can record an honest shadow outcome rather than
    propagating an exception into the scheduler run (Section 31).
    """
    started = time.monotonic()
    result = AssemblyResult()

    # --- 1. Underlying, at decision time -----------------------------
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

    # --- 2. Expiration, via the V4 policy ----------------------------
    # Section 79 -- V4's own expiration policy, NOT V3's expiration
    # score. The provider's own resolver already encodes the earnings-
    # anchored rule; a None earnings_date degrades to nearest-listed.
    metadata_started = time.monotonic()
    try:
        expiration = provider.resolve_expiration_for_reconstruction(
            ticker, as_of.date(), earnings_date
        )
        result.budget.metadata_calls += 1
    except Exception as exc:  # noqa: BLE001
        result.failure_category = "CHAIN_METADATA_FAILED"
        result.failure_detail = f"expiration resolution failed: {type(exc).__name__}: {exc}"
        return result
    if expiration is None:
        result.failure_category = "CHAIN_METADATA_FAILED"
        result.failure_detail = "no listed expiration could be resolved"
        return result
    result.expiration = expiration
    result.latency.metadata_ms = Decimal(str((time.monotonic() - metadata_started) * 1000))

    # --- 3. Bounded discovery chain for the expected-move context ----
    # get_option_chain is the provider's OWN bounded ATM window (V4.3's
    # documented input), never a full-chain sweep -- see this module's
    # docstring and providers/ibkr_tws_options.py::STRIKES_AROUND_ATM.
    em_started = time.monotonic()
    try:
        chain_quotes = provider.get_option_chain(ticker, as_of, expiration=expiration)
        result.budget.chain_discovery_calls += 1
    except Exception as exc:  # noqa: BLE001
        result.failure_category = "MARKET_DATA_UNAVAILABLE"
        result.failure_detail = f"chain discovery failed: {type(exc).__name__}: {exc}"
        return result
    if not chain_quotes:
        result.failure_category = "CHAIN_METADATA_FAILED"
        result.failure_detail = f"no quotes returned for {ticker} {expiration}"
        return result

    expected_move = derive_expected_move_context(
        spot=underlying.price,
        observed_at=underlying.timestamp,
        expiration=expiration,
        quotes_for_expiration=list(chain_quotes),
        historical_next_day_move_pcts=historical_next_day_move_pcts,
    )
    result.expected_move_context = expected_move
    result.latency.expected_move_ms = Decimal(str((time.monotonic() - em_started) * 1000))

    # Section 10 -- the trading-class filter already applied inside the
    # provider's own metadata path is what keeps adjusted series (2AAPL
    # and friends) out of this set; the strikes below come from real
    # normal-series quotes only.
    call_strikes = tuple(sorted({q.strike for q in chain_quotes if q.option_type == "call"}))
    put_strikes = tuple(sorted({q.strike for q in chain_quotes if q.option_type == "put"}))
    chain_metadata = ChainMetadata(
        ticker=ticker.upper(),
        expiration=expiration,
        observed_at=underlying.timestamp,
        call_strikes=call_strikes,
        put_strikes=put_strikes,
        # Honest provenance: this IS a bounded ATM window from the
        # provider's own discovery call, not an unsliced complete listing.
        # Claiming "complete_listed" here would make V4.3.1's coverage
        # judgments assert a real absence it cannot actually confirm.
        source="captured_window",
        captured_window_size=len(call_strikes) or None,
    )
    result.chain_metadata_source = chain_metadata.source

    # --- 4. Bounded geometry generation (V4.3.1) ---------------------
    geometry_started = time.monotonic()
    try:
        variant_sets = generate_all_strategy_variant_sets(
            expected_move, list(chain_quotes), chain_metadata=chain_metadata
        )
    except Exception as exc:  # noqa: BLE001
        result.failure_category = "NO_VALID_CANDIDATE"
        result.failure_detail = f"geometry generation failed: {type(exc).__name__}: {exc}"
        return result
    result.latency.geometry_ms = Decimal(str((time.monotonic() - geometry_started) * 1000))

    # --- 5. Flatten to concrete candidates, dedupe contracts ---------
    # Live-found defect (activation dry-run, 2026-09-02): the DecisionView
    # carries plain strings ("bullish", "long_vol"), while
    # derive_v4_market_view reads enum members. The orchestration path
    # passed strings through here behind a type: ignore, which every test
    # had mocked away -- the first real in-process run raised
    # AttributeError: 'str' object has no attribute 'value'. Coerce at the
    # boundary; enum members and None pass through unchanged.
    market_view = derive_v4_market_view(
        _coerce_enum(DecisionDirection, direction),
        _coerce_enum(DecisionVolatilityView, volatility_view),
    )

    # (candidate_id, strategy, variant_id, legs). ``strategy`` is the real
    # StrategyCategory enum, carried through to the valuation context
    # unchanged -- never stringified and re-parsed.
    flat: list[tuple[str, StrategyCategory, str, tuple]] = []
    for strategy, candidate_set in variant_sets.items():
        for variant in candidate_set.variants:
            selection = variant.result
            if selection.status != "constructed" or not selection.legs:
                continue
            legs = tuple(
                (leg.action, _right_word(leg.right), leg.selected_strike, leg.quantity)
                for leg in selection.legs
                if leg.selected_strike is not None
            )
            if len(legs) != len(selection.legs):
                # A leg without a resolved strike cannot be quoted or
                # valued honestly -- skip rather than invent one.
                continue
            flat.append(
                (f"{strategy}:{variant.variant_id}", strategy, variant.variant_id, legs)
            )

    result.generated_candidate_count = len(flat)
    if not flat:
        result.failure_category = "NO_VALID_CANDIDATE"
        result.failure_detail = "no strategy produced a constructable geometry"
        return result

    if len(flat) > MAX_TOTAL_CANDIDATES:
        result.truncation_note = (
            f"generated {len(flat)} candidates; retained the first {MAX_TOTAL_CANDIDATES} "
            f"(MAX_TOTAL_CANDIDATES cap). Reason: bounded latency/request budget."
        )
        flat = flat[:MAX_TOTAL_CANDIDATES]

    # Section 12 -- one request per unique contract, shared by every
    # candidate that uses it.
    unique: dict[tuple[Decimal, str], SelectedLeg] = {}
    for _cid, _strategy, _variant, legs in flat:
        for action, right, strike, _qty in legs:
            key = _contract_key(strike, right)
            if key not in unique:
                unique[key] = SelectedLeg(strike=strike, option_type=right, action=action)

    quote_started = time.monotonic()
    try:
        quotes = provider.get_quotes_for_selected_legs(
            ticker, list(unique.values()), expiration, as_of
        )
        result.budget.selected_leg_quote_calls += 1
        result.budget.unique_contracts_quoted = len(unique)
    except Exception as exc:  # noqa: BLE001
        result.failure_category = "MARKET_DATA_UNAVAILABLE"
        result.failure_detail = (
            f"selected-leg quote acquisition failed: {type(exc).__name__}: {exc}"
        )
        return result
    result.latency.quote_acquisition_ms = Decimal(
        str((time.monotonic() - quote_started) * 1000)
    )

    by_contract: dict[tuple[Decimal, str], OptionQuote] = {
        _contract_key(q.strike, q.option_type): q for q in quotes
    }

    # --- 6. Build executable candidate inputs ------------------------
    semantics_cache: dict[str, object] = {}
    # keyed by the string form of StrategyCategory -- the enum itself is
    # carried through to the valuation context unchanged below.
    assembled: list[ShadowCandidateInput] = []
    for candidate_id, strategy, variant_id, legs in flat:
        leg_inputs: list[V4T1LegInput] = []
        retrieved: dict[int, datetime] = {}
        contract_ids: dict[int, str] = {}
        for index, (action, right, strike, qty) in enumerate(legs):
            quote = by_contract.get(_contract_key(strike, right))
            leg_inputs.append(
                V4T1LegInput(
                    leg_index=index,
                    action=action,  # type: ignore[arg-type]
                    right=right,  # type: ignore[arg-type]
                    strike=strike,
                    quantity=qty,
                    multiplier=Decimal("100"),
                    # A contract with no quote at all yields None on every
                    # field -- the candidate is then honestly
                    # QUOTE_INCOMPLETE, never padded with a default.
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
                # Section 15 -- the provider's own genuinely per-leg
                # retrieved_at, never one aggregate stamp copied across
                # legs.
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
            evaluate_semantic_compatibility(market_view, semantics)  # type: ignore[arg-type]
            if semantics is not None
            else None
        )

        context = V4T1ValuationContext(
            ticker=ticker.upper(),
            underlying_price=underlying.price,
            observed_at=underlying.timestamp,
            entry_timestamp=as_of,
            expected_exit_timestamp=as_of,
            strategy=strategy,  # type: ignore[arg-type]
            expiration=expiration,
            legs=tuple(leg_inputs),
            expected_move_context=expected_move,
        )
        assembled.append(
            ShadowCandidateInput(
                candidate_id=candidate_id,
                context=context,
                semantic_compatibility=compatibility,
                geometry_variant_id=variant_id,
                external_contract_ids=contract_ids,
                leg_retrieved_at=retrieved,
            )
        )

    result.candidates = assembled
    result.latency.total_ms = Decimal(str((time.monotonic() - started) * 1000))
    return result


def summarize_assembly(result: AssemblyResult) -> dict:
    """Flat, serializable summary for the dry-run endpoint and for run
    telemetry. Contains no realized outcome and no order concept."""
    return {
        "expiration": str(result.expiration) if result.expiration else None,
        "underlying_price": str(result.underlying_price) if result.underlying_price else None,
        "market_data_quality": result.market_data_quality,
        "chain_metadata_source": result.chain_metadata_source,
        "generated_candidate_count": result.generated_candidate_count,
        "retained_candidate_count": len(result.candidates),
        "truncation_note": result.truncation_note,
        "failure_category": result.failure_category,
        "failure_detail": result.failure_detail,
        "request_budget": {
            "underlying_quotes": result.budget.underlying_quotes,
            "metadata_calls": result.budget.metadata_calls,
            "chain_discovery_calls": result.budget.chain_discovery_calls,
            "selected_leg_quote_calls": result.budget.selected_leg_quote_calls,
            "unique_contracts_quoted": result.budget.unique_contracts_quoted,
            "total_ibkr_interactions": result.budget.total,
        },
        "latency_ms": {
            "expected_move": str(result.latency.expected_move_ms),
            "metadata": str(result.latency.metadata_ms),
            "geometry": str(result.latency.geometry_ms),
            "quote_acquisition": str(result.latency.quote_acquisition_ms),
            "total": str(result.latency.total_ms),
        },
    }


def utc_now() -> datetime:
    return datetime.now(UTC)
