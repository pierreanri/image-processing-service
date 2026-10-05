"""Webhooks: each user may register one URL (PUT /me/webhook), and is sent an event there each
time one of their jobs finishes.

Events are an outbox: the transaction that marks a job succeeded or failed also inserts its
event into webhook_deliveries (queue_job_event), so a job never finishes without its event, and
a run that is rolled back or loses its claim leaves none. Workers then deliver events at least
once, retrying failures for a day.

Each endpoint has a gate, `webhook_endpoints.next_attempt_at`: when a sender should next look at
it, NULL while nothing is pending. It is only changed while holding the endpoint's row lock, so
whoever queues an event, and every sender, sees the others' changes.

Senders (threads of the worker processes) claim an endpoint whose gate is due, with a lease:
one request at a time per endpoint, across all workers. A claim also schedules the event's next
attempt, as if this one were to fail, so an attempt whose outcome is never recorded (a crash,
a lease taken over, the URL changed meanwhile) is retried on schedule, never at once. After a
failure the endpoint is paused too, so a dead endpoint gets about one attempt an hour however
many events wait for it.

Requests are signed as Standard Webhooks (https://www.standardwebhooks.com) specifies, with a
secret per user derived from JWT_SECRET and a random salt: the database alone reveals no secret.
"""

import base64
import hmac
import logging
import secrets
import ssl
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
from urllib.parse import urlsplit

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from app import webhook_client
from app.config import Settings, get_settings
from app.models import Image, Job, WebhookDelivery, WebhookEndpoint
from app.routers.views import UrlSource, job_data
from app.schemas import WebhookEvent
from app.webhook_client import INTERNAL_ERROR, Outcome

logger = logging.getLogger(__name__)

# An event's attempts are 30 s apart at first, then twice as far each time, at most an hour.
RETRY_BASE_SECONDS = 30
# Also the longest an endpoint is paused after a failure.
MAX_RETRY_DELAY_SECONDS = 60 * 60
# How long after its job finished an event is given up on. At most a day, the shortest
# JOB_RETENTION_DAYS: an event is deleted with its job.
RETRY_FOR = timedelta(hours=24)
# How long a sender holds an endpoint: an attempt (WEBHOOK_TIMEOUT_SECONDS, at most 30 s) and
# recording it must fit, with room for a slow DNS resolver. After that another sender may take
# the endpoint over, and resends the event it was sending.
LEASE_SECONDS = 120
# How long the previous secret keeps signing requests after a rotation, by default and at most.
DEFAULT_ROTATION_GRACE_SECONDS = 24 * 60 * 60
MAX_ROTATION_GRACE_SECONDS = 7 * 24 * 60 * 60

SECRET_PREFIX = "whsec_"
SALT_BYTES = 16
# Gives webhook secrets a key of their own, apart from JWTs and share links.
_KEY_LABEL = b"imgsvc webhook secrets v1"

# The only messages last_error holds: nothing a receiver or a DNS server says is shown.
WEBHOOK_REMOVED = "Webhook removed"
GAVE_UP = "Not delivered within 24 hours"


class WebhookSecrets:
    """Derives users' signing secrets, and signs requests with them."""

    def __init__(self, secret: str, *, clock: Callable[[], float] = time.time) -> None:
        self._key = hmac.digest(secret.encode(), _KEY_LABEL, "sha256")
        self._clock = clock

    @staticmethod
    def new_salt() -> bytes:
        return secrets.token_bytes(SALT_BYTES)

    def key(self, user_id: uuid.UUID, salt: bytes) -> bytes:
        return hmac.digest(self._key, user_id.bytes + salt, "sha256")

    def secret(self, user_id: uuid.UUID, salt: bytes) -> str:
        """The secret as users are given it: `whsec_` and the base64 of the key."""
        return SECRET_PREFIX + base64.b64encode(self.key(user_id, salt)).decode()

    def headers(
        self, event_id: uuid.UUID, body: bytes, user_id: uuid.UUID, salts: Sequence[bytes]
    ) -> dict[str, str]:
        """The webhook-id, webhook-timestamp and webhook-signature headers of a request: one
        signature per salt, in order."""
        timestamp = str(int(self._clock()))
        signatures = (
            sign(self.key(user_id, salt), str(event_id), timestamp, body) for salt in salts
        )
        return {
            "webhook-id": str(event_id),
            "webhook-timestamp": timestamp,
            "webhook-signature": " ".join(signatures),
        }


def sign(key: bytes, message_id: str, timestamp: str, body: bytes) -> str:
    """A Standard Webhooks signature (`v1,` and the base64 HMAC-SHA256 of the id, the timestamp
    and the body)."""
    message = f"{message_id}.{timestamp}.".encode() + body
    return "v1," + base64.b64encode(hmac.digest(key, message, "sha256")).decode()


@lru_cache
def get_webhook_secrets() -> WebhookSecrets:
    return WebhookSecrets(get_settings().jwt_secret)


def queue_job_event(db: Session, job_id: uuid.UUID, owner_id: uuid.UUID, event_type: str) -> None:
    """Queue the event of a job that has just finished, in the transaction that finishes it, if
    its owner has a webhook.

    Locks the owner's endpoint row until the transaction ends: callers lock it after anything
    else they lock (see the lock order in the README).
    """
    # Due now, or once a pause ends (GREATEST and LEAST ignore NULLs). While a sender holds the
    # endpoint, the gate is its lease: the sender reschedules when it is done, and then sees
    # this event, whether this transaction committed first or waited for the sender's.
    wake = func.least(
        WebhookEndpoint.next_attempt_at,
        func.greatest(func.now(), WebhookEndpoint.paused_until),
    )
    held = db.execute(
        update(WebhookEndpoint)
        .where(WebhookEndpoint.user_id == owner_id)
        .values(
            next_attempt_at=case(
                (
                    or_(
                        WebhookEndpoint.claim_token.is_(None),
                        WebhookEndpoint.next_attempt_at.is_(None),
                    ),
                    wake,
                ),
                else_=WebhookEndpoint.next_attempt_at,
            )
        )
        .returning(WebhookEndpoint.user_id)
        .execution_options(synchronize_session=False)
    ).first()
    if held is None:
        return  # No webhook: no event.
    db.execute(
        pg_insert(WebhookDelivery)
        .values(id=uuid.uuid4(), job_id=job_id, user_id=owner_id, type=event_type)
        # Only one run can finish a job; this is a safeguard, never a reason to roll back.
        .on_conflict_do_nothing(index_elements=["job_id"])
    )


def reschedule(db: Session, user_id: uuid.UUID) -> None:
    """Set the endpoint's gate from its pending deliveries: when the earliest is due, but not
    before its pause ends, nor before now (so an endpoint just served waits for its turn behind
    those already due). Only call it while holding the endpoint's row lock."""
    earliest = (
        select(func.min(WebhookDelivery.next_attempt_at))
        .where(WebhookDelivery.user_id == user_id, WebhookDelivery.state == "pending")
        .scalar_subquery()
    )
    db.execute(
        update(WebhookEndpoint)
        .where(WebhookEndpoint.user_id == user_id)
        .values(
            next_attempt_at=case(
                (earliest.is_(None), None),
                else_=func.greatest(earliest, WebhookEndpoint.paused_until, func.now()),
            )
        )
        .execution_options(synchronize_session=False)
    )


def retry_delay(attempts: int) -> int:
    """Seconds from an event's attempt number `attempts` (from 1) to its next one, should it
    fail; also how long an endpoint is paused after that many failures in a row."""
    return min(RETRY_BASE_SECONDS * 2 ** min(attempts - 1, 20), MAX_RETRY_DELAY_SECONDS)


@dataclass(frozen=True)
class DeliveryClaim:
    """An attempt a sender may make: it holds the endpoint until it records the outcome."""

    user_id: uuid.UUID
    token: uuid.UUID
    delivery_id: uuid.UUID
    type: str
    # When the job finished.
    created_at: datetime
    # Including this one.
    attempts: int
    # Detached from their session, for the payload.
    job: Job = field(repr=False)
    result: Image | None = field(repr=False)
    # Neither appears in logs: the URL may carry a credential for the receiver, and the salts
    # with JWT_SECRET make the signing secrets.
    url: str = field(repr=False)
    salts: tuple[bytes, ...] = field(repr=False)


def claim_delivery(db: Session) -> DeliveryClaim | None:
    """Claim the endpoint that has been due longest and its event due first, in one short
    transaction per endpoint looked at. Returns None when no endpoint has an event due."""
    # An endpoint was due with no event due (its due events were just given up on, say): it is
    # rescheduled and the next one looked at, a few at most before the caller waits a while.
    for _ in range(10):
        row = db.execute(
            select(WebhookEndpoint, func.now())
            .where(WebhookEndpoint.next_attempt_at <= func.now())
            .order_by(WebhookEndpoint.next_attempt_at)
            .limit(1)
            .with_for_update(skip_locked=True, key_share=True)
        ).one_or_none()
        if row is None:
            db.rollback()
            return None
        endpoint, now = row
        user_id = endpoint.user_id
        _give_up_on_expired(db, user_id)
        delivery = db.scalar(
            select(WebhookDelivery)
            .where(
                WebhookDelivery.user_id == user_id,
                WebhookDelivery.state == "pending",
                WebhookDelivery.next_attempt_at <= func.now(),
            )
            .order_by(WebhookDelivery.next_attempt_at, WebhookDelivery.created_at)
            .limit(1)
        )
        if delivery is None:
            # Also ends the claim of a sender whose lease ran out: whoever holds the endpoint
            # next must find no stale token, or queued events would leave its gate alone.
            db.execute(
                update(WebhookEndpoint)
                .where(WebhookEndpoint.user_id == user_id)
                .values(claim_token=None)
                .execution_options(synchronize_session=False)
            )
            reschedule(db, user_id)
            db.commit()
            continue

        token = uuid.uuid4()
        attempts = delivery.attempts + 1
        db.execute(
            update(WebhookEndpoint)
            .where(WebhookEndpoint.user_id == user_id)
            .values(
                claim_token=token,
                next_attempt_at=func.now() + timedelta(seconds=LEASE_SECONDS),
            )
            .execution_options(synchronize_session=False)
        )
        db.execute(
            update(WebhookDelivery)
            .where(WebhookDelivery.id == delivery.id)
            .values(
                attempts=attempts,
                last_attempt_at=func.now(),
                next_attempt_at=func.now() + timedelta(seconds=retry_delay(attempts)),
            )
            .execution_options(synchronize_session=False)
        )
        job = db.get(Job, delivery.job_id)
        if job is None:  # Deleted (and the event with it) since the event was read.
            db.rollback()
            continue
        result = db.get(Image, job.result_image_id) if job.result_image_id else None
        salts = (endpoint.secret_salt,)
        previous_expires = endpoint.previous_secret_expires_at
        if previous_expires is not None and previous_expires > now:
            salts += (endpoint.previous_secret_salt,)
        claim = DeliveryClaim(
            user_id=user_id,
            token=token,
            delivery_id=delivery.id,
            type=delivery.type,
            created_at=delivery.created_at,
            attempts=attempts,
            job=job,
            result=result,
            url=endpoint.url,
            salts=salts,
        )
        db.expunge(job)
        if result is not None:
            db.expunge(result)
        db.commit()
        return claim
    return None


def _give_up_on_expired(db: Session, user_id: uuid.UUID) -> None:
    db.execute(
        update(WebhookDelivery)
        .where(
            WebhookDelivery.user_id == user_id,
            WebhookDelivery.state == "pending",
            WebhookDelivery.created_at <= func.now() - RETRY_FOR,
        )
        .values(
            state="failed",
            next_attempt_at=None,
            finished_at=func.now(),
            # The last attempt's error says more, if there was one.
            last_error=func.coalesce(WebhookDelivery.last_error, GAVE_UP),
        )
        .execution_options(synchronize_session=False)
    )


def record_attempt(db: Session, claim: DeliveryClaim, outcome: Outcome) -> bool:
    """Record an attempt's outcome and release the endpoint, unless the claim no longer holds it
    (another sender took it over, or the webhook was removed) or the URL changed meanwhile: the
    outcome then counts for nothing. Returns whether it was recorded."""
    released = db.execute(
        update(WebhookEndpoint)
        .where(WebhookEndpoint.user_id == claim.user_id, WebhookEndpoint.claim_token == claim.token)
        .values(claim_token=None)
        .returning(WebhookEndpoint.url, WebhookEndpoint.consecutive_failures)
        .execution_options(synchronize_session=False)
    ).one_or_none()
    if released is None:
        db.rollback()
        return False
    if released.url != claim.url:
        # The event goes to the new URL at the time the claim set for its next attempt.
        reschedule(db, claim.user_id)
        db.commit()
        return False

    if outcome.delivered:
        breaker = {"consecutive_failures": 0, "paused_until": None}
    elif outcome.internal:
        breaker = {}  # Not the endpoint's fault.
    else:
        failures = released.consecutive_failures + 1
        pause = max(retry_delay(failures), min(outcome.retry_after or 0, MAX_RETRY_DELAY_SECONDS))
        breaker = {
            "consecutive_failures": failures,
            "paused_until": func.now() + timedelta(seconds=pause),
        }
    if breaker:
        db.execute(
            update(WebhookEndpoint)
            .where(WebhookEndpoint.user_id == claim.user_id)
            .values(**breaker)
            .execution_options(synchronize_session=False)
        )

    if outcome.delivered:
        values = {
            "state": "delivered",
            "next_attempt_at": None,
            "finished_at": func.now(),
            "last_error": None,
        }
    else:
        # The next attempt is when the claim scheduled it, unless that is too late.
        too_late = WebhookDelivery.next_attempt_at > WebhookDelivery.created_at + RETRY_FOR
        values = {
            "state": case((too_late, "failed"), else_="pending"),
            "next_attempt_at": case((too_late, None), else_=WebhookDelivery.next_attempt_at),
            "finished_at": case((too_late, func.now()), else_=None),
            "last_error": outcome.error,
        }
    # Unless it was canceled or given up on meanwhile.
    db.execute(
        update(WebhookDelivery)
        .where(WebhookDelivery.id == claim.delivery_id, WebhookDelivery.state == "pending")
        .values(last_status_code=outcome.status_code, **values)
        .execution_options(synchronize_session=False)
    )
    reschedule(db, claim.user_id)
    db.commit()
    return True


def event_body(claim: DeliveryClaim, urls: UrlSource) -> bytes:
    """The request body: the event, with the job as GET /jobs/{id} describes it now."""
    event = WebhookEvent(
        id=claim.delivery_id,
        type=claim.type,
        timestamp=claim.created_at,
        data=job_data(claim.job, urls, claim.result),
    )
    return event.model_dump_json().encode()


class WebhookSender:
    """Delivers events, one attempt per run_once(); any number of threads may share one."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        settings: Settings,
        secrets: WebhookSecrets,
        urls: UrlSource,
        *,
        resolve: webhook_client.Resolver = webhook_client.system_resolver,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._sessions = sessions
        self._settings = settings
        self._secrets = secrets
        self._urls = urls
        # For tests: a resolver, and a TLS context (shared by every thread using this sender).
        self._resolve = resolve
        self._ssl_context = ssl_context

    def run_once(self) -> bool:
        """Make one delivery attempt; returns False when no event was due."""
        with self._sessions() as db:
            claim = claim_delivery(db)
        if claim is None:
            return False
        try:
            body = event_body(claim, self._urls)
            headers = self._secrets.headers(claim.delivery_id, body, claim.user_id, claim.salts)
            outcome = webhook_client.post(
                claim.url,
                headers,
                body,
                timeout=self._settings.webhook_timeout_seconds,
                allow_private=self._settings.webhook_allow_private_networks,
                resolve=self._resolve,
                ssl_context=self._ssl_context,
            )
        except Exception:
            logger.exception(
                "Webhook event %s (job %s) could not be sent", claim.delivery_id, claim.job.id
            )
            outcome = Outcome(error=INTERNAL_ERROR, internal=True)
        with self._sessions() as db:
            recorded = record_attempt(db, claim, outcome)
        # The host only: the URL's path and query may carry a credential for the receiver.
        host = urlsplit(claim.url).hostname
        if not recorded:
            logger.info(
                "Webhook event %s (job %s, user %s): attempt %d to %s counts for nothing (the "
                "webhook was changed or removed meanwhile, or the lease ran out)",
                claim.delivery_id,
                claim.job.id,
                claim.user_id,
                claim.attempts,
                host,
            )
        elif outcome.delivered:
            logger.info(
                "Webhook event %s (job %s, user %s) delivered to %s (attempt %d): HTTP %d",
                claim.delivery_id,
                claim.job.id,
                claim.user_id,
                host,
                claim.attempts,
                outcome.status_code,
            )
        else:
            logger.info(
                "Webhook event %s (job %s, user %s) not delivered to %s (attempt %d): %s",
                claim.delivery_id,
                claim.job.id,
                claim.user_id,
                host,
                claim.attempts,
                outcome.error,
            )
        return True
