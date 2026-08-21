# Phase 4.8A Architecture Review — IBKR Runtime Automation

Status: **draft — open questions below require explicit confirmation before any code is written.**

Branch: `feature/ai-earnings-forward-test`.

Scope of this document: designs a production runtime that lets Earnings Decision Lab pull real
IBKR market data 24/7 without a human re-logging in to the Gateway every few hours. This is a
**design review only** — no code, Dockerfile, or compose file in this repository is modified by
this document. It covers Docker/container architecture, authentication automation, provider
integration, reliability, scheduler integration, and secrets management, and ends with the open
questions that must be resolved (several of which are genuine forks with materially different
implementation costs) before Phase 4.8B (implementation) can start.

---

## 0. Pre-flight investigation — what this review is grounded in

Read in full before writing this document: `providers/ibkr_client.py`, `providers/ibkr_options.py`,
`providers/ibkr_portfolio.py`, `providers/base.py` (`OptionsDataProvider`), `providers/factory.py`,
`services/benchmark_entry_capture.py`, `services/benchmark_exit_capture.py` (the module the Phase
4.8A brief refers to as "settlement_capture.py" — see §0.1), `services/scheduler.py`,
`services/system_status.py`, `services/secret_store/` (all five files), `core/config.py`,
`docker-compose.yml`, `backend/Dockerfile`, `.env.example`, `api/main.py`'s `lifespan()`,
`api/routers/health.py`, `frontend/src/pages/Settings/Ibkr.tsx`, and `docs/ibkr_integration.md`
(the Phase 13 design doc for the *existing* integration this phase builds on top of).

### 0.1 Two naming corrections, confirmed against the actual code

- **There is no `services/settlement_capture.py`.** The module that captures real option exit
  quotes is `services/benchmark_exit_capture.py` (`capture_benchmark_exit()`), a Phase 4.5
  deliverable. This review reads and cites that file.
- **"IB Gateway + IBC" and this project's existing IBKR integration are not the same product.**
  This is not a naming nit — it is the single fork this whole review turns on. See §2.

### 0.2 What already exists, confirmed by reading the code (not assumed)

- The integration talks to IBKR's **Client Portal (Web) API Gateway** — a local Java process, REST
  over HTTPS, port 5001, endpoints under `/iserver/*` and `/portfolio/*`. Confirmed by every call
  site in `ibkr_client.py`/`ibkr_options.py`/`ibkr_portfolio.py`.
- **Read-only, enforced by construction**: `IBKRClient.get()` is the only HTTP verb method that
  exists on the class — there is no `post`/`put`/`delete`. No order-placement endpoint is called
  anywhere (`docs/ibkr_integration.md`, confirmed by grep).
- **Authentication today is 100% manual and out-of-band**: the user opens
  `https://localhost:5001` in a browser and logs in (username, password, 2FA) themselves. The
  backend never sees any of that — it only ever calls `GET /iserver/auth/status` and reacts to
  `{authenticated, connected, competing}` (`IBKRClient.auth_status()` /
  `ensure_authenticated()`). This is stated as a hard guarantee in two places that this phase's
  goal directly contradicts — see the callout in §2.4.
- **`OPTIONS_PROVIDER=ibkr` is "always configured"**: `providers/factory.py::_build_options_provider`
  constructs an `IBKROptionsProvider` unconditionally when selected — unlike Alpha Vantage, no API
  key is checked at construction time, because auth happens per-call against the Gateway, not at
  provider-build time. This matters for §6.
- **Capture failure handling already degrades gracefully.** Both
  `services/benchmark_entry_capture.py::capture_benchmark_entry` and
  `services/benchmark_exit_capture.py::capture_benchmark_exit` wrap every provider call in a broad
  `try/except Exception`, turning a Gateway/auth failure into one honest, immutable
  `CaptureStatus.FAILED` row (with `capture_error` set to the exception text) — never a crash,
  never a partial write. This is a real strength this phase's design should preserve and build on,
  not work around (see §4, §5).
- **The scheduler already runs inside the FastAPI process**, not a separate worker: `AsyncIOScheduler`
  built in `services/scheduler.py::build_scheduler()`, started from `api/main.py`'s `lifespan()`,
  backed by `SQLAlchemyJobStore` against the same Postgres instance so job registration survives a
  container restart. Three cron jobs exist today, none of them touch IBKR outside their own narrow
  window:
  - `earnings_calendar_sync` — daily 00:00 UTC (Finnhub only, never IBKR).
  - `decision_and_entry_capture` — daily 15:55 America/New_York.
  - `exit_capture` — daily 15:55 America/New_York (separate job, same trigger time, deliberately
    kept independent per the Phase 4.5 review's own decision — see that job's docstring).
  Both capture jobs call `providers.factory.build_options_provider_chain(settings, db=db)` fresh,
  every run — no cached provider instance, no long-lived connection. **This means the IBKR session
  is exercised for a few seconds, once or twice a day, at a fixed wall-clock time** — a detail with
  real consequences for session-expiry design (§3.4) and reliability (§4).
- **`docker-compose.yml`'s existing services**: `db` (healthchecked), `migrate` (one-shot,
  `depends_on: db: condition: service_healthy`, exits), `backend` (`depends_on: migrate: condition:
  service_completed_successfully`, its own `/api/v1/health` healthcheck, `restart: unless-stopped`,
  `extra_hosts: host.docker.internal:host-gateway` specifically so it can reach a Gateway running on
  the *host* machine), `frontend`. No service today runs anything IBKR-related inside Docker — the
  Gateway is assumed to be a process on the operator's own machine, outside the compose stack
  entirely.
- **Secrets today**: `services/secret_store/` is a real, working, encrypted-at-rest credential
  store (`LocalEncryptedSecretStore`, Fernet, `SECRET_STORE_MASTER_KEY`, `provider_credential`
  table) already used for Alpha Vantage/Tiingo/Finnhub/LLM API keys, with env-var config
  (`EnvironmentSecretStore`) as the always-working fallback. **IBKR participates in neither** —
  `ibkr_base_url` is a plain, non-secret `Settings` field, because this project has never held an
  IBKR credential of any kind. That is the exact policy this phase is being asked to change (§2.4,
  §6).
- **No alerting/notification integration exists anywhere in this codebase** — confirmed by
  searching for Slack, SMTP, SendGrid, PagerDuty, and generic "alert" across the repository: zero
  matches outside this document. Monitoring in this project today means one live-polled status page
  (`services/system_status.py::get_ibkr_status`, rendered by
  `frontend/src/pages/Settings/Ibkr.tsx`) that a human has to open. This bounds what §4's monitoring
  proposal can responsibly assume is in place already.

---

## 1. Current architecture (as it exists today)

```
Operator's own machine (outside Docker, outside this repo)
    Browser -> https://localhost:5001 -> IBKR Client Portal Gateway (Java process)
                                          self-authenticated by the operator: username,
                                          password, 2FA -- entirely outside this codebase

docker-compose network
    backend container
        IBKRClient          -- GET-only REST wrapper, verify=False (scoped to itself),
                                base_url = IBKR_BASE_URL
        IBKROptionsProvider  -- implements OptionsDataProvider
        IBKRPortfolioProvider -- read-only positions
              |
              | https://host.docker.internal:5001/v1/api  (extra_hosts: host-gateway)
              v
        [reaches the Gateway running on the HOST machine, outside Docker]
```

Every real request first calls `ensure_authenticated()`. If the operator hasn't logged in via the
browser recently enough, or the CP Gateway's own session has expired, every IBKR call fails with
`IBKRNotAuthenticatedError` — which today simply means "re-open the browser tab and log in again."
That manual step, repeated every few hours, is exactly the problem this phase exists to remove.

---

## 2. The fork this whole design turns on: which "Gateway"?

The Phase 4.8A brief asks for **"IB Gateway + IBC integration."** Read literally and precisely,
that names a specific, well-known pairing in the IBKR API ecosystem — and it is **not** what this
codebase talks to today.

### 2.1 Two different IBKR products, not two names for one thing

| | **Client Portal (Web) API Gateway** — what this project uses today | **Classic "IB Gateway"** — what "IB Gateway + IBC" literally names |
|---|---|---|
| Protocol | REST/JSON over HTTPS | TWS socket API (binary, stateful) |
| Port | 5001 (default) | 4001 (live) / 4002 (paper) |
| This project's client | `httpx` calls to `/iserver/*`, `/portfolio/*` — everything in `ibkr_client.py`/`ibkr_options.py`/`ibkr_portfolio.py` | Would require `ibapi` or `ib_async`/`ib_insync`, a different Python dependency, a different connection model entirely |
| Automated-login tooling | Third-party: **IBeam** (`voyz/ibeam`) — purpose-built for exactly this Gateway | **IBC** (`IbcAlpha/IBC`) — purpose-built for exactly *this* Gateway, commonly packaged as `gnzsnz/ib-gateway-docker` |

These are not interchangeable. IBC does not automate the Client Portal Gateway; IBeam does not
automate classic IB Gateway. Picking one over the other is not a config change — it determines
whether §3 (provider integration) is a **zero-line-of-business-logic change** or a **full
provider-layer rewrite**.

### 2.2 What switching to classic IB Gateway would actually cost

Every method on `IBKROptionsProvider`/`IBKRPortfolioProvider` — `_resolve_underlying`,
`_strikes_near_atm`, `_expirations_for_strike`, `_fetch_snapshots`, `get_positions`, the whole
priming-delay/field-code logic documented in `docs/ibkr_integration.md` and confirmed live during
Phase 13 — is written against REST endpoints that **do not exist** on classic IB Gateway. Moving to
it means re-implementing the entire discovery/quote flow against `ib_async`'s
`reqSecDefOptParams`/`reqMktData`/`reqHistoricalData`/`reqPositions` equivalents: a new client, new
tests, new field-mapping, and a second live-verification pass exactly like Phase 13's own (real
account, real symbol, hand-checked values) — not a Phase 4.8A-sized task, and in direct tension with
the brief's own requirement 3 ("how no code duplication should happen").

### 2.3 Recommendation

**Keep the Client Portal Gateway. Automate its login with IBeam, not IBC.** This satisfies
requirement 3 for free: `IBKR_BASE_URL` changes from `https://host.docker.internal:5001/v1/api` to
the new gateway container's compose DNS name, and nothing else in `backend/src` needs to change —
`IBKRClient`, `IBKROptionsProvider`, `IBKRPortfolioProvider`, `providers/factory.py`, and both
capture services are untouched. IBeam is a real, actively maintained, open-source project
(`github.com/Voyz/ibeam`) built specifically to run the Client Portal Gateway unattended in Docker
— which is exactly this phase's stated goal, just not the exact tool name the brief used.

This is flagged as an explicit, load-bearing deviation from the brief's literal wording, the same
way this session has flagged literal-instruction deviations before (see the
`/earnings-calendar/:symbol` vs. `/earnings/:symbol` precedent from the dashboard phase) — **not**
silently substituted. If the user specifically wants classic IB Gateway (e.g. for a market-data
entitlement or streaming capability the Client Portal Gateway doesn't offer), that's a materially
larger, separate migration project and should be scoped as its own phase, not folded into this one.
See Open Question 1.

The remainder of this document (§3 onward) designs the recommended path — **IBeam + the existing
Client Portal Gateway integration**. Where classic IB Gateway would meaningfully change a section's
answer, that's called out inline.

### 2.4 A policy this phase directly reverses — stated plainly, not glossed over

Two places in this codebase currently assert, as a design guarantee, the opposite of this phase's
goal:

- `docs/ibkr_integration.md`: *"This project never sees an IBKR username, password, or 2FA code,
  and never talks to IBKR's cloud directly."*
- `frontend/src/pages/Settings/Ibkr.tsx`: *"This project never automates IBKR login — no username,
  password, or 2FA code is ever entered here."*

Under the proposed design, the **application code** (`backend/src`) still never sees these — see
§6.4 for why that boundary survives intact. But **the project as a whole** (its Docker Compose
stack, its `.env` file) would, for the first time, hold real IBKR login credentials in a new,
purpose-built automation container. That is a real, meaningful expansion of what this project
holds, even though it's cleanly isolated to one new component rather than smeared across the
existing, already-audited codebase. Both documents above will need honest rewrites once this ships
(§8.2) — leaving them as-is while shipping this feature would make them false.

---

## 3. Authentication strategy

### 3.1 What IBeam actually automates

IBeam runs the same Client Portal Gateway Java process this project already talks to, inside its
own container, and drives its login page programmatically (username + password) instead of a human
clicking through a browser. It re-authenticates on a schedule and exposes its own health/status
surface in addition to the underlying Gateway's `/v1/api/iserver/auth/status` (the same endpoint
`IBKRClient.auth_status()` already polls).

### 3.2 2FA — the real limitation, not glossed over

IBKR's standard second factor for a fresh Client Portal login is an **IBKR Mobile push
notification** the account holder taps to approve (SMS/security-card fallbacks exist in some
regions/account configurations). Nothing in this codebase, and nothing IBeam itself does, can tap
that phone notification unattended by design — that's the point of the control.

What actually determines whether 24/7 unattended operation is achievable is **account-level
configuration this review has not verified against the user's real account and cannot assume**:

- IBKR's Account Management offers settings that can reduce or exempt second-factor prompts for
  designated trusted devices/sessions in some configurations. Whether the user's account is
  eligible, and what exactly it requires, is not something this document can confirm from the
  codebase — it must be checked directly in IBKR Account Management. This is the single biggest
  open question in this whole design (Open Question 2) and should be resolved **before** any
  container is built, because it determines whether full automation is achievable at all versus
  "automation that still needs a daily phone tap."
- Even in the worst case (2FA required on every fresh login), IBKR sessions are documented to
  remain valid without a fresh login for up to ~24 hours once established, provided the session is
  kept alive (§3.4). If that ceiling holds for this account, unattended operation only needs to
  survive **one** login event per day, not "every few hours" — which suggests the pain point
  described in the brief may actually be a session going idle faster than IBKR's documented ceiling
  (because today nothing calls the Gateway except two ~15:55 ET cron fires — see §3.4), not a
  fundamentally shorter forced-relogin cadence. Worth diagnosing empirically (Open Question 2)
  before assuming full login automation is the only fix.

### 3.3 Trusted session handling

The Client Portal login flow supports persisting a trusted-session token/cookie so a subsequent
login from the same device doesn't require a fresh 2FA challenge for some window. IBeam supports
persisting its runtime/session state across container restarts via a mounted volume — recommended
here specifically to avoid forcing a full (possibly 2FA-gated) re-login every time the container
restarts, as opposed to only when the underlying IBKR session actually expires.

### 3.4 Session expiration handling — a real gap in today's cadence

IBKR documents that a Client Portal session needs a low-cost "keep-alive" touch (any authenticated
call, or the dedicated `/tickle` endpoint) at least every few minutes to avoid going idle, separate
from the ~24-hour hard re-authentication ceiling. **Today, nothing in this codebase calls the
Gateway between the two daily 15:55 ET cron fires** (§0.2) — meaning even a perfectly automated
login is not sufficient on its own; the session can go idle between scheduled runs regardless of
which Gateway automation tool sits underneath. §5.3 proposes a new, small keep-alive scheduler job
to close this gap — a genuinely new piece of backend behavior, not something IBeam alone solves.

---

## 4. Provider integration — how the existing code connects, and why nothing duplicates

### 4.1 The interface was already built for this

`providers/base.py::OptionsDataProvider` is an abstract interface; `providers/factory.py` is the
only place that knows which concrete provider is selected. Neither `services/benchmark_entry_
capture.py` nor `services/benchmark_exit_capture.py` nor `services/scheduler.py` imports
`IBKROptionsProvider` directly — they all go through `build_options_provider_chain(settings,
db=db)`. Under the recommended design (§2.3), the only thing that changes is **one configuration
value**: `IBKR_BASE_URL` moves from `https://host.docker.internal:5001/v1/api` (a Gateway on the
operator's own machine) to the new gateway container's service DNS name inside the compose network
(e.g. `https://ibkr-gateway:5000/v1/api` — exact port depends on IBeam's own default, confirmed
during implementation, not guessed here).

### 4.2 What stays byte-for-byte unchanged

`providers/ibkr_client.py`, `providers/ibkr_options.py`, `providers/ibkr_portfolio.py`,
`providers/factory.py`, `services/benchmark_entry_capture.py`,
`services/benchmark_exit_capture.py`, `services/scheduler.py`. The TLS-verify-disabled scoping in
`IBKRClient.__init__` stays correct and necessary unchanged too — IBeam's internal Gateway serves
the same kind of local self-signed certificate the manual one does.

### 4.3 What genuinely is new

Nothing in the provider layer. The only new backend-adjacent code is the optional keep-alive job
(§5.3) and the optional status-page extension (§4.4/§8.2) — both additive, neither touches how
capture services request data.

### 4.4 Preserving the manual/local workflow

Nothing about this design should force every deployment onto the automated container. `IBKR_BASE_
URL` is already an env var read fresh from `Settings` (`core/config.py`) — an operator running the
backend locally against their own browser-authenticated Gateway keeps working exactly as before,
by simply not setting the new gateway service's URL. See Open Question 5.

---

## 5. Reliability

### 5.1 What already works, and should not be rebuilt

Both capture services already turn any provider exception — Gateway unreachable, not authenticated,
competing session, rate-limited, whatever — into one honest `CaptureStatus.FAILED` row with a
readable `capture_error`, inside a `try/except Exception` at the scheduler-job level too
(`run_decision_and_entry_capture_job`/`run_exit_capture_job` both catch, log, `db.rollback()`,
and continue to the next event). A gateway that's mid-restart or mid-reauth exactly when a capture
job fires already fails safely today, no new code required for that baseline.

### 5.2 The real gap: one shot, no retry, at a single fixed time

Both capture jobs run once, at 15:55 ET, with no retry inside that run beyond whatever a single
provider call attempt gives. If the automated gateway happens to be re-authenticating (IBeam's own
periodic re-validation, or a crash-restart) in that exact narrow window, every eligible event that
day gets one FAILED attempt and nothing tries again until tomorrow's cron fire — a pre-existing
design property, not something this phase introduces, but one that automation makes more likely to
actually bite (a human-driven Gateway rarely restarts itself mid-afternoon; an automated one, doing
periodic re-auth for reliability, might). Recommended hardening, scoped for implementation (not
built here): a small bounded retry (e.g. 2 attempts, a few minutes apart) inside the capture job
before accepting the FAILED outcome — see Open Question 4 on whether this is worth the added
complexity for a personal-scale system.

### 5.3 Gateway crash recovery and health checks

- **Container-level**: `restart: unless-stopped` on the new gateway service, matching every other
  long-running service already in `docker-compose.yml` (`db`, `backend`, `frontend`) — if IBeam's
  process dies (e.g. an unrecoverable login failure), Docker restarts the container and IBeam
  re-attempts login from a clean state. A real `healthcheck:` block on the new service (mirroring
  the existing pattern for `backend`/`frontend`), polling the Gateway's own
  `/v1/api/iserver/auth/status` or IBeam's own status endpoint — whichever proves more reliable
  during implementation. An unhealthy container is visible via `docker compose ps`/`docker inspect`
  the same way every other service's health already is.
- **Application-level, new**: a small scheduler job (e.g. `run_ibkr_gateway_healthcheck_job`),
  registered the same way the three existing jobs are, on a short interval (every 5–10 minutes, all
  day — not just the 15:55 ET window), doing nothing more than `IBKRClient(base_url=...)
  .auth_status()` and logging the result. This closes the session-idle gap from §3.4 (the call
  itself is a keep-alive touch) and gives an early, log-visible signal — "gateway has been
  unauthenticated for the last N checks" — well before the next actual capture window, rather than
  discovering it only after a FAILED capture attempt at 15:55 ET.

### 5.4 Monitoring — scoped to what this project actually has

No alerting integration exists anywhere in this codebase (§0.2). Recommended v1 scope: extend
`services/system_status.py::IbkrStatus` (and the `Ibkr.tsx` page that already renders it) with a
"last known authenticated at" timestamp sourced from the new keep-alive job, so a human glancing at
the existing Settings → Interactive Brokers page gets a real freshness signal instead of only a
live-right-now check. Proactive notification (email/Slack/push) is explicitly **not** proposed for
v1 — it would be new infrastructure this project has never needed before, disproportionate to a
personal-scale system, per the same reasoning `services/scheduler.py`'s own docstring gives for
rejecting Celery. See Open Question 6.

---

## 6. Scheduler integration

### 6.1 The direct answer: no scheduler code changes needed

`run_decision_and_entry_capture_job` and `run_exit_capture_job` already request IBKR data exactly
the way every other provider is requested — via `build_options_provider_chain(settings, db=db)`,
which reads `settings.options_provider`/`settings.ibkr_base_url` fresh on every call. Once
`IBKR_BASE_URL` points at the new automated gateway container, both jobs transparently get live data
from it with **zero changes to `services/scheduler.py`**. This is the concrete payoff of the
interface boundary already built in `providers/base.py` — confirmed by reading the actual call
sites, not assumed from the interface's design intent alone.

### 6.2 What is new

Exactly one addition: the keep-alive/health job from §5.3, registered in
`build_scheduler()` alongside the three existing jobs, following the same conventions already
established there (own `SessionLocal()`, defensive about an unconfigured provider, a fixed job id
with `replace_existing=True`).

---

## 7. Security

### 7.1 Secrets management

IBKR login credentials (username + password, and whatever 2FA-adjacent configuration the resolved
Open Question 2 requires) are meaningfully more sensitive than every other credential this project
currently stores — a compromise gets an attacker into the live brokerage account, not just a
read-only market-data API key. Two real options:

1. **Env-var-only** (`.env`, gitignored, `env_file:` in `docker-compose.yml`), exactly matching how
   `POSTGRES_PASSWORD` is handled today in the same file. Read only by the new gateway container —
   the Python backend never reads these variables at all (§7.3).
2. **Extend `services/secret_store/`** with a new IBKR credential type through
   `LocalEncryptedSecretStore`, matching the encrypted-at-rest pattern already built for Alpha
   Vantage/Tiingo/Finnhub/LLM keys.

**Recommendation: option 1 for v1.** The existing secret store's `provider_credential` schema and
Settings UI are built for a single opaque API-key string per provider, not a username+password
pair — extending it is a real, separable schema/UI change, not a natural fit to bolt on here. Since
the backend application code never needs to read these values at all (only the new, isolated
gateway container does), env-var-only is not a lesser security posture for the value that actually
matters (the running application's own attack surface never gains a new secret) — it just means the
credentials live in `.env`/the gateway container's environment rather than Postgres. Option 2 is a
reasonable future enhancement, not a blocker for this phase. See Open Question 3.

### 7.2 Environment variables and `.env.example`

New variables (exact names to be finalized during implementation, e.g. `IBKR_USERNAME`,
`IBKR_PASSWORD`) must be added to `.env.example` with empty/placeholder values only, matching every
existing entry in that file, and documented with the same kind of explanatory comment
`IBKR_BASE_URL`'s existing entry already has.

### 7.3 What must never happen — enforced by design, not just policy

- Credentials are never baked into a Docker image layer — only ever supplied at runtime via
  `environment:`/`env_file:`, matching how this project already builds `backend`'s image with zero
  secrets in any layer (`backend/Dockerfile`, confirmed — no `ARG`/`COPY` of anything credential-
  shaped).
  - Credentials are never written into `docker-compose.yml` itself, only referenced via `${VAR}`
  interpolation, matching every existing secret-shaped value in that file
  (`POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-change_me}`).
- `.env` is already gitignored (confirmed) — the new variables inherit that protection automatically
  since they live in the same file, not a new one.
- **The Python backend process (`backend/src`) never reads or receives the IBKR username/password
  at all** — it only ever reads `IBKR_BASE_URL`, exactly as it does today. This is the concrete
  reason §2.4's policy reversal is narrower than it first sounds: the credential now lives in one
  new, small, purpose-built container's environment, never in the already-audited application code
  path.
- A real, practical risk worth explicitly checking during implementation, not assumed away: tools
  in this class have a known failure mode of printing the password to container startup logs for
  debugging. This must be verified against IBeam's actual log output during implementation
  (`docker compose logs ibkr-gateway`) before this is considered done — not assumed safe because the
  tool is reputable.
- Existing outbound-HTTP logging (`observability/http_client.py`) logs method/path/status/duration
  only, never headers or bodies — already safe for the backend's own calls to the gateway container
  and requires no change.

---

## 8. Implementation plan

### 8.1 Docker changes

- New service, e.g. `ibkr-gateway`, image `voyz/ibeam` pinned to a specific tag/digest (matching
  this project's existing convention of pinned base images — `pgvector/pgvector:pg16`,
  `python:3.12-slim` — never `:latest` unpinned).
- `restart: unless-stopped`, a real `healthcheck:` block, `env_file: .env` for the new credential
  variables, and a named volume for session/runtime persistence (§3.3).
- No host port mapping required — `backend` reaches it over the compose network by service name,
  same as it already reaches `db`.
- `backend`'s `IBKR_BASE_URL` default changes (in `.env.example`) to point at the new service.
  `extra_hosts: host.docker.internal:host-gateway` stays, unchanged, to preserve the manual/local
  workflow (§4.4, Open Question 5).
- `backend`'s `depends_on` gains the new service, but **not** as a hard `condition:
  service_healthy` gate on backend startup — recommended soft/independent, matching the existing,
  already-correct posture that a not-yet-authenticated IBKR Gateway must never block the rest of the
  app (§0.2, §5.1). See Open Question 4.

### 8.2 Backend changes

- Zero changes to the provider layer or capture services (§4.2).
- New, small: the keep-alive scheduler job (§5.3, §6.2).
- New, small: `IbkrStatus`/`system_status.py` extended with a "last known authenticated at" field
  (§5.4).
- Required, not optional: honest rewrites of `docs/ibkr_integration.md`'s Authentication section
  and `frontend/src/pages/Settings/Ibkr.tsx`'s "never automates IBKR login" copy (§2.4) — shipping
  this feature while those documents assert the opposite would make them false, which this
  project's own conventions (see e.g. every other Phase 4 sub-phase's doc discipline) don't do.

### 8.3 Testing strategy

- `test_providers_ibkr_client.py`, `test_providers_ibkr_options.py`,
  `test_providers_ibkr_portfolio.py` already mock the Gateway's HTTP responses and are unaffected —
  zero test changes required for the provider layer under the recommended design, since the REST
  shape being talked to doesn't change.
- No existing docker-compose-level integration test harness exists in this project (confirmed by
  investigation) and building one is disproportionate to this phase — recommend a **manual, live
  verification runbook** instead, mirroring exactly how Phase 13's original IBKR integration was
  itself verified (`docs/ibkr_integration.md`'s "Real verification" section: real account, real
  symbol, hand-checked values, honestly recorded, not assumed): cold-start the new container against
  a **paper trading account first**, confirm it reaches `authenticated: true`, force-kill the
  container and confirm it recovers unattended, let a session run past its natural expiry and
  confirm automatic re-auth, then repeat once against the live account before trusting it for real
  official captures.
- CI continues to never depend on a live IBKR account or the new gateway container at all — it must
  not be added to any CI compose profile, preserving `docs/ibkr_integration.md`'s existing
  guarantee that CI never even attempts to reach a live IBKR account.

---

## 9. Risks

1. **Protocol/product mismatch** (§2) — the brief's literal "IB Gateway + IBC" names a different
   product than this codebase integrates with. Proceeding on the recommended path without explicit
   confirmation risks building the wrong thing relative to the user's actual intent.
2. **2FA may not be fully automatable for this account** (§3.2) — a real, possibly hard limitation,
   not merely an inconvenience; unverified against the user's actual IBKR account settings.
3. **New credential-compromise blast radius** (§2.4, §7) — this project has never held a real
   brokerage login before; a compromised gateway container's environment now can log into the live
   account, a materially different risk than a leaked read-only market-data key.
4. **Single daily capture window, no retry** (§5.2) — pre-existing design property that automation
   makes more likely to matter, since an automated gateway may restart/reauth on its own schedule
   during market hours.
5. **Third-party, unofficial tooling** — IBeam (like IBC) is community-maintained, not published or
   supported by IBKR itself. An IBKR-side login-flow change could break it without notice, unlike
   the documented REST API surface this project already depends on.
6. **Market data entitlements are unaffected by login automation** — the account's own
   subscriptions still determine live/delayed/frozen/unavailable per contract, exactly as already
   documented in `docs/ibkr_integration.md`; automating login doesn't change what data is available.
7. **IBKR Terms of Service** — whether unattended, credential-based automated login is permitted
   under the user's account agreement is outside this review's technical scope. Flagged for the
   user to confirm directly, not assumed acceptable here.

---

## 10. Summary — open questions requiring confirmation before coding

1. **Client Portal Gateway + IBeam (recommended, §2.3) vs. classic IB Gateway + IBC (the brief's
   literal wording, §2.2).** The former is a config-only change to this codebase; the latter is a
   full provider-layer rewrite and a separate, larger migration project. Confirm the recommended
   path, or confirm the classic-Gateway path is genuinely wanted (and why — e.g. a specific
   entitlement or streaming need the Client Portal Gateway lacks) so it can be scoped as its own
   phase.
2. **Does the user's IBKR account support reduced-friction unattended 2FA for API-only sessions?**
   Must be checked directly in IBKR Account Management — this review cannot verify it from the
   codebase. This determines whether full 24/7 automation is achievable at all, or whether the
   realistic target is "automation that still needs one daily phone tap." Also worth empirically
   diagnosing (§3.4) whether the described "every few hours" pain point is actually IBKR's ~24h
   session ceiling, or session idle-timeout from the current lack of any keep-alive call between the
   two daily cron fires — the fix differs depending on which it is.
3. **Where should IBKR login credentials live** — env-var-only (§7.1 option 1, recommended) or an
   extension of `services/secret_store/` (§7.1 option 2)? Confirm the recommendation, or specify
   the encrypted-store path should be built now instead.
4. **Should `backend` hard-depend on the gateway container's health at startup**
   (`depends_on: condition: service_healthy`), or start independently and degrade per-call exactly
   as it already does for a not-yet-authenticated manual Gateway (recommended, §8.1)? Also: is the
   bounded in-job retry proposed in §5.2 worth building now, or a later hardening pass?
5. **Should the existing manual/local Gateway workflow be kept working side-by-side** (recommended,
   §4.4 — selected simply by which `IBKR_BASE_URL` is configured), or is the goal to fully replace
   it with the always-on containerized flow?
6. **Is a passive System Status page extension sufficient monitoring for v1** (recommended, §5.4),
   or is proactive notification (email/Slack/etc.) actually expected — new infrastructure this
   project has never needed before?
7. **Paper account first, or go straight to live** for the first real, live verification pass of the
   automated login flow (§8.3)? Recommendation: paper first, lower blast radius for a first attempt
   at unattended credential-based login.
8. **IBKR Terms of Service confirmation** (§9.7) — outside this review's scope; needs the user's own
   confirmation before implementation.

Do not start coding until these are resolved.

## 11. Implementation order (once the open questions above are resolved)

1. Resolve Open Questions 1–8 with the user.
2. Stand up the chosen gateway-automation container in isolation (new compose service only,
   `backend` untouched) and verify it reaches `authenticated: true` against a **paper** account with
   zero backend involvement — pure infrastructure validation before any application code depends on
   it.
3. Repoint `IBKR_BASE_URL` at the new service and re-run the existing, unmodified IBKR provider
   test suite plus a manual `get_option_chain`/`get_underlying_quote` smoke call against the paper
   account — this is where the "no code duplication" claim in §4.2 gets empirically confirmed, not
   just asserted.
4. Add the keep-alive scheduler job and extend `IbkrStatus`/the System Status page with the new
   freshness signal (§5.3, §5.4).
5. Add crash-recovery/health-check hardening on the new container, and whatever bounded retry
   behavior Open Question 4 settles on.
6. Rewrite `docs/ibkr_integration.md` and `frontend/src/pages/Settings/Ibkr.tsx` to honestly
   reflect the new automated-login reality (§2.4, §8.2) — required, not optional, before calling
   this phase done.
7. Run the full paper-account verification runbook (§8.3) for a real observation period (length to
   be agreed with the user) before switching to the live account.
8. Switch to the live account. Only after step 7 has been stable — and only relax/remove the manual-
   login guidance if Open Question 5 decided to fully replace, rather than keep alongside, the
   existing local workflow.
