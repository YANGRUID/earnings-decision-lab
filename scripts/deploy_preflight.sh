#!/usr/bin/env bash
# Phase 4 deployment-safety hardening (2026-08-26), Section 40 -- checks
# real, current disk space before a large Docker rebuild/migration cycle.
#
# This exists because of a real incident during this project's own
# development: `docker compose run --rm migrate` hit a genuine Postgres
# "No space left on device" error mid-migration, traced (via `df -h` and
# `docker system df`, not guessed) to accumulated Docker build cache and
# images from repeated rebuild cycles -- the database volume itself was
# small and never implicated. See this phase's own final report for the
# full incident writeup.
#
# Read-only: this script only inspects real, current disk/Docker state
# and prints a warning or fails -- it NEVER runs any pruning or cleanup
# itself. In particular it never runs `docker system prune --volumes` or
# any other destructive command; freeing space, if needed, is always a
# deliberate, separate, human-reviewed action (see the warning message
# below for the exact non-destructive commands this project has already
# used safely: `docker builder prune -a -f` and `docker image prune -f`).
#
# Usage: ./scripts/deploy_preflight.sh [min_free_gb]
#   min_free_gb defaults to 15 -- comfortably above what one real
#   migration + a two-service image rebuild has needed in practice this
#   session, with headroom for Docker's own layer overhead.

set -euo pipefail

MIN_FREE_GB="${1:-15}"

echo "== Deployment preflight: disk space =="
echo

echo "-- Host filesystem (df -h /) --"
df -h / 2>&1
echo

if ! command -v docker >/dev/null 2>&1; then
  echo "docker CLI not found -- skipping Docker-specific checks." >&2
  exit 0
fi

echo "-- Docker Desktop VM disk usage (docker system df) --"
docker system df 2>&1
echo

# macOS (BSD df) reports Avail in 1K blocks under -Pk; Linux df -Pk does
# too -- this parses portably instead of relying on df -h's
# locale/unit-dependent human-readable column.
AVAIL_KB=$(df -Pk / | awk 'NR==2 {print $4}')
AVAIL_GB=$((AVAIL_KB / 1024 / 1024))

echo "Free space on host filesystem: ${AVAIL_GB}Gi (threshold: ${MIN_FREE_GB}Gi)"
echo

if [ "$AVAIL_GB" -lt "$MIN_FREE_GB" ]; then
  echo "FAIL: only ${AVAIL_GB}Gi free, below the ${MIN_FREE_GB}Gi preflight threshold." >&2
  echo "Refusing to proceed automatically -- a migration or image rebuild failing" >&2
  echo "mid-operation from disk exhaustion is worse than stopping here first." >&2
  echo >&2
  echo "This project's own real incident was resolved with (non-destructive," >&2
  echo "does NOT touch the database volume):" >&2
  echo "    docker builder prune -a -f" >&2
  echo "    docker image prune -f" >&2
  echo >&2
  echo "Never run 'docker system prune --volumes' or any command that touches" >&2
  echo "named volumes as an automated recovery -- that risks the database." >&2
  echo "Review what 'docker system df' above shows is reclaimable, free space" >&2
  echo "deliberately, then re-run this script." >&2
  exit 1
fi

echo "OK: ${AVAIL_GB}Gi free, at or above the ${MIN_FREE_GB}Gi threshold."
