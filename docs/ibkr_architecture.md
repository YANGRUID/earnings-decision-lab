# IBKR integration architecture (current production)

**Status: authoritative.** This is the canonical reference for how this project talks to
Interactive Brokers as of the **2026-09-01 production cutover**. Where this document and any
older IBKR document disagree, this one is correct.

| | |
|---|---|
| **Production transport** | IB Gateway / TWS socket API (`IBKR_PROVIDER=tws`) |
| **Socket port** | `4001` (IB Gateway **LIVE**), set explicitly in `.env` |
| **Market data** | **DELAYED** — labeled `delayed` everywhere it is stored or displayed |
| **Web / Client Portal** | Retained, working, **manual rollback only** |
| **Order capability** | None. Structurally blocked in code (see below) |

Two older documents remain useful but are now scoped to the rollback path:
[`ibkr_integration.md`](ibkr_integration.md) (manual Client Portal login) and
[`ibkr_gateway_runtime.md`](ibkr_gateway_runtime.md) (IBeam automation). Neither describes the
current production path.

---

## Production data flow

```
IB Gateway (desktop app, on the operator's own Mac, LIVE, manually authenticated)
    |
    |  TWS socket API, port 4001, read-only
    v
TWSConnectionManager            providers/ibkr_tws_client.py
    |    one long-lived socket per process; bounded timeouts; typed errors
    v
IBKRTWSProvider                 providers/ibkr_tws_options.py
    |    normalized OptionQuote / UnderlyingQuote, source_provider="ibkr_tws"
    v
providers/factory.py            _build_ibkr_transport -> the process's shared provider
    |
    +--> decision generation        services/decision_engine.py
    +--> entry capture              services/benchmark_entry_capture.py
    +--> exit / settlement capture  services/benchmark_exit_capture.py
    +--> close reconstruction       services/options_reconstruction.py
    +--> research preparation       workers/research_preparation_worker.py
```

### One shared connection per process

`providers/factory.py` holds a module-level `_shared_tws_provider`. It is set **once per
process**, at startup, and every consumer resolves through the factory to that same instance.
Nothing constructs a connection per request or per job.

Two processes own connections, and each registers its own:

| Process | Registers via | Client ID |
|---|---|---|
| Backend (FastAPI) | `api/main.py` lifespan → `set_shared_tws_provider` | **101** |
| Backend TWS health probe | `api/main.py` lifespan (separate connection) | **1001** (`101 + 900`) |
| research-worker | `workers/research_preparation_worker.py` → `set_shared_tws_provider` | **102** |

Client IDs are deterministic and never reused. IB Gateway rejects a second connection at an
already-active client ID with **error 326**, so these must not collide.

> **A separate process does not inherit the singleton.** `_shared_tws_provider` is a module-level
> global, so a script — or `docker compose exec backend python -c ...` — imports a *fresh* copy
> where it is `None` and silently builds its **own** connection at the configured client ID,
> colliding with production. This was hit for real during the cutover. To verify the live shared
> connection, use the in-process diagnostic endpoint (below), never a separate process.

### What must never spawn a connection

Operations polling, System Status polling, and Settings → IBKR "Test Connection" all reuse the
existing long-lived connections. Verified under load during the cutover: 90 consecutive
Operations/System-Status requests held the established socket count flat at exactly 2.

"Test Connection" reuses the app's **persistent health probe**. It previously called
`get_tws_status(probe=None)`, which spawned a one-shot connection at the probe's own client ID
1001 and collided with it (a real error 326). Regression coverage lives in
`tests/test_services_provider_test_connection.py`.

---

## Read-only enforcement

Two independent layers:

1. **Code (enforced, testable).** `TWSConnectionManager` overrides every order-capable ibapi
   method — `placeOrder`, `cancelOrder`, `reqOpenOrders`, `reqGlobalCancel`, `exerciseOptions` —
   to raise `RuntimeError` *before* touching arguments or the socket. There is no order schema,
   no execution service, and no UI that could submit one. Receiving `nextValidId` on connect does
   **not** authorize order placement.

2. **IB Gateway's "Read-Only API" checkbox (operator-controlled).** This setting is **not
   queryable through the API**, and this project deliberately does **not** probe it — any probe
   would be order-adjacent. It is therefore an **operator responsibility**: confirm it is ticked
   in the IB Gateway UI. Treat layer 1 as the guarantee and layer 2 as defence in depth.

---

## Operating IB Gateway

**Authentication is manual, in the IB Gateway desktop application.** This backend never holds an
IBKR username, password, account ID, session token, or 2FA code, and no UI in this project asks
for one. When the API is not ready, Settings → IBKR says so and gives instructions — it does not
offer a login button, because there is nothing this application could do with one.

1. Open IB Gateway on the host machine.
2. Log in and complete 2FA there.
3. Return to the app and use **Refresh Status**.

**Auto Restart.** IB Gateway's own Auto Restart re-launches the Gateway daily without a full
re-login. The socket drops across that restart; `TWSConnectionManager.ensure_connected()` performs
bounded-backoff reconnection, and a reconnect immediately after a controlled disconnect was
verified live (0.003 s). A restart that requires a *full* re-login is a manual step.

**Backend restart.** The shared provider connects **lazily** — on first real market-data use, not
at startup. Immediately after a cold restart the connection state is honestly `disconnected` and
market-data quality is honestly unknown until IBKR's first real `marketDataType` callback. This is
correct behavior, not a fault: the system never claims `delayed` before IBKR has said so.

---

## Market-data quality

Current entitlements deliver **delayed** data. Every `OptionQuote` and `UnderlyingQuote` carries
`market_data_quality="delayed"` and `source_provider="ibkr_tws"`, persisted rows record it, and
the UI shows it prominently rather than hiding it in a tooltip. This matters for interpreting any
V4 research or benchmark output.

**Live/paper account is not knowable over TWS.** The Web Gateway exposes an `isPaper` boolean;
the TWS socket API has no equivalent wired up here. `live_account` is therefore reported as
`null` (unknown) — never guessed, and the Operations pre-flight check for it is *omitted* under
TWS rather than fabricated as a pass.

---

## Known limitation: historical bars (error 2188)

TWS returns **error 2188** — *"Up-to-the-second historical data requires additional subscription
for the API"* — for the historical-bar requests `services/options_reconstruction.py` uses.

Traced dependency, deliberately not worked around:

| Consumer | Depends on historical bars? |
|---|---|
| Official entry capture | **No** |
| Official settlement / exit capture | **No** |
| Decision generation | Only when the market is closed *and* nothing adequate was captured live |
| Strategy Lab / research | Yes (research only, never an official record) |

Because neither official entry nor official settlement depends on it, this is **non-blocking**.
Reconstruction fails honestly with a typed `IBKRError` where TWS historical data is unavailable.
No fallback data is fabricated, and no other provider supplies these bars
(Alpha Vantage inherits the "unsupported" default; Tiingo has no options provider).

---

## Diagnostics

`GET /api/v1/internal/ibkr/tws-production-sanity` — read-only, in-process. Fetches one underlying
quote and one exact option quote through the **shared production provider**, returning latency,
provenance, and market-data quality. Opens no database session, exposes no credentials, and cannot
reach any order path.

**Disabled by default.** It is registered only when `ENABLE_INTERNAL_DIAGNOSTICS=true`; otherwise
the route does not exist and does not appear in `/docs`. Like every endpoint in this local-first
deployment it is unauthenticated, which is why it is opt-in.

---

## Rollback to Web

Retained for rollback and exercised by tests: `IBKROptionsProvider`, `IBKRClient`, the
`ibkr-gateway` (IBeam) Compose service, all Web `.env` configuration, and the Web rollback UI.
The frontend switches automatically on `tws.configured` — rollback needs **no code change**.

1. Set `IBKR_PROVIDER=web`.
2. Close the TWS brokerage session.
3. Authenticate the Client Portal Gateway.
4. Restart the backend.
5. Verify Web health in Settings → IBKR.

> IBKR permits only **one brokerage session per username**, so TWS and Web cannot be
> authenticated simultaneously — step 2 must precede step 3.

There is deliberately **no automatic cross-provider failover**, and none per-leg. Switching
transports is always an explicit human decision.

---

## Configuration reference

| Variable | Production value | Notes |
|---|---|---|
| `IBKR_PROVIDER` | `tws` | `web` for rollback |
| `IBKR_TWS_HOST` | `host.docker.internal` | Gateway runs on the host, not in a container |
| `IBKR_TWS_PORT` | `4001` | **LIVE.** Code default is `4002` (paper) — set explicitly |
| `IBKR_TWS_CLIENT_ID` | `101` | Health probe derives `1001` from this |
| `IBKR_TWS_RESEARCH_WORKER_CLIENT_ID` | `102` | Applied to research-worker via Compose |
| `ENABLE_INTERNAL_DIAGNOSTICS` | `false` | Opt-in diagnostic endpoint |
| `IBKR_BASE_URL` | *(rollback)* | Client Portal Gateway REST base |

`IBKR_TWS_PORT` deserves care: the code default `4002` is the **paper** port, this deployment's
Gateway is **live on 4001**, and nothing listens on 4002. A mismatch fails every options request
silently.
