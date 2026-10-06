"""Your webhook: where events about your jobs are sent (see app/webhooks.py)."""

import uuid
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.deps import CurrentUser, DbSession, SettingsDep, WebhookSecretsDep
from app.models import WebhookDelivery, WebhookEndpoint
from app.schemas import (
    RotateWebhookSecretRequest,
    WebhookEvent,
    WebhookIn,
    WebhookOut,
    WebhookSecretOut,
)
from app.webhook_client import check_webhook_url
from app.webhooks import WEBHOOK_REMOVED, reschedule

router = APIRouter(prefix="/me/webhook", tags=["webhooks"])

_NOT_SET = "No webhook is set"
_NOT_SET_RESPONSE = {404: {"description": _NOT_SET}}
# Responses that carry a signing secret.
_NO_STORE = {"Cache-Control": "no-store"}


@router.put(
    "",
    responses={
        201: {
            "model": WebhookSecretOut,
            "description": "The webhook was set: the response carries its signing secret, which "
            "is shown only this once.",
            "headers": {
                "Location": {"description": "GET /me/webhook.", "schema": {"type": "string"}}
            },
        },
        503: {"description": "Webhooks are not enabled on this server."},
    },
)
def set_webhook(
    body: WebhookIn,
    request: Request,
    user: CurrentUser,
    db: DbSession,
    settings: SettingsDep,
    secrets: WebhookSecretsDep,
) -> WebhookOut:
    """Set where events are sent each time one of your background transformations (POST
    /images/{id}/transform with `Prefer: respond-async`) finishes: `job.succeeded` or
    `job.failed` (see the `webhooks` section of this document).

    The first time, the answer is 201 with the secret requests are signed with, shown only then
    (POST /me/webhook/secret issues a new one). Changing the URL keeps the secret: events not
    yet delivered go to the new URL. Setting the same URL again changes nothing.

    No request is sent to the URL now. Its host must be public: private, loopback and similar
    addresses are refused, here and whenever an event is sent.
    """
    if not settings.public_base_url:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Webhooks are not enabled on this server"
        )
    try:
        url = check_webhook_url(body.url, settings.webhook_allow_private_networks)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"The webhook URL {exc}"
        ) from None

    # Twice at most: a webhook deleted between the two statements is inserted again.
    for _ in range(2):
        salt = secrets.new_salt()
        created = db.scalar(
            pg_insert(WebhookEndpoint)
            .values(user_id=user.id, url=url, secret_salt=salt)
            .on_conflict_do_nothing(index_elements=["user_id"])
            .returning(WebhookEndpoint.user_id)
        )
        if created is not None:
            out = WebhookSecretOut(
                **dict(_webhook_out(db, user.id)), secret=secrets.secret(user.id, salt)
            )
            db.commit()
            return JSONResponse(
                out.model_dump(mode="json"),
                status_code=status.HTTP_201_CREATED,
                headers={"Location": str(request.url_for("get_webhook")), **_NO_STORE},
            )
        endpoint = db.scalar(
            select(WebhookEndpoint)
            .where(WebhookEndpoint.user_id == user.id)
            .with_for_update(key_share=True)
        )
        if endpoint is not None:
            break
    else:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "The webhook was changed at the same time; try again"
        )

    if endpoint.url != url:
        # A new URL starts with a clean record: the pause ends, so events already due go at
        # once, but none before the time of its next attempt. A sender holding the endpoint
        # keeps it (and reschedules when done); its attempt, which went to the old URL, then
        # counts for nothing.
        reschedule(
            db,
            user.id,
            unless_claimed=True,
            url=url,
            updated_at=func.now(),
            consecutive_failures=0,
            paused_until=None,
        )
    out = _webhook_out(db, user.id)
    db.commit()
    return out


@router.get("", responses=_NOT_SET_RESPONSE)
def get_webhook(user: CurrentUser, db: DbSession) -> WebhookOut:
    """Your webhook, and how deliveries to it are going. Its secret is never shown again."""
    out = _webhook_out(db, user.id)
    if out is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_SET)
    return out


@router.delete("", status_code=status.HTTP_204_NO_CONTENT, responses=_NOT_SET_RESPONSE)
def delete_webhook(user: CurrentUser, db: DbSession) -> None:
    """Stop sending events: those not yet delivered are canceled (an attempt already under way
    may still arrive). Setting a webhook again issues a new secret."""
    deleted = db.scalar(
        delete(WebhookEndpoint)
        .where(WebhookEndpoint.user_id == user.id)
        .returning(WebhookEndpoint.user_id)
    )
    if deleted is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_SET)
    # A statement of its own: it also sees events queued by jobs that finished while this waited
    # for the endpoint's lock.
    db.execute(
        update(WebhookDelivery)
        .where(WebhookDelivery.user_id == user.id, WebhookDelivery.state == "pending")
        .values(
            state="canceled",
            next_attempt_at=None,
            finished_at=func.now(),
            last_error=WEBHOOK_REMOVED,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()


@router.post("/secret", responses=_NOT_SET_RESPONSE)
def rotate_webhook_secret(
    response: Response,
    user: CurrentUser,
    db: DbSession,
    secrets: WebhookSecretsDep,
    body: RotateWebhookSecretRequest | None = None,
) -> WebhookSecretOut:
    """Issue a new signing secret, shown only in this response.

    Requests are signed with both secrets for `previous_secret_expires_in` seconds (1 day by
    default), so your endpoint can switch over without rejecting any; 0 stops the previous one at
    once, as for a leaked secret. Only the latest previous secret is kept.
    """
    grace = (body or RotateWebhookSecretRequest()).previous_secret_expires_in
    salt = secrets.new_salt()
    rotated = db.scalar(
        update(WebhookEndpoint)
        .where(WebhookEndpoint.user_id == user.id)
        .values(
            # The value before this update.
            previous_secret_salt=WebhookEndpoint.secret_salt if grace else None,
            previous_secret_expires_at=func.now() + timedelta(seconds=grace) if grace else None,
            secret_salt=salt,
            secret_created_at=func.now(),
        )
        .returning(WebhookEndpoint.user_id)
        .execution_options(synchronize_session=False)
    )
    if rotated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_SET)
    out = WebhookSecretOut(**dict(_webhook_out(db, user.id)), secret=secrets.secret(user.id, salt))
    db.commit()
    response.headers.update(_NO_STORE)
    return out


def _webhook_out(db: Session, user_id: uuid.UUID) -> WebhookOut | None:
    pending = (
        select(func.count())
        .where(WebhookDelivery.user_id == user_id, WebhookDelivery.state == "pending")
        .scalar_subquery()
    )
    row = db.execute(
        select(
            WebhookEndpoint.url,
            WebhookEndpoint.created_at,
            WebhookEndpoint.updated_at,
            WebhookEndpoint.secret_created_at,
            WebhookEndpoint.previous_secret_expires_at,
            WebhookEndpoint.consecutive_failures,
            WebhookEndpoint.paused_until,
            pending.label("pending"),
            func.statement_timestamp().label("now"),
        ).where(WebhookEndpoint.user_id == user_id)
    ).one_or_none()
    if row is None:
        return None
    return WebhookOut(
        url=row.url,
        created_at=row.created_at,
        updated_at=row.updated_at,
        secret_created_at=row.secret_created_at,
        previous_secret_expires_at=_if_after(row.previous_secret_expires_at, row.now),
        pending_deliveries=row.pending,
        consecutive_failures=row.consecutive_failures,
        paused_until=_if_after(row.paused_until, row.now),
    )


def _if_after(moment: datetime | None, now: datetime) -> datetime | None:
    return moment if moment is not None and moment > now else None


# --- The events, as the OpenAPI document's `webhooks` (included in app/main.py) ---------------

events = APIRouter()

WebhookIdHeader = Annotated[
    uuid.UUID,
    Header(
        alias="webhook-id",
        description="The event's id (also its `id`): the same on every attempt.",
    ),
]
WebhookTimestampHeader = Annotated[
    int,
    Header(
        alias="webhook-timestamp",
        description="When this attempt was signed, in Unix seconds: reject requests more than a "
        "few minutes from your clock.",
    ),
]
WebhookSignatureHeader = Annotated[
    str,
    Header(
        alias="webhook-signature",
        description="`v1,` and the base64 HMAC-SHA256 of `{webhook-id}.{webhook-timestamp}.` "
        "followed by the raw body, keyed with the base64-decoded secret (after `whsec_`). "
        "During a rotation, a second signature follows after a space.",
    ),
]
_EVENT_RESPONSES = {
    200: {
        "description": "Any 2xx status, within WEBHOOK_TIMEOUT_SECONDS, marks the event "
        "delivered. Anything else (including redirects) is retried for 24 hours."
    }
}


@events.post("job.succeeded", responses=_EVENT_RESPONSES)
def job_succeeded(
    event: WebhookEvent,
    webhook_id: WebhookIdHeader,
    webhook_timestamp: WebhookTimestampHeader,
    webhook_signature: WebhookSignatureHeader,
) -> None:
    """Sent to your webhook when one of your background transformations succeeds:
    `data.result` is the new image."""


@events.post("job.failed", responses=_EVENT_RESPONSES)
def job_failed(
    event: WebhookEvent,
    webhook_id: WebhookIdHeader,
    webhook_timestamp: WebhookTimestampHeader,
    webhook_signature: WebhookSignatureHeader,
) -> None:
    """Sent to your webhook when one of your background transformations fails: `data.error`
    says why."""
