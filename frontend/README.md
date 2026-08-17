# Frontend

React + TypeScript research interface for Earnings Decision Lab. Vite + React Router, no UI
framework dependency — a small hand-written design system (`src/index.css`) instead, to keep
the bundle lean and the look intentionally plain: clarity and density over a flashy
trading-terminal aesthetic (see `docs/engineering_decisions.md`).

## Screens

- **Dashboard** — covered companies
- **Company** (`/company/:ticker`) — earnings history
- **Earnings Event** (`/earnings/:id`) — actual results, price reaction, market-expectations
  section (honestly empty — no options-data provider configured yet)
- **Options Lab** — deterministic strategy payoff calculator + ATM-straddle implied move,
  both pure client-driven calculators requiring no live market data
- **AI Research** — the agent orchestrator's question/answer interface: answer, citations, and
  the full execution trace (tool calls, verification outcome, model, tokens, estimated cost)
- **Historical Replay** — honestly empty (no historical options-chain data yet)
- **Data / Evaluation Status** — live counts plus a plain statement of what has real data
  behind it and what doesn't

## Local development

```bash
cp .env.development .env.development.local   # optional: override VITE_API_BASE_URL
npm install
npm run dev
```

Requires the backend running locally (see `../backend/README.md`) — default expected at
`http://localhost:8000/api/v1`.

## Type safety

`src/types/api.ts` mirrors `backend/src/schemas/api.py` by hand — there's no codegen pipeline
yet (see `docs/limitations.md`). If a backend response schema changes, this file needs a
matching manual update.
