# CV entry

Finalized after implementation was verified — every number below is real and reproducible
(`docs/evaluation.md`, `git log`, a real test run), not estimated for effect.

## Earnings Decision Lab — AI-assisted earnings intelligence & options analytics platform

- Built a production-style research platform (Python/FastAPI, React/TypeScript, PostgreSQL +
  pgvector) combining deterministic financial analytics with a tool-calling AI agent over real
  SEC filing data for four tickers — provider-agnostic LLM layer (DeepSeek/OpenAI/Anthropic),
  hybrid vector+keyword retrieval, structured extraction, and explicit multi-stage agent
  orchestration with self-verification.
- Designed and ran a hand-curated 51-item evaluation framework measuring retrieval, RAG-answer,
  agent, and extraction quality against the live system; used it to find and fix two real
  dataset-construction errors and a measured retrieval-quality gap (35% Recall@5), documented
  with root-cause analysis rather than hidden.
- Shipped with 243 automated tests, a Dockerized multi-service deployment (CI-verified build
  and boot on every push), and structured observability — catching and fixing a real credential-
  leak bug and a packaging bug that only surfaced when the built artifact was actually run.
