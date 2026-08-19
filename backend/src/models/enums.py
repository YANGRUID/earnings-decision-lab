import enum


class AnnouncementTime(enum.StrEnum):
    BEFORE_MARKET = "before_market"
    AFTER_MARKET = "after_market"
    UNKNOWN = "unknown"


class RevisionDirection(enum.StrEnum):
    UP = "up"
    DOWN = "down"
    FLAT = "flat"
    UNKNOWN = "unknown"


class OptionType(enum.StrEnum):
    CALL = "call"
    PUT = "put"


class GreeksSource(enum.StrEnum):
    PROVIDER = "provider"
    BLACK_SCHOLES = "black_scholes"


class MarketDataQuality(enum.StrEnum):
    """Whether a stored quote reflects real-time, delayed, or stale
    (frozen, e.g. last recorded at market close) data -- only ever set from
    a real signal a provider actually returned (see
    providers/ibkr_client.py's decoding of IBKR's own market-data-
    availability flag), never guessed."""

    LIVE = "live"
    DELAYED = "delayed"
    FROZEN = "frozen"
    UNKNOWN = "unknown"


class FilingType(enum.StrEnum):
    FORM_10K = "10-K"
    FORM_10Q = "10-Q"
    FORM_8K = "8-K"
    EARNINGS_RELEASE = "earnings_release"
    INVESTOR_PRESENTATION = "investor_presentation"
    OTHER = "other"


class OptionsSnapshotAnchor(enum.StrEnum):
    """Whether an options-chain snapshot's expiration was chosen relative to
    a known upcoming earnings date, or as a general "nearest practical"
    snapshot taken when no reliable earnings date exists yet -- see the two
    expiration-selection rules in analytics/options/implied_move.py
    (select_expiration_after vs select_nearest_listed_expiration). Set once,
    at collection time, from which rule actually ran -- never inferred
    after the fact from expiration_date alone."""

    EARNINGS_ANCHORED = "earnings_anchored"
    GENERAL_CURRENT = "general_current"


class UpcomingEarningsDateSource(enum.StrEnum):
    """Provenance of an upcoming earnings report date. A provider-confirmed
    date, a manually entered one, and an algorithmically estimated one
    carry different reliability and must never be silently conflated --
    anything downstream that reads an EarningsEstimateSnapshot's date must
    read this field too, not assume the date is provider-confirmed."""

    ALPHA_VANTAGE = "alpha_vantage"
    MANUAL = "manual"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class ProviderHealthStatus(enum.StrEnum):
    """Outcome of a real provider check -- either an explicit "Test
    Connection" call or a real failure observed during actual ingestion.
    Never inferred; always the direct result of a real HTTP call or a real
    exception class raised by the provider adapter (see
    services/provider_status.py)."""

    CONNECTED = "connected"
    AUTH_FAILED = "auth_failed"
    RATE_LIMITED = "rate_limited"
    PREMIUM_REQUIRED = "premium_required"
    GATEWAY_OFFLINE = "gateway_offline"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"


class DataState(enum.StrEnum):
    """Shared vocabulary for "what kind of data is this, right now" --
    every page that shows a data point derived from a provider must pick
    one of these rather than inventing its own ad hoc label. See
    analytics/data_state.py for the real derivation rules."""

    LIVE = "live"
    DELAYED = "delayed"
    FROZEN = "frozen"
    STALE = "stale"
    PREVIOUS_SESSION = "previous_session"
    MARKET_CLOSED = "market_closed"
    GATEWAY_DISCONNECTED = "gateway_disconnected"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    RATE_LIMITED = "rate_limited"
    PREMIUM_REQUIRED = "premium_required"
    NOT_COLLECTED = "not_collected"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class DecisionDirection(enum.StrEnum):
    """The AI Options Decision Engine's directional view (Phase 14.9) --
    an LLM judgment grounded in the same real evidence an Earnings Thesis
    uses, never a fabricated number. See services/decision_engine.py."""

    STRONG_BULLISH = "strong_bullish"
    BULLISH = "bullish"
    NEUTRAL = "neutral"
    BEARISH = "bearish"
    STRONG_BEARISH = "strong_bearish"


class DecisionVolatilityView(enum.StrEnum):
    LONG_VOL = "long_vol"
    NEUTRAL_VOL = "neutral_vol"
    SHORT_VOL = "short_vol"


class DecisionSource(enum.StrEnum):
    """Whether a decision's direction/volatility view came from the LLM
    or was manually overridden by the owner (see Phase 14.9 Part K) --
    strategy ranking is deterministic either way, only the input view
    differs."""

    AI = "ai"
    MANUAL_OVERRIDE = "manual_override"


class DecisionStatus(enum.StrEnum):
    OPEN = "open"
    SETTLED = "settled"
    VOID = "void"


class OptionsSnapshotPurpose(enum.StrEnum):
    """Why an options/volatility snapshot was captured, set once at
    collection time from which code path actually ran -- never inferred
    later from its timestamp alone (see Phase 14.10 Part D). This is
    orthogonal to OptionsSnapshotAnchor (which describes *which expiration*
    was chosen); purpose describes *when/why* the capture happened, and is
    what lets the snapshot-selection fallback (services/options_analytics.py)
    honestly distinguish "captured deliberately at a session's close" from
    "just whatever was most recently captured on demand or by the forward
    scheduler" -- see OptionsSnapshotPurpose.CLOSE's docstring below for the
    exact honesty rule this enables."""

    #: Captured on demand (a user's Prepare/Refresh click) or by the
    #: forward-collection cron -- the default for every snapshot this
    #: project has ever taken before this enum existed.
    INTRADAY = "intraday"
    #: Deliberately captured at/near a real market close by
    #: ingestion/capture_close_snapshot.py -- ONLY a snapshot with this
    #: purpose may ever be labeled "Previous close" in the UI; every other
    #: snapshot is labeled by its real timestamp instead (e.g.
    #: "Previous-session snapshot · 15:21 ET"), never assumed to be a close.
    CLOSE = "close"
    #: A human-triggered one-off capture outside the normal pipeline.
    MANUAL = "manual"
    #: Captured specifically anchored to an upcoming earnings event (T-14/
    #: T-7/T-3/T-1) by the forward-collection scheduler.
    EARNINGS = "earnings"


class StrategyRiskPreference(enum.StrEnum):
    """Owner-controlled ceiling on which strategy categories the Decision
    Engine (and Strategy Lab) may surface -- defaults to the most
    conservative tier. See services/provider_settings.py. Uncovered short
    options are deliberately not generated by this project yet (see
    analytics/options/strategy_candidates.py) so ADVANCED today behaves
    identically to ALLOW_SINGLE_LEG_LONG; the tier exists so a future
    naked-short-supporting change has a real setting to gate on rather
    than needing a new migration."""

    DEFINED_RISK_ONLY = "defined_risk_only"
    ALLOW_SINGLE_LEG_LONG = "allow_single_leg_long"
    ADVANCED_ALLOW_UNCOVERED_SHORT = "advanced_allow_uncovered_short"
