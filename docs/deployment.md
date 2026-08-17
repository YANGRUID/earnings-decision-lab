# Deployment

This document covers two things: the deployment architecture the Docker setup is already
built for (real, working, verified locally), and a real cost comparison for where to actually
run it (research only — nothing described as "estimated" below has been provisioned; no cloud
resources have been created, and no money has been spent).

## What's already real

`docker compose up --build` runs the full stack locally: `db` (PostgreSQL + pgvector) →
`migrate` (one-shot Alembic upgrade, must complete before anything else starts) → `backend`
(FastAPI, health-checked) → `frontend` (the Vite build served by nginx, with a client-side
routing fallback for React Router). Each service has a real Dockerfile and was verified by
actually building the images and running real requests against the running containers — not
just written and assumed to work. See [engineering_decisions.md](engineering_decisions.md)
(Phase 10) for two real bugs this verification step caught (a broken wheel package layout, and
an embedding-model cache permission error) that neither code review nor the test suite would
have caught, since both only surface when the actual built artifact runs.

This same set of images is what any of the deployment targets below would run — the
architecture question is *where* to run them and what manages the database, not *what* to run.

## Cost comparison

Researched using current (2026) pricing pages and vendor documentation, not memorized/assumed
numbers. All figures are for a single small, mostly-idle instance — this project's actual
traffic profile (a personal research tool + portfolio demo, not a production service with
real users).

### Azure (the preferred target per this project's original scope, given the Zurich/enterprise
context this portfolio is built for)

| Component | Service | Estimated monthly cost |
|---|---|---|
| Backend | Azure Container Apps, Consumption plan, scale-to-zero (`min replicas: 0`) | ~$0–5 (near-zero when idle; billed per vCPU-second/GiB-second only while a replica is actually running a request) |
| Frontend | Azure Static Web Apps, Free tier | $0 |
| Database | Azure Database for PostgreSQL Flexible Server, Burstable B1ms (1 vCPU, 2 GiB) | ~$12–20 (compute ~$12.41 + storage/backup — this tier does **not** scale to zero, it's a persistent managed instance) |
| **Total** | | **~$15–25/month**, dominated by the database, which is the one component that can't scale to zero |

pgvector is confirmed supported on Flexible Server (via `shared_preload_libraries`, requiring
one server restart to enable). Scale-to-zero on Container Apps means a real, meaningful cost
lever — the tradeoff is cold-start latency on the first request after an idle period (the
embedding model has to load into memory again; baked into the image per Phase 10, so at least
it doesn't also need network access to Hugging Face on that cold start).

### Cheaper alternative: Fly.io

| Component | Service | Estimated monthly cost |
|---|---|---|
| Backend | Fly Machine, shared-cpu-1x, sized up from the minimum 256MB (onnxruntime + a loaded embedding model realistically needs more headroom) | ~$4–6 |
| Frontend | Served as static files from the same or a second small Fly Machine, or Cloudflare Pages' free tier instead | $0–2 |
| Database | Fly Postgres, shared-cpu-1x, 1GB volume | ~$2–3 |
| **Total** | | **~$8–12/month**, no component scales to zero (Fly removed always-free compute in 2024) |

Roughly 40–50% cheaper than the Azure floor, at the cost of a less "enterprise cloud" story on
a CV/interview walkthrough — a real tradeoff, not a strictly-better option.

### Cheapest real alternative: a single small VPS running `docker compose up -d` directly

A €4–6/month VPS (Hetzner, DigitalOcean) running this repo's own `docker-compose.yml` as-is,
with a reverse proxy (Caddy or nginx) in front for TLS. No managed-service abstraction, no
scale-to-zero, but also no per-service pricing to reason about — one fixed, predictable cost,
and the exact same Docker images and compose file already verified locally. The honest
downside: the user becomes responsible for OS patching, disk space, and uptime monitoring that
a managed platform would otherwise handle.

## What this project is not doing right now

No cloud resources have been created. Provisioning any of the above means real, recurring
personal spending and requires cloud account credentials this environment doesn't have —
that's a decision for the project owner to make explicitly, not something to default into.
The Docker/Compose setup above is deployment-ready for any of the three paths without further
code changes; the remaining work for an actual live deployment is infrastructure
provisioning and secrets configuration on whichever platform is chosen, not application code.

## Secrets in a deployed environment

However this gets deployed, the pattern stays the same as local Docker Compose:
provider/LLM API keys are injected as real environment variables by the hosting platform
(Azure Container Apps secrets, Fly.io secrets, or the VPS's own `.env` file kept outside version
control) — never baked into an image layer, never committed. See
[llm_providers.md](llm_providers.md) for the existing secret-safety protocol this project
already follows for local `.env` handling; the same rules apply to whatever secret store a
cloud platform provides.
