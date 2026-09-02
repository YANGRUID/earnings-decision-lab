# syntax=docker/dockerfile:1
# Multi-stage build following uv's current documented Docker pattern
# (https://docs.astral.sh/uv/guides/integration/docker/): a builder stage
# resolves and installs the locked dependency set with cache mounts, then
# only the resulting virtualenv is copied into a slim runtime image — no
# uv, no build tools, no source-controlled cache in the final image.
#
# --no-editable on the final `uv sync` matters, not a stylistic flag: uv
# installs the local project in editable mode by default, which points back
# at /app/src via a .pth file rather than copying it into site-packages —
# fine in the builder stage, but the final stage below only copies
# /app/.venv, not /app/src, so an editable install would import-error on a
# path that no longer exists. --no-editable makes the venv self-contained.
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# IBKR TWS Migration Phase 1.1 -- `ibapi` is a local-path dependency
# (pyproject.toml's [tool.uv.sources]) resolved from the OFFICIAL
# Interactive Brokers TWS API source, never PyPI (see vendor/
# ibapi_official/PROVENANCE.md for why). It must exist on disk before
# `uv sync` can resolve it below, so this fetch-and-SHA256-verify step
# (stdlib-only: no curl/unzip apt packages needed) runs first. The
# script is idempotent and network-bound only to interactivebrokers.
# github.io's own documented download URL -- never PyPI, never a mirror.
COPY scripts/fetch_ibapi_official.py scripts/fetch_ibapi_official.py
RUN python3 scripts/fetch_ibapi_official.py

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# The RAG embedding model (rag/embeddings.py's FastEmbedProvider) downloads
# its ONNX weights from Hugging Face Hub on first use. Pre-warming that
# cache at build time, not container startup, means: (1) the running
# container never needs outbound network access to Hugging Face to become
# healthy, and (2) startup doesn't race a multi-second download. Found via
# an actual `docker compose up` run, not assumed: the final image runs as a
# non-root user with no writable default HOME, so an un-cached download at
# startup fails with a permission error, not just "slow."
ENV HF_HOME=/app/.cache/huggingface
RUN .venv/bin/python -c "from rag.embeddings import FastEmbedProvider; FastEmbedProvider()"

# Phase 4 RAG hardening (2026-08-26), Section 25 -- the OPTIONAL local
# reranker (rag/reranking.py's FastEmbedReranker) is not called by any
# default code path yet (opt-in only, via hybrid_search's own
# ``reranker`` parameter), but pre-warms here anyway: the exact same
# no-writable-HOME failure mode documented above for the embedding model
# would otherwise hit whoever first enables it in the running container.
# Small (0.08GB), same HF_HOME cache, same build-time-not-startup reasoning.
RUN .venv/bin/python -c "from rag.reranking import FastEmbedReranker; FastEmbedReranker()"

FROM python:3.12-slim
RUN groupadd --system app && useradd --system --gid app app

WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/migrations /app/migrations
COPY --from=builder --chown=app:app /app/alembic.ini /app/alembic.ini
COPY --from=builder --chown=app:app /app/.cache /app/.cache

ENV PATH="/app/.venv/bin:$PATH" \
    HF_HOME=/app/.cache/huggingface
USER app

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/v1/health')" || exit 1

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
