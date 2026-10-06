"""Runs background transformations, and sends webhooks: `python -m app.worker`.

One job at a time per process; run more processes (or containers) to go faster. Beside the
thread running jobs, WEBHOOK_SENDERS threads send webhook events (see app/webhooks.py), so a slow
webhook never delays a job. SIGTERM or SIGINT stops the worker once its current job, and each
sender's current attempt, is done. While the database is failing the worker backs off and
retries; a job it was running when that happened is taken over once its lease runs out.
"""

import logging
import signal
import threading
import time
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.jobs import claim_next, fail, prune_finished, run_job
from app.models import Image, Job, User, WebhookDelivery, WebhookEndpoint
from app.storage import Storage, get_storage
from app.webhooks import WebhookSender, get_webhook_secrets, give_up_on_expired_events

logger = logging.getLogger(__name__)

PRUNE_EVERY_SECONDS = 60 * 60
MAX_ERROR_BACKOFF_SECONDS = 30


class Worker:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        storage: Storage,
        settings: Settings,
        sender: WebhookSender | None = None,
    ) -> None:
        self._sessions = sessions
        self._storage = storage
        self._settings = settings
        # None: this worker sends no webhooks.
        self._sender = sender

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
        # First, so that the jobs of those events can go too.
        given_up = give_up_on_expired_events(self._sessions)
        if given_up:
            logger.info("Gave up on %d webhook events not delivered within 24 hours", given_up)
        with self._sessions() as db:
            deleted = prune_finished(db, self._settings.job_retention_days)
        if deleted:
            logger.info(
                "Deleted %d finished jobs older than %d days",
                deleted,
                self._settings.job_retention_days,
            )

    def run(self, stop: threading.Event) -> None:
        """Run jobs (and send webhooks) until `stop` is set, waiting `JOB_POLL_SECONDS` between
        checks when idle."""
        if not self._wait_for_database(stop):
            return
        senders = self._start_senders(stop)
        logger.info("Worker started")
        next_prune = 0.0

        def step() -> bool:
            nonlocal next_prune
            if time.monotonic() >= next_prune:
                self.prune()
                next_prune = time.monotonic() + PRUNE_EVERY_SECONDS
            return self.run_once()

        run_loop(stop, step, self._settings.job_poll_seconds, (SQLAlchemyError,))
        # Each finishes and records its attempt, which ends within WEBHOOK_TIMEOUT_SECONDS.
        deadline = time.monotonic() + self._settings.webhook_timeout_seconds + 5
        for thread in senders:
            thread.join(max(deadline - time.monotonic(), 0))
        if busy := [thread.name for thread in senders if thread.is_alive()]:
            # Their endpoints are taken over once their leases run out.
            logger.warning("Stopping without waiting any longer for %s", ", ".join(busy))
        logger.info("Worker stopped")

    def _start_senders(self, stop: threading.Event) -> list[threading.Thread]:
        if self._sender is None:
            return []
        senders = [
            threading.Thread(
                target=run_loop,
                args=(stop, self._sender.run_once, self._settings.job_poll_seconds, (Exception,)),
                name=f"webhook-sender-{number}",
                # Should one still be waiting for a resolver when the worker stops.
                daemon=True,
            )
            for number in range(1, self._settings.webhook_senders + 1)
        ]
        for thread in senders:
            thread.start()
        return senders

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
                    db.execute(select(WebhookEndpoint).limit(1))
                    db.execute(select(WebhookDelivery).limit(1))
                return True
            except SQLAlchemyError as exc:
                if not logged:
                    logger.info(
                        "Waiting for the database to be migrated (%s)", exc.__class__.__name__
                    )
                    logged = True
                stop.wait(self._settings.job_poll_seconds)
        return False


def run_loop(
    stop: threading.Event,
    step: Callable[[], bool],
    poll_seconds: float,
    retry_on: tuple[type[Exception], ...],
) -> None:
    """Call `step` until `stop` is set, waiting `poll_seconds` after a call that found nothing
    to do (returned False). After an exception in `retry_on`, wait longer each time it happens
    again in a row, up to MAX_ERROR_BACKOFF_SECONDS."""
    failures = 0
    while not stop.is_set():
        try:
            worked = step()
            failures = 0
        except retry_on as exc:
            # Bounded, so that a long outage can't overflow the delay computation.
            failures = min(failures + 1, 32)
            delay = min(poll_seconds * 2**failures, MAX_ERROR_BACKOFF_SECONDS)
            if isinstance(exc, SQLAlchemyError):
                logger.warning(
                    "Database error (%s); retrying in %gs", exc.__class__.__name__, delay
                )
            else:
                # A bug: logged with its stack, without ending the thread.
                logger.exception("Unexpected error; retrying in %gs", delay)
            stop.wait(delay)
            continue
        if not worked:
            stop.wait(poll_seconds)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    stop = threading.Event()
    stop_on_signals(stop)
    sessions, settings = get_sessionmaker(), get_settings()
    Worker(sessions, get_storage(), settings, webhook_sender(sessions, settings)).run(stop)


def webhook_sender(sessions: sessionmaker[Session], settings: Settings) -> WebhookSender | None:
    """The sender this worker's settings call for, if any."""
    if not settings.public_base_url:
        logger.info("This worker sends no webhooks: PUBLIC_BASE_URL is not set")
        return None
    if not settings.webhook_senders:
        logger.info("This worker sends no webhooks: WEBHOOK_SENDERS is 0")
        return None
    if settings.webhook_allow_private_networks:
        logger.warning(
            "WEBHOOK_ALLOW_PRIVATE_NETWORKS is on: webhooks may be sent to private networks "
            "(for development only)"
        )
    # Only to build the API's URLs: nothing is served.
    from app.main import app
    from app.routers.views import PublicUrls

    return WebhookSender(
        sessions, settings, get_webhook_secrets(), PublicUrls(app, settings.public_base_url)
    )


def stop_on_signals(stop: threading.Event) -> None:
    """Set `stop` on SIGTERM or SIGINT."""

    def request_stop() -> None:
        logger.info("Stopping once the current job (and webhook attempts) are done")
        stop.set()

    def handler(signum: int, frame: object) -> None:
        # Handlers run on the main thread between two bytecodes, possibly while it is inside
        # stop.wait() holding the lock that stop.set() needs, so set it from another thread.
        threading.Thread(target=request_stop, daemon=True).start()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


if __name__ == "__main__":
    main()
