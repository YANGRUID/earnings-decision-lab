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
