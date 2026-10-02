"""Runs background transformations: `python -m app.worker`.

One job at a time per process; run more processes (or containers) to go faster. SIGTERM or
SIGINT stops the worker once its current job is done. While the database is failing the worker
backs off and retries; a job it was running when that happened is taken over once its lease runs
out.
"""

import logging
import signal
import threading
import time

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.jobs import claim_next, fail, prune_finished, run_job
from app.models import Image, Job, User
from app.storage import Storage, get_storage

logger = logging.getLogger(__name__)

PRUNE_EVERY_SECONDS = 60 * 60
MAX_ERROR_BACKOFF_SECONDS = 30


class Worker:
    def __init__(
        self, sessions: sessionmaker[Session], storage: Storage, settings: Settings
    ) -> None:
        self._sessions = sessions
        self._storage = storage
        self._settings = settings

    def run_once(self) -> bool:
        """Claim and run one job; returns False when no job was waiting."""
        with self._sessions() as db:
            claim = claim_next(db, self._settings.job_lease_seconds)
        if claim is None:
            return False
        try:
            run_job(self._sessions, self._storage, self._settings, claim)
        except SQLAlchemyError:
            # Likely transient (the database restarting, a deadlock): the job is run again once
            # its lease runs out, and fails after JOB_MAX_ATTEMPTS claims if it keeps happening.
            raise
        except Exception:
            logger.exception("Job %s failed unexpectedly", claim.job_id)
            fail(self._sessions, claim, 500, "Internal error while processing the job")
        return True

    def prune(self) -> None:
        with self._sessions() as db:
            deleted = prune_finished(db, self._settings.job_retention_days)
        if deleted:
            logger.info(
                "Deleted %d finished jobs older than %d days",
                deleted,
                self._settings.job_retention_days,
            )

    def run(self, stop: threading.Event) -> None:
        """Run jobs until `stop` is set, waiting `JOB_POLL_SECONDS` between checks when idle."""
        if not self._wait_for_database(stop):
            return
        logger.info("Worker started")
        next_prune = 0.0
        failures = 0
        while not stop.is_set():
            try:
                if time.monotonic() >= next_prune:
                    self.prune()
                    next_prune = time.monotonic() + PRUNE_EVERY_SECONDS
                worked = self.run_once()
                failures = 0
            except SQLAlchemyError as exc:
                # Bounded, so that a long outage can't overflow the delay computation.
                failures = min(failures + 1, 32)
                delay = min(
                    self._settings.job_poll_seconds * 2**failures, MAX_ERROR_BACKOFF_SECONDS
                )
                logger.warning(
                    "Database error (%s); retrying in %gs", exc.__class__.__name__, delay
                )
                stop.wait(delay)
                continue
            if not worked:
                stop.wait(self._settings.job_poll_seconds)
        logger.info("Worker stopped")

    def _wait_for_database(self, stop: threading.Event) -> bool:
        """Wait until the database is reachable and migrated (the API container migrates it)."""
        logged = False
        while not stop.is_set():
            try:
                with self._sessions() as db:
                    # Every column of the tables jobs use, so a new worker doesn't start before
                    # the migrations that came with it have run.
                    db.execute(select(Job).limit(1))
                    db.execute(select(Image).limit(1))
                    db.execute(select(User).limit(1))
                return True
            except SQLAlchemyError as exc:
                if not logged:
                    logger.info(
                        "Waiting for the database to be migrated (%s)", exc.__class__.__name__
                    )
                    logged = True
                stop.wait(self._settings.job_poll_seconds)
        return False


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    stop = threading.Event()
    stop_on_signals(stop)
    Worker(get_sessionmaker(), get_storage(), get_settings()).run(stop)


def stop_on_signals(stop: threading.Event) -> None:
    """Set `stop` on SIGTERM or SIGINT."""

    def request_stop() -> None:
        logger.info("Stopping once the current job is done")
        stop.set()

    def handler(signum: int, frame: object) -> None:
        # Handlers run on the main thread between two bytecodes, possibly while it is inside
        # stop.wait() holding the lock that stop.set() needs, so set it from another thread.
        threading.Thread(target=request_stop, daemon=True).start()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


if __name__ == "__main__":
    main()
