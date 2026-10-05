"""Webhooks: each user may register one URL (PUT /me/webhook), and is sent an event there each
time one of their jobs finishes.

Events are an outbox: the transaction that marks a job succeeded or failed also inserts its
event into webhook_deliveries (queue_job_event), so a job never finishes without its event, and
a run that is rolled back or loses its claim leaves none. Workers then deliver events at least
once, retrying failures for a day.

Each endpoint has a gate, `webhook_endpoints.next_attempt_at`: when a sender should next look at
it, NULL while nothing is pending. It is only changed while holding the endpoint's row lock, so
whoever queues an event, and every sender, sees the others' changes.

Requests are signed as Standard Webhooks (https://www.standardwebhooks.com) specifies, with a
secret per user derived from JWT_SECRET and a random salt: the database alone reveals no secret.
"""

import base64
import hmac
import secrets
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import timedelta
from functools import lru_cache

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import WebhookDelivery, WebhookEndpoint

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
        message = f"{event_id}.{timestamp}.".encode() + body
        signatures = (
            "v1," + base64.b64encode(hmac.digest(key, message, "sha256")).decode()
            for key in (self.key(user_id, salt) for salt in salts)
        )
        return {
            "webhook-id": str(event_id),
            "webhook-timestamp": timestamp,
            "webhook-signature": " ".join(signatures),
        }


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
