"""Black-Scholes-Merton pricing and Greeks.

Used only when a provider doesn't supply Greeks directly, or to sanity-check
provider values — never to override real market-quoted prices. Every value
this module produces is tagged ``GreeksSource.BLACK_SCHOLES`` when persisted
(see models.enums), so a Black-Scholes estimate is never confused with a
provider-quoted Greek downstream.

Documented model assumptions (see docs/options_methodology.md for the full
discussion):
  - European exercise. NVDA/AMD/MU/SNDK equity options are American-style
    (early exercise is possible), so this is a genuine model/market mismatch,
    not just a simplification — Black-Scholes prices are an approximation
    for American options, most accurate for calls on non-dividend-paying
    stock and least accurate for deep ITM puts. This is never silently
    papered over.
  - Constant volatility, constant risk-free rate, continuous trading, no
    transaction costs, lognormal underlying price distribution.
  - Dividends handled via a continuous yield ``q`` (0 by default) — a
    simplification of discrete dividend payments, adequate for short-dated
    options on these tickers but not exact.

Uses ``statistics.NormalDist`` (stdlib) for the normal CDF/PDF rather than
adding a numpy/scipy dependency for this alone.
"""

import math
from dataclasses import dataclass
from statistics import NormalDist

from models.enums import OptionType

_N = NormalDist()


@dataclass(frozen=True)
class Greeks:
    price: float
    delta: float
    gamma: float
    theta: float  # per calendar day
    vega: float  # per 1 vol point (0.01)
    rho: float  # per 1% rate move (0.01)


def _d1_d2(
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    rate: float,
    vol: float,
    dividend_yield: float,
) -> tuple[float, float]:
    if time_to_expiry_years <= 0:
        raise ValueError("time_to_expiry_years must be positive")
    if vol <= 0:
        raise ValueError("vol must be positive")
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    sqrt_t = math.sqrt(time_to_expiry_years)
    d1 = (
        math.log(spot / strike) + (rate - dividend_yield + 0.5 * vol**2) * time_to_expiry_years
    ) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t
    return d1, d2


def price_and_greeks(
    option_type: OptionType,
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    rate: float,
    vol: float,
    dividend_yield: float = 0.0,
) -> Greeks:
    d1, d2 = _d1_d2(spot, strike, time_to_expiry_years, rate, vol, dividend_yield)
    sqrt_t = math.sqrt(time_to_expiry_years)
    disc_q = math.exp(-dividend_yield * time_to_expiry_years)
    disc_r = math.exp(-rate * time_to_expiry_years)
    pdf_d1 = _N.pdf(d1)

    gamma = disc_q * pdf_d1 / (spot * vol * sqrt_t)
    vega = spot * disc_q * pdf_d1 * sqrt_t / 100  # per 1 vol point

    if option_type == OptionType.CALL:
        price = spot * disc_q * _N.cdf(d1) - strike * disc_r * _N.cdf(d2)
        delta = disc_q * _N.cdf(d1)
        theta_annual = (
            -spot * disc_q * pdf_d1 * vol / (2 * sqrt_t)
            - rate * strike * disc_r * _N.cdf(d2)
            + dividend_yield * spot * disc_q * _N.cdf(d1)
        )
        rho = strike * time_to_expiry_years * disc_r * _N.cdf(d2) / 100
    else:
        price = strike * disc_r * _N.cdf(-d2) - spot * disc_q * _N.cdf(-d1)
        delta = -disc_q * _N.cdf(-d1)
        theta_annual = (
            -spot * disc_q * pdf_d1 * vol / (2 * sqrt_t)
            + rate * strike * disc_r * _N.cdf(-d2)
            - dividend_yield * spot * disc_q * _N.cdf(-d1)
        )
        rho = -strike * time_to_expiry_years * disc_r * _N.cdf(-d2) / 100

    return Greeks(
        price=price,
        delta=delta,
        gamma=gamma,
        theta=theta_annual / 365,
        vega=vega,
        rho=rho,
    )
