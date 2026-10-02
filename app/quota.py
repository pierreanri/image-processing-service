"""Per-user storage quotas, and the operator command that sets them:
`python -m app.quota show [USERNAME] | set USERNAME SIZE | unset USERNAME`.

A user's usage is the total size of their images (uploads and transformation results), summed from
the images table whenever it's needed, so it is always exact. Their limit is their own (set with
this command) or STORAGE_QUOTA_BYTES.

Adding an image is checked twice. An early check, without locks, refuses what certainly doesn't fit
before anything is stored. The check that decides comes first in the transaction that inserts the
image: it locks the owner's users row (FOR NO KEY UPDATE, which doesn't block the foreign-key checks
of inserts), then sums their images in a statement of its own, which under READ COMMITTED sees the
images of every addition that held the lock before. One user's additions therefore take turns.
Deleting needs no lock: a check that still counts an image being deleted only errs on the safe side.
"""

import argparse
import re
import sys
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.imaging import ImageProcessingError
from app.models import Image, User

# A user's own limit meaning "no limit" (NULL means STORAGE_QUOTA_BYTES applies).
UNLIMITED = -1
# The largest limit the users.storage_quota_bytes column (a BIGINT) can hold.
MAX_LIMIT = 2**63 - 1


class QuotaExceededError(ImageProcessingError):
    """Storing an image would take its owner over their storage quota. Answered with 403: only
    deleting images (or a larger quota) helps, so repeating the request is pointless."""

    status_code = 403


@dataclass(frozen=True)
class Usage:
    used_bytes: int
    # None: no limit.
    limit_bytes: int | None

    @property
    def available_bytes(self) -> int | None:
        if self.limit_bytes is None:
            return None
        return max(self.limit_bytes - self.used_bytes, 0)


def quota_limit(own_limit: int | None, settings: Settings) -> int | None:
    """A user's limit in bytes, from their own limit (None: STORAGE_QUOTA_BYTES applies, where 0
    means no limit); None means no limit."""
    if own_limit is None:
        return settings.storage_quota_bytes or None
    return None if own_limit == UNLIMITED else own_limit


def storage_used(db: Session, owner_id: uuid.UUID) -> int:
    """The total size of a user's images."""
    return db.scalar(
        select(func.coalesce(func.sum(Image.size_bytes), 0)).where(Image.owner_id == owner_id)
    )


def check_quota(
    db: Session,
    settings: Settings,
    owner_id: uuid.UUID,
    size: int | None,
    *,
    lock: bool = False,
) -> None:
    """Raise QuotaExceededError unless `size` more bytes (None: not known yet, but every image
    takes at least one) fit in the owner's quota.

    Without `lock`, this only saves work: another request may fill the quota right after. With
    `lock` it decides, and must be the first statement of the transaction that inserts the image,
    before the image is added to the session; that transaction then holds the owner's row until
    it ends, so it must not wait on storage.
    """
    # The column, not the User: the session may hold one loaded before the limit last changed.
    query = select(User.storage_quota_bytes).where(User.id == owner_id)
    if lock:
        query = query.with_for_update(key_share=True)
    row = db.execute(query).one_or_none()
    if row is None:
        return  # The user is gone, so inserting their image fails on its foreign key.
    limit = quota_limit(row.storage_quota_bytes, settings)
    if limit is None:
        return
    # A statement of its own: with `lock`, its snapshot is taken once the lock is granted.
    used = storage_used(db, owner_id)
    if used + (1 if size is None else size) <= limit:
        return
    # Only advise deleting images when that can make room.
    if limit == 0:
        raise QuotaExceededError("Storage quota exceeded: this account may not store any images")
    if size is not None and size > limit:
        raise QuotaExceededError(
            f"Storage quota exceeded: this image takes {size} bytes, more than your whole quota "
            f"of {limit}"
        )
    if size is None:
        raise QuotaExceededError(
            f"Storage quota exceeded: you are using {used} of your {limit} bytes; delete images "
            "to make room"
        )
    raise QuotaExceededError(
        f"Storage quota exceeded: {used} of your {limit} bytes are used and this image takes "
        f"{size}; delete images to make room"
    )


# --- The operator command ---------------------------------------------------------------------

_SIZE = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([a-z]*)\s*", re.IGNORECASE)
_UNITS = {
    "": 1,
    "b": 1,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "tb": 1000**4,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
}


def parse_size(value: str) -> int:
    """A limit given on the command line, in bytes: a number with an optional unit (KB, MB, GB
    and TB are powers of 1000; KiB, MiB, GiB and TiB powers of 1024), or `unlimited`."""
    if value.strip().lower() == "unlimited":
        return UNLIMITED
    match = _SIZE.fullmatch(value)
    unit = _UNITS.get(match[2].lower()) if match else None
    if unit is None:
        raise argparse.ArgumentTypeError(
            "expected a size such as 500MB, 5GB, 2GiB or 1048576 (bytes), or 'unlimited'"
        )
    number = Decimal(match[1])
    if unit == 1 and number != int(number):
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number of bytes")
    size = int(number * unit)
    if size == 0 and number != 0:
        raise argparse.ArgumentTypeError(f"{value!r} is less than one byte")
    if size > MAX_LIMIT:
        raise argparse.ArgumentTypeError(f"{value!r} is too large (at most {MAX_LIMIT} bytes)")
    return size


def _amount(size: int, note: str | None = None) -> str:
    """A number of bytes, with a binary-unit figure once it is large, and a note."""
    notes = [] if note is None else [note]
    if size >= 1024:
        figure = size / 1024
        for unit in ("KiB", "MiB", "GiB", "TiB", "PiB", "EiB"):
            if figure < 1024 or unit == "EiB":
                break
            figure /= 1024
        notes.insert(0, f"{figure:.1f} {unit}")
    return f"{size} bytes" + (f" ({', '.join(notes)})" if notes else "")


def _describe(
    username: str, user_id: uuid.UUID, own_limit: int | None, used: int, settings: Settings
) -> str:
    limit = quota_limit(own_limit, settings)
    source = "the default" if own_limit is None else "own limit"
    line = f"{username} ({user_id}): using {_amount(used)}; "
    if limit is None:
        return line + f"no limit ({source})"
    line += f"limit {_amount(limit, source)}; "
    if used > limit:
        return line + f"over the limit by {used - limit} bytes: can't add images"
    return line + f"{limit - used} bytes left"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.quota",
        description="Show and set users' storage quotas (STORAGE_QUOTA_BYTES applies to users "
        "without a limit of their own). Put -- before a username that starts with -.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("show", help="show a user's usage and limit, or every user's")
    show.add_argument("username", nargs="?")
    set_ = commands.add_parser("set", help="give a user a limit of their own")
    set_.add_argument("username")
    set_.add_argument(
        "size",
        type=parse_size,
        help="e.g. 500MB, 5GB, 2GiB, 1048576 (bytes), 0 (nothing more may be added) or unlimited",
    )
    unset = commands.add_parser("unset", help="make the default limit apply to a user again")
    unset.add_argument("username")
    args = parser.parse_args(argv)

    settings = get_settings()
    with get_sessionmaker()() as db:
        if args.command in ("set", "unset"):
            own_limit = args.size if args.command == "set" else None
            updated = db.execute(
                update(User)
                .where(User.username == args.username)
                .values(storage_quota_bytes=own_limit)
                .returning(User.id)
            ).one_or_none()
            db.commit()
            if updated is None:
                print(f"No user named {args.username!r}", file=sys.stderr)
                return 1
        used = func.coalesce(func.sum(Image.size_bytes), 0)
        query = (
            select(User.username, User.id, User.storage_quota_bytes, used)
            .outerjoin(Image, Image.owner_id == User.id)
            .group_by(User.id)
            .order_by(used.desc(), User.username)
        )
        if args.username is not None:
            query = query.where(User.username == args.username)
        rows = db.execute(query).all()
    if args.username is not None and not rows:
        print(f"No user named {args.username!r}", file=sys.stderr)
        return 1
    for username, user_id, own_limit, used_bytes in rows:
        print(_describe(username, user_id, own_limit, used_bytes, settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
