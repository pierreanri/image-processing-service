"""Your URL signing key: what your backend signs signed URLs with (see app/signed_urls.py)."""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.deps import CurrentUser, DbSession, UrlSignerDep
from app.models import UrlSigningKey
from app.schemas import UrlSigningKeyOut, UrlSigningKeySecretOut
from app.signed_urls import UrlSigner

router = APIRouter(prefix="/me/url-signing-key", tags=["signed URLs"])

_NOT_SET = "No URL signing key"
_NOT_SET_RESPONSE = {404: {"description": _NOT_SET}}
# Responses that carry the key.
_NO_STORE = {"Cache-Control": "no-store"}


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {
            "description": "The key was created; the response is the only one that shows it.",
            "headers": {
                "Location": {
                    "description": "GET /me/url-signing-key.",
                    "schema": {"type": "string"},
                }
            },
        },
        409: {"description": "You already have a key."},
    },
)
def create_url_signing_key(
    request: Request, user: CurrentUser, db: DbSession, signer: UrlSignerDep
) -> UrlSigningKeySecretOut:
    """Create the key your backend signs URLs with (see the README), shown only in this
    response. An account has one key: replacing it is POST /me/url-signing-key/rotate, so that
    calling this again can never end your URLs."""
    key_id = signer.new_key_id()
    created_at = db.scalar(
        pg_insert(UrlSigningKey)
        .values(user_id=user.id, key_id=key_id)
        .on_conflict_do_nothing(index_elements=["user_id"])
        .returning(UrlSigningKey.created_at)
    )
    if created_at is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "You already have a URL signing key; POST /me/url-signing-key/rotate replaces it",
        )
    db.commit()
    out = _key_out(request, signer, key_id, created_at)
    return JSONResponse(
        out.model_dump(mode="json"),
        status_code=status.HTTP_201_CREATED,
        headers={"Location": str(request.url_for("get_url_signing_key")), **_NO_STORE},
    )


@router.get("", responses=_NOT_SET_RESPONSE)
def get_url_signing_key(request: Request, user: CurrentUser, db: DbSession) -> UrlSigningKeyOut:
    """Your key's id and the prefix of the URLs it signs. The key itself is never shown again."""
    row = db.execute(
        select(UrlSigningKey.key_id, UrlSigningKey.created_at).where(
            UrlSigningKey.user_id == user.id
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_SET)
    return UrlSigningKeyOut(
        key_id=row.key_id, url_prefix=_url_prefix(request, row.key_id), created_at=row.created_at
    )


@router.post("/rotate", responses=_NOT_SET_RESPONSE)
def rotate_url_signing_key(
    request: Request, response: Response, user: CurrentUser, db: DbSession, signer: UrlSignerDep
) -> UrlSigningKeySecretOut:
    """Replace your key with a new one, shown only in this response. Every URL signed with the
    previous key stops working at once (copies a CDN or browser already holds expire with their
    caching, see the README)."""
    key_id = signer.new_key_id()
    created_at = db.scalar(
        update(UrlSigningKey)
        .where(UrlSigningKey.user_id == user.id)
        .values(key_id=key_id, created_at=func.now())
        .returning(UrlSigningKey.created_at)
        .execution_options(synchronize_session=False)
    )
    if created_at is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_SET)
    db.commit()
    response.headers.update(_NO_STORE)
    return _key_out(request, signer, key_id, created_at)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT, responses=_NOT_SET_RESPONSE)
def delete_url_signing_key(user: CurrentUser, db: DbSession) -> None:
    """Delete your key: every URL it signed stops working."""
    deleted = db.scalar(
        delete(UrlSigningKey)
        .where(UrlSigningKey.user_id == user.id)
        .returning(UrlSigningKey.user_id)
    )
    if deleted is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_SET)
    db.commit()


def _url_prefix(request: Request, key_id: str) -> str:
    return f"{request.base_url}signed/{key_id}/"


def _key_out(
    request: Request, signer: UrlSigner, key_id: str, created_at: datetime
) -> UrlSigningKeySecretOut:
    return UrlSigningKeySecretOut(
        key_id=key_id,
        url_prefix=_url_prefix(request, key_id),
        created_at=created_at,
        key=signer.key(key_id),
    )
