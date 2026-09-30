"""Finds, and with --delete removes, stored files that no image refers to:
`python -m app.sweep_orphans [--grace-hours 24] [--delete]`.

Files are saved before their image row is committed, so a crash or a failed commit in between
leaves an orphan (as does a failed delete after an image is deleted, or a worker that lost its
job). Only files older than the grace period are orphans: younger ones may belong to an upload
or a job that is about to commit. Files whose names the service would never have made are
ignored, so a bucket or directory shared with other data is safe. Without --delete, nothing is
deleted.
"""

import argparse
import logging
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import islice

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db import get_sessionmaker
from app.models import Image
from app.storage import (
    SERVICE_KEY,
    SERVICE_TEMP_KEY,
    Storage,
    StorageUnavailableError,
    StoredFile,
    get_storage,
)

logger = logging.getLogger(__name__)

DEFAULT_GRACE = timedelta(hours=24)
# Far longer than an upload or a job takes between saving its file and committing its image.
MIN_GRACE = timedelta(hours=1)
# Longer would be pointless (and could overflow datetime arithmetic).
MAX_GRACE = timedelta(days=100 * 365)
# Keys looked up in the database at a time.
BATCH_SIZE = 1000


@dataclass
class SweepReport:
    orphans: list[StoredFile] = field(default_factory=list)
    # Files the service didn't name (see the module docstring); never deleted.
    ignored: int = 0
    scanned: int = 0

    @property
    def orphaned_bytes(self) -> int:
        return sum(file.size for file in self.orphans)


def find_orphans(
    sessions: sessionmaker[Session],
    storage: Storage,
    grace: timedelta,
    now: datetime,
    report: SweepReport,
) -> Iterator[StoredFile]:
    """Yield the stored files older than `grace` that no image refers to, as they are found,
    counting everything seen in `report`."""
    if not MIN_GRACE <= grace <= MAX_GRACE:
        raise ValueError(f"The grace period must be between {MIN_GRACE} and {MAX_GRACE}")
    cutoff = now - grace
    files = storage.list_files()
    while batch := list(islice(files, BATCH_SIZE)):
        report.scanned += len(batch)
        candidates = []
        for file in batch:
            if SERVICE_TEMP_KEY.fullmatch(file.key):
                # Temporary files are never referenced; only a crash mid-save leaves one.
                if file.last_modified < cutoff:
                    yield file
            elif not SERVICE_KEY.fullmatch(file.key):
                report.ignored += 1
            elif file.last_modified < cutoff:
                candidates.append(file)
        if not candidates:
            continue
        # A short session per batch: listing a large bucket takes a while.
        with sessions() as db:
            referenced = set(
                db.scalars(
                    select(Image.storage_key).where(
                        Image.storage_key.in_([file.key for file in candidates])
                    )
                )
            )
        yield from (file for file in candidates if file.key not in referenced)


def sweep(
    sessions: sessionmaker[Session],
    storage: Storage,
    grace: timedelta,
    *,
    delete: bool,
    now: datetime | None = None,
) -> tuple[SweepReport, int]:
    """Report (and with `delete`, delete) orphaned files; returns the report and how many
    deletions failed."""
    report = SweepReport()
    failures = 0
    for file in find_orphans(sessions, storage, grace, now or datetime.now(UTC), report):
        report.orphans.append(file)
        modified = file.last_modified.isoformat(timespec="seconds")
        if not delete:
            print(f"would delete {file.key} ({file.size} bytes, modified {modified})")
            continue
        try:
            storage.delete(file.key)
        except Exception as exc:
            failures += 1
            logger.error("Could not delete %s: %s", file.key, exc)
            continue
        print(f"deleted {file.key} ({file.size} bytes, modified {modified})")
    return report, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.sweep_orphans",
        description="Find stored files that no image refers to; delete them with --delete.",
    )
    parser.add_argument(
        "--grace-hours",
        type=_grace_hours,
        default=DEFAULT_GRACE,
        metavar="HOURS",
        help="only files older than this many hours are orphans (default: 24, minimum: 1)",
    )
    parser.add_argument(
        "--delete", action="store_true", help="delete the orphans (default: only list them)"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    sessions, storage = get_sessionmaker(), get_storage()
    try:
        report, failures = sweep(sessions, storage, args.grace_hours, delete=args.delete)
    except StorageUnavailableError as exc:
        print(f"Storage is unavailable, stopping: {exc}", file=sys.stderr)
        return 1

    found = _files(len(report.orphans), "orphaned file")
    summary = f"Found {found} ({report.orphaned_bytes} bytes) among {_files(report.scanned)}"
    if report.ignored:
        summary += f"; ignored {_files(report.ignored)} the service did not create"
    print(f"{summary}.")
    if not args.delete:
        if report.orphans:
            print("Dry run: nothing was deleted. Run again with --delete to delete them.")
        return 0
    if report.orphans:
        print(f"Deleted {len(report.orphans) - failures} of them.")
    if failures:
        print(f"Could not delete {failures} of them; see the errors above.", file=sys.stderr)
        return 1
    return 0


def _files(count: int, noun: str = "file") -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _grace_hours(value: str) -> timedelta:
    try:
        hours = float(value)
        grace = timedelta(hours=hours)
    except (ValueError, OverflowError):  # Not a number, NaN or huge.
        grace = None
    if grace is None or not MIN_GRACE <= grace <= MAX_GRACE:
        limit = MAX_GRACE // timedelta(hours=1)
        raise argparse.ArgumentTypeError(f"expected a number of hours from 1 to {limit}")
    return grace


if __name__ == "__main__":
    sys.exit(main())
