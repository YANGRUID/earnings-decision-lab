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
