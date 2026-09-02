# Earnings Calendar Provider Architecture Review — EarningsAPI.com primary, Finnhub fallback

Status: **implemented and live-verified.** §7's open questions were answered by your follow-up
message (real API key provided, explicit 1–3 req/day target, explicit 7–14 day window rationale) —
see §8 for what actually shipped, including two things this original draft got only partly right.

Branch: `feature/ai-earnings-forward-test`.

Scope: replaces the earnings-calendar provider chain's primary source (Finnhub → EarningsAPI.com,
primary; Finnhub, fallback) to fix real, observed bad forward-looking dates on the dashboard. Does
not touch the decision engine, benchmark/settlement logic, or the frontend beyond the two additive
items requirement 6 asks for (real data-source label, real last-sync timestamp). Per your explicit
instruction, no code has been written for this yet — this is investigation and a proposed design
only.

---

## 1. Current implementation, as it actually is

### 1.1 The real bug, confirmed against live data from your own database

Finnhub's `/calendar/earnings` is a genuine **free-tier limitation, not a bug in this codebase's
sync logic**: your account's key currently returns entries clustering almost entirely around
**May–June 2027**, even for mega-caps with well-known real quarterly cadences (NVDA, WMT, AVGO,
ORCL — confirmed live via direct query against `earnings_calendar_event` earlier this session).
Finnhub's free tier appears to serve a coarse, far-future estimate once its near-term window is
exhausted, rather than erroring — so the sync doesn't fail, it just silently returns dates that
don't reflect near-term reality. The sync code itself (`services/earnings_calendar_sync.py`) is
correct; it faithfully stores whatever Finnhub returns, honestly, with no fabrication anywhere in
the pipeline. The provider's own data is the problem.

### 1.2 The calendar models, sync service, scheduler job, API endpoints, frontend calendar

- **Model**: `models/earnings_calendar_event.py::EarningsCalendarEvent` — one row per
  (symbol, earnings_date). Fields: `symbol`, `company_name`, `logo_url`, `earnings_date`,
  `earnings_time` (`EarningsTiming`: BMO/AMC/DMH/UNKNOWN), `eps_estimate`, `revenue_estimate`,
  `market_cap`, `country`, `source` (`EarningsSource` enum, currently **one member: `FINNHUB`**),
  `status` (`EarningsCalendarEventStatus`), plus `TimestampMixin`'s real `created_at`/`updated_at`.
- **Provider interface**: `providers/base.py::EarningsCalendarProvider` (ABC), two methods:
  `get_earnings_calendar(from_date, to_date) -> list[FinnhubCalendarEntry]` and
  `get_company_profile(symbol) -> FinnhubCompanyProfile | None`. **This interface already exists**
  — see §4.1 on why I'm not creating a second, parallel one.
- **Sync service**: `services/earnings_calendar_sync.py::sync_earnings_calendar(db, provider, *,
  today=None, from_date=None)` — fetches the calendar for `[from_date or today, today +
  SYNC_HORIZON_DAYS]` (365 days forward), then for each unique symbol fetches a company profile
  (name/logo/market cap/country), then upserts. Already has real, tested per-item failure isolation
  (a profile fetch failure for one symbol never aborts the run) and real upsert/date-correction
  logic (see `_find_existing_row`'s own docstring). `source` is currently **never set explicitly on
  either the create or update path** — it silently relies on the column's `default=
  EarningsSource.FINNHUB`, which will need to change (§5.3).
- **Scheduler job**: `services/scheduler.py::run_earnings_calendar_sync_job(from_date=None)` — daily
  cron at 00:00 UTC, builds the provider via `providers/factory.py::build_earnings_calendar_provider`
  and calls `sync_earnings_calendar`. Also reused directly by the dev-only
  `POST /admin/run-earnings-sync` endpoint (`api/routers/admin.py`, non-production only).
- **API endpoints**: `api/routers/earnings_calendar.py` — `GET /earnings-calendar` (UPCOMING-status
  only), `GET /earnings-calendar/{symbol}`, `GET /earnings-calendar/by-month?year=&month=` (new this
  session, any status, for the calendar-grid UI). None of these read from a specific provider —
  they only read `earnings_calendar_event` rows, so **none of them need to change** regardless of
  which provider populated a row.
- **Frontend calendar**: `frontend/src/components/EarningsCalendarGrid.tsx` (month grid) and the
  card-list "Upcoming Earnings" section in `EarningsAnalystDashboard.tsx` — both read the same API
  endpoints above, so both automatically show whatever real data the new provider chain produces,
  with no frontend logic changes required for the data itself. Only the two additive display items
  in requirement 6 need real UI work (§6).

### 1.3 Database schema — confirmed field-by-field against your requirement 2

| Required field | Already exists? | Column |
|---|---|---|
| ticker | Yes | `symbol` |
| company name | Yes | `company_name` |
| earnings date | Yes | `earnings_date` |
| BMO/AMC timing | Yes | `earnings_time` (`EarningsTiming` enum) |
| EPS estimate | Yes | `eps_estimate` |
| revenue estimate | Yes | `revenue_estimate` |
| source/provider | Yes, but **only one real value exists today** | `source` (`EarningsSource` enum: `FINNHUB` only) |

**One migration is needed, and it's small and precedented**: `EarningsSource` needs a new member
(e.g. `EARNINGSAPI`). This isn't incidental scope creep — the enum's own docstring, written back in
Phase 4.2, says: *"kept as a real enum rather than a bare string so a second calendar provider added
later is a one-line addition with a real migration, not a silent convention."* This is exactly that
anticipated addition. The real Postgres mechanics (`ALTER TYPE earnings_source ADD VALUE ...` inside
an `autocommit_block()`) already have a direct precedent in this codebase —
`796745399657_add_option_reconstruction_fields_and_.py` did the same thing for a different enum. No
other schema change is needed: every other required field already exists.

---

## 2. EarningsAPI.com — what I could confirm from public docs, and what I honestly could not

I have no API key for this service and could not make a single live authenticated call — everything
below is from `earningsapi.com/docs` directly, not verified against a real response the way this
project's own convention prefers (see e.g. `docs/ibkr_integration.md`'s "Real verification" section,
or this session's own live Nasdaq-endpoint testing a few messages ago). **A live verification pass
against your real key needs to happen before this is trusted for real syncs** — flagged plainly as
Open Question 1, not assumed away.

### 2.1 What the docs say

- Base URL: `https://api.earningsapi.com/v1`. Auth: `apikey` query parameter (not a header).
- `GET /v1/calendar/earnings?date=YYYY-MM-DD&apikey=...` — **one calendar date per call, not a
  range** (same real constraint as Finnhub's own `/calendar/earnings`, and the same constraint I
  found live-testing Nasdaq's unofficial endpoint before this message arrived — every earnings
  calendar source I've looked at recently shares this shape). Response:
  `{"date": "...", "pre": [...], "after": [...], "notSupplied": [...]}` — timing is which array an
  entry is in, not a field on the entry. Each entry: `symbol`, `name`, `epsEstimate`, `eps` (actual,
  unused by this sync), `revenue` (actual, unused), `revenueEstimate`. **Revenue estimate is
  genuinely present** — a real improvement over the unofficial Nasdaq endpoint I'd been evaluating
  before your message, which has no revenue field at all.
- `GET /v1/profile/{symbol}` — `symbol`, `companyName`, `exchange`, `country` (a full name like
  `"United States"`, **not** the ISO-2 `"US"` this codebase's eligibility check currently compares
  against — `services/earnings_eligibility.py::US_COUNTRY_CODE = "US"` does
  `event.country.upper() != US_COUNTRY_CODE`; a real, small mapping step is needed, not a
  fabrication risk, but worth getting right and verifying live), `marketCap` (a real number, not a
  formatted string), `sector`, `industry`, `cik`, `type`, `tags`.
- Free plan: 60 req/min, 100 req/day, 1,000 req/month; **the docs do not state a forward-looking
  date-range limit** the way Finnhub's does. This is the single most important unknown for this
  whole review (§7, Open Question 2) — I cannot promise the free tier won't have the same kind of
  undocumented forward-range restriction that caused this problem in the first place. It needs to be
  confirmed empirically once you have a key, not assumed better just because it's a different vendor.

### 2.2 Rate budget, given the per-day-not-range constraint

Syncing `SYNC_HORIZON_DAYS` (365) forward = 365 real calls just for the calendar (before any profile
calls) if done naively every run. At 100 req/day free-tier, **a full 365-day sync cannot complete in
one day on the free plan** — this needs a real design decision, not a naive port of the Finnhub
range-call pattern. See §7, Open Question 3.

---

## 3. Proposed architecture

```
services/scheduler.py::run_earnings_calendar_sync_job
    |
    v
providers/factory.py::build_earnings_calendar_provider
    |
    v
providers/fallback.py::EarningsCalendarProviderChain   (NEW -- mirrors the existing
    |                                                     MarketDataProviderChain /
    |-- EarningsApiCalendarProvider   (NEW, primary)      OptionsProviderChain shape exactly,
    |-- FinnhubEarningsCalendarProvider   (existing,       see §4.2)
        unchanged, fallback)
    |
    v
services/earnings_calendar_sync.py::sync_earnings_calendar   (small, additive changes -- §5.3)
    |
    v
earnings_calendar_event table   (one new enum member, §1.3 -- no other schema change)
    |
    v
GET /earnings-calendar, /by-month, /{symbol}   (unchanged)
    |
    v
Dashboard card list + EarningsCalendarGrid   (unchanged for data; two additive display items, §6)
```

---

## 4. Provider integration — reusing what already exists, not duplicating it

### 4.1 The interface already exists — I'm not creating a second one

Your requirement 1 sketches a new `EarningsCalendarProvider` with an `async def fetch_earnings(...)`
method. **`providers/base.py::EarningsCalendarProvider` already exists**, with
`get_earnings_calendar(from_date, to_date) -> list[FinnhubCalendarEntry]` — structurally the same
question, already implemented by `FinnhubEarningsCalendarProvider`, already the exact interface
`sync_earnings_calendar`/the scheduler job/the admin endpoint all depend on. I'm implementing
`EarningsApiCalendarProvider` against **this existing ABC**, not building a parallel one — a second,
differently-named interface answering the same question would be real duplication this project's own
conventions consistently avoid (see e.g. `providers/base.py`'s own docstrings on why each interface
exists exactly once).

**One deliberate deviation from your literal sketch, flagged explicitly**: `def`, not `async def`.
Every provider adapter in this entire codebase (Finnhub, Tiingo, Alpha Vantage, IBKR, SEC EDGAR) is
synchronous `httpx.Client` + `tenacity` retry — there is no async provider anywhere, and the
scheduler/FastAPI routes that call these are themselves sync-in-a-threadpool, not an asyncio call
chain. Introducing one async provider here would be a real, novel architectural seam (sync callers
would need `asyncio.run()` or similar just to call it) for no functional benefit — EarningsAPI.com's
API is a plain REST call, exactly as easy to call synchronously as Finnhub's. I'm matching the
existing, ubiquitous convention instead. If you specifically want this provider to be the first async
one in the codebase for a reason I'm not seeing, say so and I'll reconsider.

### 4.2 The fallback chain already exists as a pattern — extending it, not inventing a new shape

`providers/fallback.py` already has `MarketDataProviderChain` and `OptionsProviderChain`: try
providers in order, fall through to the next on **any exception**, track
`last_requested_provider`/`last_actual_provider`/`last_fallback_reason` for observability, raise
`AllProvidersFailedError` only if every provider fails. I'll add `EarningsCalendarProviderChain`
following this exact shape — this is also exactly your requirement 3's fallback logic ("API
unavailable, rate limited, invalid response" → Finnhub): a broad `except Exception` already covers
all three (an invalid/malformed response is handled by having `EarningsApiCalendarProvider` itself
raise a clear error when the response doesn't parse, the same way `FinnhubEarningsCalendarProvider`
already does for its own malformed-response case).

`providers/factory.py::build_earnings_calendar_provider` changes from "build Finnhub, or None" to
"build the chain: EarningsAPI (if `EARNINGSAPI_API_KEY` configured) primary, Finnhub (if
`FINNHUB_API_KEY` configured) fallback" — mirroring `build_options_provider_chain`'s own existing
"skip whichever isn't configured, single-provider chain if only one is available, `None` if
neither" logic exactly. **The scheduler job's own code does not need to change at all** — it already
just calls `build_earnings_calendar_provider(...)` and uses whatever comes back.

### 4.3 `get_company_profile` fallback semantics

Matching `OptionsProviderChain.get_underlying_quote`'s own precedent: both an exception **and** a
`None` result fall through to the next provider (a `None` profile is a real, valid "nothing known"
answer per-provider, but the *other* provider might know), so the chain only returns `None` if every
provider genuinely has nothing. This maximizes real data availability rather than giving up the
moment the primary provider has a gap for one symbol.

---

## 5. Reliability and honest-data details

### 5.1 What "no fake/demo data" concretely means here, confirmed against real code paths

This is already this codebase's default behavior, not something I need to add: `sync_earnings_
calendar` only ever writes rows for entries a real provider actually returned (§1.2); a provider
failure on both primary and fallback raises `AllProvidersFailedError`, which the scheduler job's own
existing `except Exception: db.rollback(); log.error(...)` already handles — **zero rows written**,
not a fabricated placeholder row. I'm not adding new fake-data guards because none are needed; I'm
confirming the existing ones already cover this path once the chain exists.

### 5.2 The per-day-call constraint (§2.2) needs a real scheduling decision

Given EarningsAPI.com's calendar endpoint is one-date-per-call and the free tier's request budget
can't cover a naive 365-day sweep in one run, I see three real options, not mutually exclusive:

1. **Narrow `SYNC_HORIZON_DAYS` for the daily job** (e.g. 30–45 days forward, matching what the
   dashboard actually needs "real, near-term" data for) instead of 365. Directly fixes the original
   complaint (nonsensical far-future dates) since a narrower window can't return them.
2. **Spread the sweep across multiple days** — sync only a rolling slice of the full window per run
   (e.g. today+180 one day, next 180-day slice the next), converging over about a week to full
   coverage, staying inside the daily request budget indefinitely.
3. **Rely on the fallback for the wide historical/far-forward backfill** (Finnhub's own range-based
   call, one HTTP request for the whole span) and use EarningsAPI.com only for the **near-term**
   window where per-day granularity and revenue-estimate richness matter most.

My recommendation is **option 1** (narrow the horizon) as the default, since it most directly targets
the actual bug you reported, is the simplest change, and stays well inside the free-tier budget with
margin for profile calls and retries — see Open Question 4 for the exact number to use.

### 5.3 `sync_earnings_calendar` changes (small, additive)

Two real code paths need a one-line addition each: on row **create**, set `source=EarningsSource(
entry.source_provider)` instead of relying on the column default; on row **update**, add a check that
updates `existing.source` (and counts it as a change) when the entry's provider differs from what's
stored — so if EarningsAPI.com later successfully re-syncs a symbol Finnhub had filled in during an
earlier fallback, the row's recorded provenance stays accurate. `FinnhubCalendarEntry` (reused
as-is for both providers, see §5.4) already carries `source_provider` per-entry via
`ProvenancedModel`, so this is a real, already-available signal, not new plumbing.

### 5.4 Type reuse — `FinnhubCalendarEntry`/`FinnhubCompanyProfile`, naming caveat flagged plainly

`providers/types.py::FinnhubCalendarEntry`/`FinnhubCompanyProfile` are structurally generic (symbol,
earnings_date, session, eps/revenue estimate, source_provider, retrieved_at) despite the
Finnhub-specific name — they're what `EarningsCalendarProvider`'s own ABC methods are typed to
return, and `EarningsApiCalendarProvider` will return real instances of the same types with
`source_provider="earningsapi"`. I'm not renaming these types as part of this change: a rename
touches every existing caller for a naming-purity win with real regression risk, for something that
already works correctly today. Worth a dedicated, low-risk cleanup later; not blocking this.

---

## 6. System status and dashboard — reusing the existing Data Provider Control Center

`services/provider_status.py` already has exactly the shape requirement 5 asks for (active
provider, last successful sync, last error) — it's the same `providers.domains` block already
powering `GET /system-status` and the Settings → Data Providers page for every other domain
(price_history, options, etc.), rendered by a **fully generic** `domains.map(...)` on the frontend
(confirmed — no hardcoded domain list to extend). Adding `"earnings_calendar"` to `DOMAIN_PROVIDERS`
with `("earningsapi", "finnhub")` gets you the whole requirement — active provider, last successful
sync timestamp, last error — with **zero new frontend code**, reusing the existing settings page.

`last_success_at` for this domain will be derived the same way every other domain already is (not
from a log claiming success, from real ingested data — see `_last_success_at`'s own docstring):
`max(earnings_calendar_event.updated_at) WHERE source = <provider>`. `TimestampMixin` already gives
every row a real `updated_at`; no new column needed.

Your requirement 6's two dashboard asks — "show the real data source" and "show last successful sync
time" — map directly onto this same data, surfaced on the calendar itself (not just the Settings
page): a small line under `EarningsCalendarGrid`'s header reading real values from `GET
/system-status`'s `providers.domains` entry for `earnings_calendar`. No redesign, no new endpoint,
additive only, per your explicit "keep existing dashboard" instruction.

---

## 7. Summary — open questions requiring your input before I write any code

1. **I have no EarningsAPI.com API key and could not live-verify anything in §2** — the response
   shapes, the country-name-vs-ISO-code mapping, and (most importantly) whether the free tier has
   its own undocumented forward-range restriction are all documentation-sourced, not confirmed live.
   Recommendation: get a free-tier key, and I run one real, live verification call (mirroring how
   every other provider integration in this codebase was verified — see docs/ibkr_integration.md's
   own "Real verification" section as the precedent) before this is trusted for the real scheduled
   sync. Confirm this is acceptable, or tell me if a key already exists somewhere.
2. **Does EarningsAPI.com's free tier actually give meaningfully better forward coverage than
   Finnhub's?** This is the whole premise of the switch and I cannot confirm it from public docs
   alone. If it turns out to have a similar undocumented cutoff, the fix might need to be narrowing
   `SYNC_HORIZON_DAYS` regardless of which provider is primary (§5.2 option 1 would still apply).
3. **How should the per-day-call / rate-budget mismatch (§2.2, §5.2) be resolved?** My
   recommendation is narrowing `SYNC_HORIZON_DAYS`, but confirm the exact target (30 days? 45? 60?)
   or pick one of the other two options instead.
4. **`EARNINGSAPI_API_KEY` config** — new `Settings` field, `.env`/`.env.example` entry, and (matching
   this project's existing `services/secret_store/` pattern for other provider keys) optionally
   settable through the Settings UI too, or `.env`-only for v1 like IBKR's own credentials are today?
   Recommendation: `.env`-only for v1, matching Finnhub's own current setup exactly — no new UI
   surface needed to ship this.
5. **New `EarningsSource` enum member naming** — `EARNINGSAPI` (matching `FINNHUB`'s all-caps
   convention) with value `"earningsapi"`, or a different label you'd prefer?

Do not start coding until these are resolved.

---

## 8. Implementation notes — what actually shipped, and what live testing corrected

### 8.1 Open questions §7, resolved

1. **Live key provided, live-verified.** Real `curl`/`httpx` calls against the real key (never
   echoed in any tool output) confirmed the documented shapes in §2 field-for-field, with two
   corrections noted in §8.2 below. No `User-Agent` header is required (unlike the unofficial Nasdaq
   endpoint evaluated earlier, which genuinely needed one).
2. **Confirmed better near-term coverage than Finnhub's.** Searching the real 14-day window found
   real near-term reports for your own named targets — **NVDA on 2026-08-26** (4 days out at
   verification time) and **AVGO on 2026-09-02** (11 days out) — not far-future placeholders. No
   forward-range cutoff was observed inside the tested window; this isn't an exhaustive guarantee for
   every possible future date, but it directly satisfies the review's original concern.
3. **Resolved by your follow-up message, not by my own guess between 30/45/60**: `SYNC_HORIZON_DAYS`
   is now **14**, matching your explicit "the useful window is 7–14 days before earnings" reasoning.
   The rate-budget problem (§2.2, §5.2) ended up needing a fourth mechanism beyond the three options
   listed there: a **per-date dedup** in `sync_earnings_calendar` (`_ranges_needing_fetch`) that skips
   any date already covered by an existing `earnings_calendar_event` row (any source), fetching only
   the missing dates as contiguous ranges. In steady state this is exactly the one new day entering
   the rolling window each run — real usage is roughly 1–3 requests/day, comfortably under the
   100/day free-tier limit, without needing a second sweep-scheduling mechanism or a new tracking
   table. A 0-event date (mostly weekends) has no row and gets re-fetched on each subsequent run until
   it ages out of the 14-day window — a small, self-limiting inefficiency, not a correctness gap.
4. **`.env`-only, as recommended** — `EARNINGSAPI_API_KEY`, resolved via
   `services/secret_store/resolve_secret(settings, "earningsapi", db)`, matching Finnhub's own
   precedent exactly. Not added to `CREDENTIAL_PROVIDERS` (no Settings-UI credential form this round).
5. **`EARNINGSAPI`, as recommended** — `models/enums.py::EarningsSource.EARNINGSAPI = "earningsapi"`.

### 8.2 Two things live testing corrected versus the docs-only draft

- **`country` on `/v1/profile/{symbol}` is a full display name** ("United States"), exactly as §2.1
  anticipated as a real possibility — confirmed live, not assumed. `providers/earningsapi.py::
  _normalize_country` maps it to the ISO-2 `"US"` `services/earnings_eligibility.py` expects.
- **A real bug was found and fixed during implementation, not anticipated by this review**:
  `services/secret_store/environment_store.py::_ENV_ATTR` — the dict mapping a provider name to its
  `Settings` field — had an entry for `"finnhub"` but none for `"earningsapi"`. Without it,
  `resolve_secret(settings, "earningsapi", db)` would have silently returned `None` even with a real
  key configured, making `providers/factory.py::build_earnings_calendar_provider` treat EarningsAPI.com
  as unconfigured and fall straight to Finnhub — a silent primary/fallback swap, not a crash, which is
  exactly the kind of bug that's hardest to notice. Caught by a real end-to-end check (`resolve_secret`
  + `build_earnings_calendar_provider` run against the real `.env`, output limited to booleans/masked
  values, never the key itself) before any real sync ran.

### 8.3 One deliberate deviation from §4.2's own plan

`providers/factory.py::build_earnings_calendar_provider` does **not** unwrap a single configured
provider the way `build_options_provider_chain`/`build_market_data_chain` do — it always returns an
`EarningsCalendarProviderChain`, even with only one provider configured. This is so
`last_actual_provider`/`last_requested_provider` are always real, populated attributes the scheduler
job can log from (Step 6.4's "log provider used" requirement), rather than only when a fallback
happens to exist. A single-provider chain behaves identically to that provider called directly — this
only changes what's observable about which provider served a given sync, never what real data comes
back.

### 8.4 One honest, acknowledged gap

Step 3's "exclude ETFs / OTC" filtering is **not fully enforced** by any new code — doing so robustly
would mean editing `services/earnings_eligibility.py`, which is on this task's explicit "Do NOT
change" list. The existing $10B market-cap + US-listed + real-tradable-option-chain gate covers most
practical cases (penny stocks and most OTC names fail on market cap or lack a real IBKR option chain),
but a large-AUM ETF with a real option chain (e.g. SPY) could theoretically still pass. Flagged here
rather than silently fixed by touching a forbidden file, and rather than silently left unmentioned.
