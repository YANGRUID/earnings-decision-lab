# Interactive Brokers integration (Phase 13)

This project can source real options-chain data from the user's own Interactive Brokers account,
via the official **Client Portal Gateway** — a small Java process IBKR ships that the user runs
and authenticates *themselves*, entirely outside this codebase. This project never sees an IBKR
username, password, or 2FA code, and never talks to IBKR's cloud directly.

**READ ONLY.** No code anywhere in this integration calls an order-placement, order-modification,
order-cancellation, order-preview, what-if, or exercise endpoint. There is no order schema, no
execution service, and no UI for submitting one. This is enforced by what was built, not just by
policy: `IBKRClient` (`backend/src/providers/ibkr_client.py`) only ever issues `GET` requests.

## Architecture

```
Mac (user's own machine)
    IBKR Client Portal Gateway  (https://localhost:5001, self-authenticated by the user)
        |
        | GET only, TLS verification disabled for this client alone
        v
    Earnings Decision Lab backend
        IBKRClient           -- session/auth, error translation
        IBKROptionsProvider  -- implements the existing OptionsDataProvider interface
        IBKRPortfolioProvider -- real, read-only positions
```

Deliberately **local-first**: the Gateway is not assumed to run in any cloud environment, and this
integration doesn't try to solve that. If/when the backend moves to Azure, either (a) the local
Gateway keeps running and only forwards already-collected snapshots, or (b) a different, officially
supported IBKR cloud authentication model is adopted — that decision is explicitly deferred, not
made here.

## Authentication

**Two supported workflows as of Phase 4.8A**, both against the exact same Gateway and the exact
same `IBKRClient`/`IBKROptionsProvider`/`IBKRPortfolioProvider` described on this page — nothing
below changes between them:

1. **Manual (this page's original design, still fully supported).** You run the Gateway yourself
   and log in by hand at `https://localhost:5001` — this backend never sees your username,
   password, or 2FA code.
2. **Automated (Phase 4.8A, optional).** `docker-compose.yml`'s `ibkr-gateway` service runs the
   same Gateway inside a container and logs in on your behalf via
   [IBeam](https://github.com/Voyz/ibeam), so the session stays authenticated 24/7 without a human
   re-logging in every few hours. This backend application still never reads your credentials —
   only that one new, isolated container does. See **`docs/ibkr_gateway_runtime.md`** for the full
   setup, 2FA options, reliability design, and security model.

Every call first checks `/iserver/auth/status`. Real response, captured live:

```json
{"authenticated": true, "established": true, "competing": false, "connected": true, ...}
```

`IBKRClient.ensure_authenticated()` raises a specific, distinguishable error for every non-usable
state — `IBKRNotAuthenticatedError` (session not logged in), `IBKRCompetingSessionError` (another
session, e.g. TWS, already holds the connection), `IBKRGatewayUnavailableError` (the Gateway
process itself isn't reachable). None of these crash the application; see "Error handling" below.

TLS verification is disabled **only** for `IBKRClient`'s own `httpx.Client` instance — the Gateway
serves a local, self-signed certificate for `https://localhost`, which is expected IBKR behavior
for local development, not a real trust concern for a loopback-only connection. No other HTTP
client in this codebase disables verification.

## Options-chain discovery flow

Confirmed live end-to-end for NVDA (conid `4815747`) during Phase 13 development:

```
/iserver/secdef/search?symbol=NVDA        -> underlying conid + available option months
/iserver/secdef/strikes?conid=...&month=  -> valid strikes for that month
/iserver/secdef/info?conid=...&strike=... -> exact expirations + real option conids
/iserver/marketdata/snapshot?conids=...   -> bid/ask/last/volume/Greeks/IV + data-quality flag
```

Unlike Alpha Vantage's `REALTIME_OPTIONS` (one call returns an entire chain), the Gateway has no
bulk endpoint. Fetching "everything" would mean walking every listed month and strike — for NVDA
alone, 14 listed months × ~130 strikes × 2 rights. `IBKROptionsProvider` deliberately fetches a
**bounded window** instead:

- **Expiration:** the nearest listed expiration strictly *after* a reference date (the real
  earnings date being researched, threaded through from
  `services/options_analytics.collect_forward_options_snapshot`), reusing the same
  `select_expiration_after` function Phase 12 already built and tested — not a duplicate rule.
- **Strikes:** `STRIKES_AROUND_ATM` (5) strikes on each side of the current ATM strike, both calls
  and puts — 11 strikes × 2 = 22 contracts for the initial NVDA test, matching what the user's
  spec asked for ("approximately 5 strikes below ATM, ATM, approximately 5 above, both CALL and
  PUT").

A real, necessary quirk found live: the snapshot endpoint needs a **priming call** before a second
call (after a real pause — `_SNAPSHOT_PRIMING_DELAY_SECONDS = 2.0`) returns actual market data; the
very first attempt at this, with zero delay between the two calls, silently returned only identity
fields (conid) for all 22 contracts — a real bug caught and fixed during Phase 13's own live
verification, not a hypothetical.

## Real market data fields

Requested field codes, confirmed against real NVDA responses (not assumed from documentation
alone):

| Field | Meaning | Notes |
|---|---|---|
| 31 | Last | |
| 84 / 86 | Bid / Ask | |
| 87 (+ `87_raw`) | Volume | prefers the raw numeric companion field over the "65.5M"-style display string |
| 6509 | Market Data Availability | see below |
| 7308–7311 | Delta / Gamma / Theta / Vega | plain decimals |
| 7633 | Implied Volatility | returned as `"31.9%"`, converted to `0.319` |
| 7638 | Open Interest | **not officially documented** in any source this project could verify — confirmed empirically from real responses (`"4.12K"`, `"791"`, etc.); genuinely absent on some real contracts, never fabricated as 0 |

### Market data quality (live / delayed / frozen / unavailable)

Field 6509's first character is IBKR's own documented signal
(`docs.interactivebrokers.com/docs/web-api/v1/endpoints/market-data/market-data-availability`):
`R`=RealTime, `D`=Delayed, `Z`=Frozen, `Y`=Frozen Delayed, `N`=Not Subscribed. `decode_market_data_quality`
decodes only this first character — the second/third characters observed live (e.g. `"ZBd"` on real
option responses) aren't documented with enough confidence in any source this project could verify,
so they're deliberately left undecoded rather than guessed.

**Real, observed result for this account:** NVDA's underlying stock quote came back `"DB"` (delayed)
at 16:02 ET; NVDA's option quotes came back `"ZBd"` (frozen) at the same moment, ~2 minutes after
the regular session closed. This is consistent with a common real entitlement pattern (delayed
stock quotes available without a subscription; options data requiring one this account doesn't
have) but a second check during live market hours would be needed to fully separate "no options
subscription" from "just reflecting the market being closed" — noted honestly rather than assumed.

## Point-in-time integrity

Every `OptionsSnapshot` row carries a real `snapshot_timestamp` (when the collection was logically
run) and a real `retrieved_at` (when the HTTP response actually arrived) — the two can differ by a
couple of seconds because of the priming delay above, which is expected and correctly modeled, not
a bug. `collect_forward_options_snapshot` (Phase 12, unchanged in Phase 13) already guarantees:

- a snapshot is only ever collected once per company per day (`_already_collected_today`),
- the underlying price used is always the latest `PriceBar` on or before the snapshot date, never
  a later one (regression-tested in Phase 12),
- historical snapshots are never overwritten — each is a new row.

Since the Gateway's `/iserver/marketdata/snapshot` endpoint only ever returns *current* data (there
is no "as of a past date" parameter), `as_of` is always the real wall-clock time of the actual
fetch — there is no mechanism by which IBKR data could leak future information into an earlier
snapshot.

## Real verification (Phase 13)

Performed against the user's own real, authenticated account. The real account number is never
printed anywhere in this repository, in logs, or in this document — masked per
`providers/ibkr_client.mask_account_id` (first 3 + `****` + last 2 characters).

| Check | Result |
|---|---|
| Authentication | `authenticated=true, connected=true, competing=false` |
| Account detected | 1 real account (masked in every log/report) |
| Portfolio data | real, zero-balance, zero-position account (CHF base currency) — a real, valid state, not a failure |
| NVDA underlying quote | real, `delayed` |
| NVDA options chain discovery | 22/22 real contracts resolved (11 strikes × calls/puts, expiration selected as the nearest listed date after the real earnings date) |
| NVDA options market data | 22/22 contracts returned real bid/ask/Greeks/IV; `frozen` (market closed at fetch time); open interest present on ~half the contracts, honestly null on the rest |
| `OptionsSnapshot` rows | 0 → 22 |
| `VolatilitySnapshot` rows | 0 → 1 (real ATM IV, implied move, put/call ratios — see below) |
| Earnings Event page | populated ATM IV, implied move %/$, expiration used, put/call OI ratio — with **zero frontend code changes**, since Phase 12 already built the real display logic |

Real computed values for NVDA (2026-08-17, ahead of the 2026-08-26 earnings date): ATM strike
$225, expiration 2026-08-28, implied move 6.41% ($14.43), ATM IV 46.3% (call/put IV matched
exactly, no divergence), put/call OI ratio 0.022, put/call volume ratio 0.248. IV term structure
was not computed on this run — it needs a *second* expiration's contracts, and this collector only
fetches one bounded window per run by design (a real, deliberate scope limit, not a bug).

## Portfolio (read-only)

`IBKRPortfolioProvider` fetches `/iserver/accounts` then `/portfolio/{accountId}/positions/{page}`
(paginated 100/page). Normalized into `PortfolioPosition` — a type kept deliberately separate from
every market-data type (a position is "what I hold", never a market quote) — and persisted into
`PortfolioPositionSnapshot`, its own append-only table, never mixed with `OptionsSnapshot`. Real
field shape, from IBKR's own documentation:

```json
{"conid": 672387468, "contractDesc": "MNQ MAR2025", "position": 2.0, "mktPrice": 21770.43,
 "mktValue": 87081.72, "avgCost": 43536.12, "unrealizedPnl": 9.48, "realizedPnl": 0.0,
 "currency": "USD", "assetClass": "FUT", "expiry": null, "putOrCall": null, "strike": 0.0}
```

The real account currently holds zero positions — verified live, an honest and valid empty state.
`GET /api/v1/portfolio/positions` serves whatever was last collected (never queries the Gateway
live on a request); `uv run python -m ingestion.collect_portfolio_snapshot` collects a real batch.
The long-term goal (a future phase, not built here) is an Earnings Event page that can show "my
current exposure to this company" — this phase is backend/data integration only, no UI changes.

## Configuration

```
OPTIONS_PROVIDER=ibkr          # alpha_vantage | ibkr, see providers/factory.py
IBKR_BASE_URL=https://host.docker.internal:5001/v1/api
```

`IBKR_BASE_URL` is never hard-coded elsewhere in the codebase — always read from `Settings`.

**Docker Compose networking (Phase 13, fixed in the on-demand-options-collection work):** the
dockerized backend container's `localhost` is the container itself, not the host machine, so
`IBKR_BASE_URL=https://localhost:5001/v1/api` cannot reach a Gateway running on the host from
inside `docker compose` — confirmed live as `IBKRGatewayUnavailableError`, reported cleanly, no
crash, rather than assumed. `docker-compose.yml`'s `backend` service now maps
`host.docker.internal` to the host on every platform (`extra_hosts: host-gateway`, not just a
Docker Desktop built-in), and `.env.example` defaults `IBKR_BASE_URL` to
`https://host.docker.internal:5001/v1/api` accordingly. Running the backend directly on the host
instead (no Docker) still needs plain `localhost` — change `IBKR_BASE_URL` back if that's your
setup. A future Azure deployment remains an explicitly separate, deferred decision.

## Error handling

None of the following crash the application; each maps to a specific, distinguishable exception
(`IBKRGatewayUnavailableError`, `IBKRNotAuthenticatedError`, `IBKRCompetingSessionError`,
`IBKRRateLimitedError`, `IBKRContractNotFoundError`, or a generic `IBKRError`), all handled
explicitly in `ingestion/collect_options_snapshots.py` and `ingestion/collect_portfolio_snapshot.py`:
Gateway not running, Gateway running but not authenticated, session expired, competing session,
no market-data entitlement (surfaces as `market_data_quality="unavailable"`, not an exception),
rate limits, unknown conid (skipped per-contract, not fatal), empty expiration list, empty strike
list, partial market-data fields (nulled per-field, never fabricated), and request timeouts.

## Testing

Every automated test mocks the Gateway's HTTP responses (via `httpx.MockTransport` for the
multi-step discovery flow, `pytest-httpx` for single-call cases) using real, live-captured response
shapes — CI never depends on, or even attempts to reach, a live IBKR account. See
`backend/tests/test_providers_ibkr_client.py`, `test_providers_ibkr_options.py`,
`test_providers_ibkr_portfolio.py`, and `test_services_portfolio.py`.
