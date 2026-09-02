"""Pre-live hardening (2026-08-25) -- the research-preparation worker
process. Runs as its own, dedicated OS process (Docker Compose service
``research-worker``), completely separate from the FastAPI backend --
see services/research_preparation_queue.py's own module docstring for
why. A backend container restart has zero effect on this process, and
a restart of this process has zero effect on the backend; each owns
only its own lifetime.

Loop: recover any stale (dead-worker) rows, claim the next real
candidate, run the exact same prepare_company_research() pipeline the
Search page's own "Prepare Research" button already uses (unchanged),
then poll again. Single instance by design for now -- claim_next_
preparation_job's own real ``SELECT ... FOR UPDATE SKIP LOCKED``
already makes running more than one safe (Section 10's own "start
conservative, allow raising later without changing queue semantics"),
but real per-company work is already comfortably fast at real observed
volume (a new company took ~55-80s end to end, live, during this
project's own pre-live hardening; an already-fresh one takes seconds),
so there's no real reason yet to add concurrent SEC/provider load.

Never generates a decision, never freezes a DecisionSnapshot, never
captures an option entry price -- see services/earnings_research_
preparation.py's own module docstring for the exact boundary this
process (like the rest of the automatic-preparation architecture)
respects.
"""

import logging
import signal
import time
import uuid
from datetime import UTC, datetime
from types import FrameType

from core.config import get_settings
from db.session import SessionLocal
from models.research_preparation_job import JobStatus, ResearchPreparationJob
from observability.logging import configure_logging
from providers.factory import set_shared_tws_provider
from providers.ibkr_tws_options import IBKRTWSProvider
from rag.embeddings import FastEmbedProvider
from services.research_orchestration import (
    UnsupportedSymbolError,
    build_research_providers,
    prepare_company_research,
)
from services.research_preparation_queue import (
    claim_next_preparation_job,
    recover_stale_running_jobs,
)

log = logging.getLogger("research_preparation_worker")

# How often to poll for new work when the queue is empty -- lightweight
# (one indexed SELECT), no real cost to keeping this short; matches the
# general "don't hammer, but stay responsive" posture the rest of this
# project's own polling already follows.
POLL_INTERVAL_SECONDS = 10

# How often to sweep for stale (dead-worker) RUNNING rows -- doesn't
# need to be as frequent as the poll loop itself; LEASE_TIMEOUT (5
# minutes, see services/research_preparation_queue.py) already bounds
# how long a truly abandoned row can sit before it's reclaimable, and a
# sweep every minute is well inside that margin.
RECOVERY_INTERVAL_SECONDS = 60

_shutdown_requested = False


def _handle_shutdown_signal(signum: int, frame: FrameType | None) -> None:
    global _shutdown_requested
    _shutdown_requested = True
    log.info("research worker: shutdown requested (signal %s)", signum)


def _process_claimed_job(
    db, job: ResearchPreparationJob, research_providers, worker_id: str
) -> None:
    try:
        prepare_company_research(db, job.ticker, research_providers, existing_job=job)
    except UnsupportedSymbolError as exc:
        db.rollback()
        row = db.get(ResearchPreparationJob, job.id)
        if row is not None:
            row.status = JobStatus.FAILED
            row.error = f"no longer a supported symbol: {exc.reason}"[:500]
            row.completed_at = datetime.now(UTC)
            db.add(row)
            db.commit()
        log.warning("research worker %s: ticker=%s unsupported: %s", worker_id, job.ticker, exc)
    except Exception as exc:  # noqa: BLE001 -- boundary: one company's failure must not crash the worker loop
        # prepare_company_research already handles every failure inside
        # its own real pipeline; reaching here means something failed
        # OUTSIDE that (e.g. building providers, a genuinely unexpected
        # error) -- the row still needs an honest terminal state rather
        # than being left RUNNING with no further heartbeat.
        db.rollback()
        row = db.get(ResearchPreparationJob, job.id)
        if row is not None:
            row.status = JobStatus.FAILED
            row.error = str(exc)[:500]
            row.completed_at = datetime.now(UTC)
            db.add(row)
            db.commit()
        log.error(
            "research worker %s: ticker=%s failed outside prepare_company_research",
            worker_id,
            job.ticker,
            exc_info=True,
        )


def run_forever() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    worker_id = f"research-worker-{uuid.uuid4().hex[:8]}"
    log.info("research worker %s starting", worker_id)

    # Same resilience posture as api/main.py's own lifespan: a real
    # embedding-model load failure at startup is fatal here (unlike the
    # API, this process has exactly one job, and that job needs it), so
    # it's allowed to raise and let the container's restart policy
    # retry -- never a silent no-op loop that claims work it can't
    # actually finish.
    embedder = FastEmbedProvider()

    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)

    # IBKR TWS Migration, post-cutover cleanup (2026-09-01) -- this
    # process needs the SAME one-shared-connection ownership api/main.py's
    # own lifespan already establishes for the backend, for a real reason
    # found by auditing this worker against the now-production TWS
    # transport: _run_one_cycle below calls build_research_providers()
    # once per claimed job, which (with ibkr_provider="tws") builds a
    # fresh IBKRTWSProvider each time. Nothing ever shut those down, and
    # they cannot be garbage-collected either -- TWSConnectionManager's
    # own reader thread (providers/ibkr_tws_client.py) is started with
    # target=self.run, so the live thread holds a reference to the
    # manager for as long as the socket is open. The FIRST job would
    # therefore connect at this process's client id and hold it, and the
    # SECOND job would hit a real IB Gateway error 326 ("client id
    # already in use") against this worker's own orphaned connection.
    # Registering one shared provider here makes providers/factory.py's
    # _build_ibkr_transport return that single instance for every job
    # instead -- the exact mechanism, and the exact reason, api/main.py
    # already uses. Deliberately client-id 102 by deployment config
    # (docker-compose.yml's IBKR_TWS_RESEARCH_WORKER_CLIENT_ID), never
    # the backend's 101 or its health probe's 1001.
    shared_tws_provider: IBKRTWSProvider | None = None
    if settings.ibkr_provider.lower() == "tws":
        shared_tws_provider = IBKRTWSProvider(
            host=settings.ibkr_tws_host,
            port=settings.ibkr_tws_port,
            client_id=settings.ibkr_tws_client_id,
        )
        set_shared_tws_provider(shared_tws_provider)
        log.info(
            "research worker %s: shared TWS provider registered (client_id=%d)",
            worker_id,
            settings.ibkr_tws_client_id,
        )

    try:
        last_recovery = 0.0
        while not _shutdown_requested:
            try:
                last_recovery, found_work = _run_one_cycle(
                    settings, embedder, worker_id, last_recovery
                )
            except Exception:  # noqa: BLE001 -- boundary: the poll loop itself must survive an unexpected error, not crash the whole worker
                log.error(
                    "research worker %s: unexpected error in poll loop", worker_id, exc_info=True
                )
                found_work = False

            # Only sleep when the queue was genuinely empty (or a cycle
            # itself failed) -- immediately re-poll after finishing a real
            # item, since there may be more due candidates waiting; never
            # busy-poll when idle.
            if not found_work and not _shutdown_requested:
                time.sleep(POLL_INTERVAL_SECONDS)
    finally:
        # Unconditional, mirroring api/main.py's own shutdown block: an
        # un-closed TWS socket keeps this process's client id checked out
        # at the Gateway (and, on an un-handled exit, keeps a non-daemon-
        # joined reader thread blocked in recv() forever).
        if shared_tws_provider is not None:
            try:
                shared_tws_provider.shutdown()
            finally:
                set_shared_tws_provider(None)

    log.info("research worker %s stopped", worker_id)


def _run_one_cycle(settings, embedder, worker_id: str, last_recovery: float) -> tuple[float, bool]:
    """One full poll cycle: recovery sweep (if due), claim, process.
    Returns (updated last_recovery timestamp, whether real work was
    claimed) so the caller can decide whether to sleep."""
    db = SessionLocal()
    try:
        now_monotonic = time.monotonic()
        if now_monotonic - last_recovery >= RECOVERY_INTERVAL_SECONDS:
            result = recover_stale_running_jobs(db)
            if result.recovered_to_interrupted or result.permanently_failed:
                log.info(
                    "research worker %s: recovery pass reclaimed=%d permanently_failed=%d",
                    worker_id,
                    result.recovered_to_interrupted,
                    result.permanently_failed,
                )
            last_recovery = now_monotonic

        job = claim_next_preparation_job(db, worker_id)
        if job is None:
            return last_recovery, False

        log.info(
            "research worker %s: claimed ticker=%s job_id=%s attempt=%d",
            worker_id,
            job.ticker,
            job.id,
            job.attempt_count,
        )
        research_providers = build_research_providers(settings, embedder, db)
        _process_claimed_job(db, job, research_providers, worker_id)
        return last_recovery, True
    finally:
        db.close()


if __name__ == "__main__":
    run_forever()
