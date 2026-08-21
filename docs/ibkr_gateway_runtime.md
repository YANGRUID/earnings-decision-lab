# IBKR Gateway runtime automation (Phase 4.8A)

Automates login to the IBKR **Client Portal (Web) API Gateway** — the same REST Gateway
`docs/ibkr_integration.md` and every file under `backend/src/providers/ibkr_*.py` have always
talked to — so the backend can pull real IBKR market data 24/7 without a human re-opening a
browser and logging in every few hours. This document covers the new Docker service, the
authentication flow, reliability design, and security model it introduces. It does not change
anything about *what* the integration does (options chains, underlying quotes, positions) — only
*how the Gateway session stays alive*.

**Optional.** Leave `IBKR_ACCOUNT`/`IBKR_PASSWORD` blank in `.env` and this entire runtime is
inert — `docker-compose.yml`'s `ibkr-gateway` container simply never authenticates, and the
original manual workflow in `docs/ibkr_integration.md` keeps working exactly as before, unchanged.

**Read-only, same guarantee as the rest of this integration, unchanged by this phase.** Nothing
here calls an order-placement, order-modification, order-cancellation, order-preview, what-if, or
exercise endpoint. `IBKRClient` (`backend/src/providers/ibkr_client.py`) still only ever issues
`GET` requests — this phase did not touch that file, or any other provider file.

## Why IBeam, not IBC

The phrase "run IBKR headless 24/7" most commonly turns up **IBC** (`IbcAlpha/IBC`), which
automates a different product: the classic, socket-protocol **"IB Gateway"** (TWS API, port
4001/4002). This codebase has never used that — it was built entirely against the **Client Portal
Gateway** (REST, port 5000/5001, `/iserver/*` endpoints). Automating login to a Gateway this
project doesn't use would mean rewriting `IBKRClient`/`IBKROptionsProvider`/`IBKRPortfolioProvider`
against a different transport entirely — not a runtime-automation task.
[**IBeam**](https://github.com/Voyz/ibeam) is the equivalent, purpose-built tool for the Gateway
this project actually uses. See `PHASE4_8A_IBKR_RUNTIME_ARCHITECTURE_REVIEW.md` §2 for the full
reasoning this decision was made under.

## Architecture

```
docker-compose network
    ibkr-gateway container (image: voyz/ibeam)
        IBeam            -- drives the login page, holds the session alive,
                             restarts the Gateway process on a dropped session
        Client Portal Gateway (the same Java process this project has always
                                talked to -- github.com/Voyz/ibeam just starts
                                and logs into it automatically)
              |
              | network_mode: bridge (IBeam's own requirement -- clientportal.gw's
              | IP allow-list rejects a custom Compose network), ports published
              | to the host: 5000 (Gateway REST API), 5001 (IBeam's own health server)
              v
        [reachable from other containers via host.docker.internal, same
         mechanism backend already used to reach a manually-run host Gateway]

    backend container
        IBKRClient / IBKROptionsProvider / IBKRPortfolioProvider  -- byte-for-byte
        unchanged; only IBKR_BASE_URL now points at the container above instead of
        a manually-run host process
              |
              | GET https://host.docker.internal:5000/v1/api/...
              v
        [reaches ibkr-gateway's published port]

    IBKR's own servers (the live account) -- only ibkr-gateway ever talks to
    these directly for login; backend never does
```

Requested shape, confirmed to match:

```
Backend -> IBKR Provider -> Client Portal Gateway -> IBeam container -> IBKR Live Account
```

## Setup

1. Copy `.env.example`'s new Phase 4.8A block into your own `.env` (never commit it).
2. Set `IBKR_ACCOUNT` and `IBKR_PASSWORD` to your real IBKR login. **This is your live brokerage
   account login — treat it with the same care as your bank password**, not like the read-only
   market-data API keys elsewhere in this file (see Security below).
3. Set `OPTIONS_PROVIDER=ibkr`.
4. Decide your 2FA path (see below) and set `IBKR_TWO_FA_HANDLER`/`IBKR_PYOTP_SECRET` accordingly,
   or leave both blank to require one manual approval per fresh login.
5. `docker compose up -d ibkr-gateway` and watch `docker compose logs -f ibkr-gateway` for the
   first login. `docker compose up -d backend` once you see it reach an authenticated state (see
   Verification below) — though `backend` does not actually wait on this; it starts regardless (see
   Reliability).

## Authentication flow

### First login

IBeam drives the same login page a human would use: submits `IBKR_ACCOUNT`/`IBKR_PASSWORD`, then
handles the second factor per `IBKR_TWO_FA_HANDLER`. **IBKR does not support disabling 2FA for API
sessions** (confirmed by IBKR to IBeam's own maintainers) — every path below still involves a real
second factor, only the handling of it differs:

| `IBKR_TWO_FA_HANDLER` | What it does | Unattended? |
|---|---|---|
| *(blank)* | No automated 2FA — a fresh login blocks until you approve it yourself (e.g. an IBKR Mobile push) | No — needs one manual tap per fresh login |
| `PYOTP` | Generates a TOTP code from `IBKR_PYOTP_SECRET` automatically | **Yes** — the only genuinely unattended path |
| `GOOGLE_MSG` | Reads an SMS code from Google Messages (requires pairing) | Partially — depends on external phone infrastructure |
| `EXTERNAL_REQUEST` | Fetches the code from an HTTP endpoint you run | Only as unattended as whatever you build |

**To get genuinely unattended operation, your IBKR account's second factor must be a TOTP
authenticator, not push-only IBKR Mobile approval.** Set this up in IBKR Account Management →
Secure Login System, and record the base32 secret it gives you (looks like `JBSWY3DPEHPK3PXP`) into
`IBKR_PYOTP_SECRET`. This is an account-level setting this project cannot configure for you and has
not verified against your specific account — confirm it there directly before assuming `PYOTP` will
work end-to-end.

### In-app connection ("Connect IBKR" button)

Settings → Interactive Brokers now has a **Connect IBKR** button, so you never need to know or
type a Gateway URL yourself. It calls `GET /api/v1/ibkr/connect`, which does nothing but return
`{"url": "https://localhost:<IBKR_GATEWAY_PORT>"}` (`api/routers/ibkr.py`) — no password field, no
session handling, no proxying. The frontend opens that URL in a new tab; you log in there, on the
Gateway's own real page, exactly as in the original manual workflow, just without having to look up
the address. This backend never sees your username, password, or 2FA code either way. Click
**Refresh Status** afterward to see the real, live result.

This is independent of, and works alongside, IBeam's own automated login above: if
`IBKR_ACCOUNT`/`IBKR_PASSWORD` are set, IBeam may already be authenticated by the time you look —
in which case the new tab just shows an already-logged-in Gateway, harmlessly. If they're blank (or
2FA needs a manual tap IBeam can't automate), this button *is* how you log in, targeting the
containerized Gateway instead of a manually-started host process.

### Session maintenance

Once authenticated, IBeam periodically re-validates the session and restarts the underlying Gateway
process on a dropped one (`RESTART_FAILED_SESSIONS`, its own default) — this project relies on that
built-in behavior rather than re-implementing it.

Separately, `services/scheduler.py::run_ibkr_gateway_healthcheck_job` polls the real
`/iserver/auth/status` every 10 minutes, all day (not just around the two daily capture windows),
purely as an observer: a low-cost keep-alive touch, and a structured log line
(`ibkr gateway healthcheck: CONNECTED` / `... AUTH_REQUIRED (...)` / `... GATEWAY_UNREACHABLE (...)`)
an operator can grep for. It never attempts to fix a broken session itself — that stays IBeam's job.

### Automatic reconnect

If a session drops mid-day, IBeam's own internal logic attempts to restore it without any container
restart. The keep-alive job above will surface the transition in the logs, and the Settings →
Interactive Brokers page (see Health monitoring below) reflects it on next load.

### Session expiration handling

IBKR documents session validity up to ~24 hours from authentication, with idle sessions requiring a
periodic touch well inside that window. The keep-alive job's 10-minute cadence is that touch. A
session that has genuinely expired (not just gone idle) triggers IBeam's own re-authentication —
which itself needs a fresh 2FA pass unless `PYOTP` is configured.

## Health monitoring

`GET /api/v1/system-status` (already existing) now returns one additional field on `ibkr`:
`status_label`, one of:

```
IBKR: CONNECTED             -- gateway reachable, authenticated, connected, no competing session
IBKR: AUTH_REQUIRED         -- gateway reachable but not authenticated/connected
IBKR: COMPETING_SESSION     -- another session (e.g. TWS) holds the connection
IBKR: GATEWAY_UNREACHABLE   -- can't reach the gateway process at all
```

Computed by `services/system_status.py::ibkr_status_label`, a pure function over the same real
fields `IbkrStatus` already carried before this phase (`gateway_reachable`, `authenticated`,
`connected`, `competing`) — no new live call, no new persisted state. Rendered on the existing
Settings → Interactive Brokers page (`frontend/src/pages/Settings/Ibkr.tsx`), and reused verbatim
by the scheduler's keep-alive job (§ above) so the two can never disagree about what "connected"
means.

The Settings page's top-level summary collapses these four values into the three-icon form the
in-app workflow asks for — 🟢 Connected / 🔴 Authentication Required / ⚪ Gateway Offline
(`COMPETING_SESSION` folds into the same red "Authentication Required" bucket, still shown
verbatim as `IBKR: COMPETING_SESSION` in the detail line underneath, never hidden) — while the API
itself keeps reporting the more precise four-value `status_label` for anything that wants it.

## Reliability

- **Container restart policy is deliberately `restart: "no"`, not `unless-stopped`.** This follows
  IBeam's own upstream Docker Compose guidance, not this project's default convention for
  long-running services: a container-level restart resets IBeam's internal failed-login counter,
  and repeated automated login attempts against a **live** brokerage account risk tripping IBKR's
  own account-security/fraud-detection systems. IBeam already restarts the *Gateway process*
  internally on a dropped session (`RESTART_FAILED_SESSIONS`) — a human investigating and manually
  restarting the *container* is the deliberate fallback for a genuine, repeated failure (wrong
  password, IBKR-side account issue, 2FA misconfiguration), not another automatic retry loop.
- **`network_mode: bridge`, not a custom Compose network** — also IBeam's own requirement:
  `clientportal.gw`'s IP allow-list rejects requests routed through a Compose-managed network.
  Reachability from `backend` is via its published host ports and the existing
  `host.docker.internal` mechanism (`docker-compose.yml`'s `backend` service already had this for
  reaching a manually-run host Gateway — nothing new there).
- **`backend` does not hard-depend on `ibkr-gateway`'s health.** A not-yet-authenticated or
  still-starting Gateway must never block every other, unrelated backend endpoint from serving —
  this was already true before this phase (`providers/ibkr_client.py::IBKRClient.get()` already
  turns an unreachable Gateway into a clean, caught `IBKRGatewayUnavailableError`, never a crash),
  and this phase preserves it rather than introducing a new hard coupling.
- **Docker health check** on `ibkr-gateway` itself polls IBeam's own `/readyz` (port 5001, plain
  HTTP) — 200 only once the session is genuinely authenticated, 503 otherwise — so
  `docker compose ps` gives a real, meaningful health signal distinct from "the container process
  is merely running."
- **Timeout handling** was already correct before this phase and needed no change:
  `IBKRClient` already sets a 20-second request timeout and turns a timeout into the same
  `IBKRGatewayUnavailableError` every other unreachable-Gateway case produces.

## Security

- **Never hardcoded, never committed.** `IBKR_ACCOUNT`/`IBKR_PASSWORD`/`IBKR_PYOTP_SECRET` live
  only in your local `.env` (already gitignored) and are passed to the `ibkr-gateway` container at
  runtime via `env_file:`/`environment:` — never baked into a Docker image layer, never written
  into `docker-compose.yml` itself (only `${VAR}` interpolation, matching every other secret-shaped
  value already in that file, e.g. `POSTGRES_PASSWORD`).
- **The backend application code (`backend/src`) never reads these values at all** — it only ever
  reads `IBKR_BASE_URL`, exactly as before this phase. The credential-holding surface is exactly
  one new, small, purpose-built container; the already-audited application code's own trust
  boundary is unchanged.
- **This is a real step up in sensitivity from every other credential in `.env`.** A compromised
  `ibkr-gateway` container's environment can log into your live IBKR account — a materially
  different blast radius than a leaked read-only market-data API key. Treat `.env` accordingly on
  any machine this runs on.
- **`IBKR_ACCOUNT`/`IBKR_PASSWORD` are held in plain text in `.env`.** An encrypted-password option
  (`IBEAM_KEY`/Fernet, IBeam's own "Advanced secrets" feature) exists upstream but is deliberately
  **not** wired into `docker-compose.yml` — a real bug was found live during setup (setting that
  variable at all, even empty, breaks plain-text login outright; see Troubleshooting) and fixing it
  properly needs a custom entrypoint, out of scope here. Unrelated to this project's own
  `SECRET_STORE_MASTER_KEY` (`services/secret_store/`), which is not used for IBKR at all — see the
  architecture review §7.1 for why extending that encrypted-at-rest store to a username+password
  pair was scoped out of this phase too.
- **Verify container logs never print the password.** Run `docker compose logs ibkr-gateway` after
  a fresh start and confirm `IBKR_PASSWORD`'s real value never appears — a known failure mode in
  this class of tool, checked here rather than assumed safe because the tool is reputable.
- **IBKR Terms of Service.** Whether unattended, credential-based automated login is permitted under
  your account agreement is your own responsibility to confirm — outside this project's scope to
  determine.

## How to start the system

```bash
docker compose up -d db migrate
docker compose up -d ibkr-gateway
docker compose logs -f ibkr-gateway   # watch for a successful first login
docker compose up -d backend frontend
```

If this fails with `Ports are not available... 0.0.0.0:5000`, something else on your machine
already has that port (on macOS, `ControlCenter`/AirPlay Receiver, real and common — see
Troubleshooting) — set `IBKR_GATEWAY_PORT`/`IBKR_GATEWAY_HEALTH_PORT` in `.env` to unused ports
instead, and update `IBKR_BASE_URL` to match.

## How to verify the IBKR connection

Ports below assume the defaults (5000/5001) — substitute your own `IBKR_GATEWAY_PORT`/
`IBKR_GATEWAY_HEALTH_PORT` if you remapped them.

1. **Container health**: `docker compose ps ibkr-gateway` should show `healthy` once authenticated
   (allow up to ~90 seconds on a cold start for the Gateway process itself to boot).
2. **Direct Gateway check**, bypassing the backend entirely:
   ```bash
   curl -k https://localhost:5000/v1/api/iserver/auth/status
   ```
   Expect `{"authenticated": true, "connected": true, "competing": false, ...}`.
3. **IBeam's own readiness endpoint**:
   ```bash
   curl http://localhost:5001/readyz   # "OK" (200) once authenticated, "Not Ready" (503) otherwise
   ```
4. **Through the backend** (confirms `IBKR_BASE_URL` is wired correctly end to end):
   ```bash
   curl http://localhost:8000/api/v1/system-status | python3 -c "import json,sys; print(json.load(sys.stdin)['ibkr'])"
   ```
   Expect `status_label` to be `"CONNECTED"`.
5. **In the app**: open Settings → Interactive Brokers, click **Connect IBKR** (opens the Gateway's
   real login page in a new tab — log in there), then **Refresh Status**. The page now shows
   🟢 Connected / `IBKR: CONNECTED` alongside the existing gateway/session detail fields.
6. **A real, read-only data call** (once `OPTIONS_PROVIDER=ibkr`): any existing research flow that
   fetches an option chain now sources it from the automated container instead of a manually-run
   Gateway — no code path changed, only where the Gateway lives.

## Troubleshooting

- **`docker compose up -d ibkr-gateway` fails with "Ports are not available... 0.0.0.0:5000"**:
  real, live-observed on macOS — `ControlCenter` (AirPlay Receiver) binds port 5000 by default on
  every current macOS install, and if you already run the manual Gateway workflow, its `java
  ...GatewayStart` process is likely already on port 5001 too (`lsof -nP -iTCP:5000 -iTCP:5001
  -sTCP:LISTEN` to check). Either disable AirPlay Receiver (System Settings → General → AirDrop &
  Handoff), or simply pick different host ports in `.env`:
  ```
  IBKR_GATEWAY_PORT=5002
  IBKR_GATEWAY_HEALTH_PORT=5003
  ```
  and update `IBKR_BASE_URL` to match (`https://host.docker.internal:5002/v1/api`) — the whole
  stack (backend, `/ibkr/connect`, the healthcheck) reads these from config, nothing is hard-coded,
  so remapping is a `.env`-only change.
- **Login always fails with `Fernet key must be 32 url-safe base64-encoded bytes` in
  `docker compose logs ibkr-gateway`, even with a correct plain-text password**: a real bug found
  and fixed during initial setup, not a credential problem. IBeam treats `IBEAM_KEY` (the optional
  encrypted-password decryption key) as "decrypt the password" the moment the variable is merely
  *present* in its environment — including present-but-empty, which Compose's `environment:` map
  form always produces when `IBKR_PASSWORD_KEY` is unset. `docker-compose.yml`'s `ibkr-gateway`
  service no longer sets `IBEAM_KEY` at all for this reason; encrypted-password support isn't wired
  up in this compose file (would need a custom entrypoint that only exports it conditionally — a
  separate, later enhancement). If you pulled this repo before this fix, update
  `docker-compose.yml` to match.
- **Stuck at `AUTH_REQUIRED` with `IBKR_TWO_FA_HANDLER` blank**: expected — a fresh login is
  waiting on a manual 2FA approval. Check `docker compose logs ibkr-gateway` for the prompt.
- **Repeated failed logins**: stop (`docker compose stop ibkr-gateway`), verify
  `IBKR_ACCOUNT`/`IBKR_PASSWORD` in `.env`, and check IBKR Account Management for any account-side
  lock triggered by the failed attempts before restarting — do not just restart in a loop.
- **`GATEWAY_UNREACHABLE` from the backend but the container looks healthy**: confirm
  `IBKR_BASE_URL` in `.env` points at `host.docker.internal:5000` (the container's published port,
  or whatever you remapped `IBKR_GATEWAY_PORT` to), not `:5001` (that's the old manual-Gateway
  default / IBeam's own health-server port, not its Gateway REST port).
