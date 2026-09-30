import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

import app.sweep_orphans
from app.config import get_settings
from app.db import get_sessionmaker
from app.models import Image
from app.storage import StorageUnavailableError, build_key
from app.sweep_orphans import SweepReport, find_orphans, main, sweep
from tests.utils import make_image_bytes, stored_keys, user_id_of

# Seen from here, every file saved during a test is two days old.
LATER = datetime.now(UTC) + timedelta(days=2)
GRACE = timedelta(hours=24)


@pytest.fixture(params=["local", "s3"])
def storage(request):
    """Every test in this module runs against both storage backends (S3 is moto's in-memory S3)."""
    return request.getfixturevalue(f"{request.param}_storage")


def upload(client, headers) -> dict:
    files = {"file": ("a.png", make_image_bytes(), "image/png")}
    response = client.post("/images", headers=headers, files=files)
    assert response.status_code == 201, response.text
    return response.json()


def stored_key(image_id) -> str:
    with get_sessionmaker()() as db:
        return db.get(Image, uuid.UUID(image_id)).storage_key


def orphan(storage, owner_id, data=b"orphan") -> str:
    key = build_key(owner_id, "png")
    storage.save(key, data, content_type="image/png")
    return key


def temp_file(storage, owner_id) -> str:
    key = f"{owner_id}/.{uuid.uuid4().hex}.png.{uuid.uuid4().hex}.tmp"
    storage.save(key, b"partial", content_type="application/octet-stream")
    return key


def run(storage, *, delete, now=LATER, grace=GRACE):
    return sweep(get_sessionmaker(), storage, grace, delete=delete, now=now)


@pytest.fixture
def owner_id(client, auth_headers):
    return user_id_of(auth_headers)


def test_a_dry_run_lists_old_orphans_and_deletes_nothing(
    client, auth_headers, storage, owner_id, capsys
):
    image = upload(client, auth_headers)
    kept = stored_key(image["id"])
    orphaned = orphan(storage, owner_id)

    report, failures = run(storage, delete=False)

    assert [file.key for file in report.orphans] == [orphaned]
    assert report.orphaned_bytes == len(b"orphan")
    assert (report.scanned, report.ignored, failures) == (2, 0, 0)
    assert stored_keys(storage) == sorted([kept, orphaned])
    assert f"would delete {orphaned} (6 bytes" in capsys.readouterr().out


def test_delete_removes_orphans_and_keeps_referenced_files(client, auth_headers, storage, owner_id):
    image = upload(client, auth_headers)
    kept = stored_key(image["id"])
    orphans = {
        orphan(storage, owner_id),
        orphan(storage, uuid.uuid4()),
        temp_file(storage, owner_id),
    }

    report, failures = run(storage, delete=True)

    assert {file.key for file in report.orphans} == orphans
    assert failures == 0
    assert stored_keys(storage) == [kept]
    assert client.get(image["url"], headers=auth_headers).status_code == 200


def test_files_younger_than_the_grace_period_are_kept(client, auth_headers, storage, owner_id):
    key = orphan(storage, owner_id)
    temp_key = temp_file(storage, owner_id)

    report, _ = run(storage, delete=True, now=datetime.now(UTC), grace=timedelta(hours=1))

    assert report.orphans == []
    assert report.scanned == 2
    assert stored_keys(storage) == sorted([key, temp_key])


def test_files_the_service_did_not_name_are_never_touched(client, storage, owner_id):
    foreign = [
        "notes.txt",
        "backups/2026/images.tar",
        f"{owner_id}/avatar.png",
        f"{owner_id}/{uuid.uuid4().hex}.png.bak",
        f"{owner_id}/.{uuid.uuid4().hex}.png.tmp",
        f"{str(owner_id).upper()}/{uuid.uuid4().hex}.png",
        f"prefix/{owner_id}/{uuid.uuid4().hex}.png",
    ]
    for key in foreign:
        storage.save(key, b"not ours", content_type="application/octet-stream")

    report, _ = run(storage, delete=True)

    assert report.orphans == []
    assert report.ignored == len(foreign)
    assert stored_keys(storage) == sorted(foreign)


def test_every_batch_is_checked_against_the_database(
    client, auth_headers, storage, owner_id, monkeypatch
):
    monkeypatch.setattr(app.sweep_orphans, "BATCH_SIZE", 2)
    images = [upload(client, auth_headers) for _ in range(3)]
    kept = [stored_key(image["id"]) for image in images]
    orphans = [orphan(storage, owner_id) for _ in range(3)]

    report, _ = run(storage, delete=True)

    assert sorted(file.key for file in report.orphans) == sorted(orphans)
    assert report.scanned == 6
    assert stored_keys(storage) == sorted(kept)


def test_derived_images_and_their_sources_are_both_referenced(client, auth_headers, storage):
    image = upload(client, auth_headers)
    response = client.post(
        f"/images/{image['id']}/transform",
        headers=auth_headers,
        json={"transformations": {"rotate": 90}},
    )
    assert response.status_code == 201

    report, _ = run(storage, delete=True)

    assert report.orphans == []
    assert len(stored_keys(storage)) == 2


def test_deleting_an_image_whose_file_was_left_behind_frees_it(
    client, auth_headers, storage, owner_id, monkeypatch
):
    image = upload(client, auth_headers)
    key = stored_key(image["id"])
    # The API deletes the row, then fails to delete the file.
    monkeypatch.setattr(type(storage), "delete", lambda self, key: None)
    assert client.delete(f"/images/{image['id']}", headers=auth_headers).status_code == 204
    monkeypatch.undo()

    report, _ = run(storage, delete=True)

    assert [file.key for file in report.orphans] == [key]
    assert stored_keys(storage) == []


def test_a_failed_deletion_is_counted_and_the_sweep_goes_on(
    client, storage, owner_id, monkeypatch, caplog
):
    first = orphan(storage, owner_id)
    orphan(storage, owner_id)
    real_delete = type(storage).delete

    def flaky_delete(self, key):
        if key == first:
            raise StorageUnavailableError("storage is down")
        real_delete(self, key)

    monkeypatch.setattr(type(storage), "delete", flaky_delete)

    report, failures = run(storage, delete=True)

    assert len(report.orphans) == 2
    assert failures == 1
    assert stored_keys(storage) == [first]
    assert f"Could not delete {first}: storage is down" in caplog.text


@pytest.mark.parametrize("grace", [timedelta(minutes=59), timedelta(days=100 * 365 + 1)])
def test_grace_period_is_bounded(storage, grace):
    with pytest.raises(ValueError, match="grace period"):
        next(find_orphans(get_sessionmaker(), storage, grace, LATER, SweepReport()))


# --- The command ----------------------------------------------------------------------------------


# The command's tests backdate files, which only local storage allows.
local_only = pytest.mark.parametrize("storage", ["local"], indirect=True)


@pytest.fixture
def cli(client, storage, monkeypatch):
    """Runs `python -m app.sweep_orphans` on the test database and the test's storage."""
    monkeypatch.setattr(app.sweep_orphans, "get_storage", lambda: storage)

    def run_cli(*args: str) -> int:
        return main(list(args))

    return run_cli


def backdate(storage, key, hours):
    moment = (datetime.now(UTC) - timedelta(hours=hours)).timestamp()
    os.utime(storage.path(key), (moment, moment))


@local_only
def test_command_dry_run_then_delete(cli, storage, auth_headers, client, capsys):
    image = upload(client, auth_headers)
    kept = stored_key(image["id"])
    owner = user_id_of(auth_headers)
    old, young = orphan(storage, owner), orphan(storage, owner)
    backdate(storage, old, hours=25)
    backdate(storage, kept, hours=25)
    backdate(storage, young, hours=23)

    assert cli() == 0
    out = capsys.readouterr().out
    assert f"would delete {old}" in out
    assert young not in out
    assert "Found 1 orphaned file (6 bytes) among 3 files.\n" in out
    assert "Dry run: nothing was deleted" in out
    assert stored_keys(storage) == sorted([kept, old, young])

    assert cli("--grace-hours", "22", "--delete") == 0
    out = capsys.readouterr().out
    assert f"deleted {old}" in out
    assert f"deleted {young}" in out
    assert "Found 2 orphaned files (12 bytes) among 3 files.\nDeleted 2 of them.\n" in out
    assert "Dry run" not in out
    assert stored_keys(storage) == [kept]


@local_only
def test_command_reports_ignored_files(cli, storage, capsys):
    storage.save("notes.txt", b"hello", content_type="text/plain")

    assert cli() == 0

    out = capsys.readouterr().out
    assert "Found 0 orphaned files (0 bytes) among 1 file; ignored 1 file the service" in out
    assert "Dry run" not in out


@local_only
def test_command_needs_a_grace_period_longer_than_the_job_lease(cli, monkeypatch, capsys):
    settings = get_settings().model_copy(update={"job_lease_seconds": 2 * 3600})
    monkeypatch.setattr(app.sweep_orphans, "get_settings", lambda: settings)

    with pytest.raises(SystemExit) as raised:
        cli("--grace-hours", "2")

    assert raised.value.code == 2
    assert "longer than JOB_LEASE_SECONDS" in capsys.readouterr().err
    assert cli("--grace-hours", "2.5") == 0


@local_only
@pytest.mark.parametrize("value", ["0.5", "0", "-3", "nan", "inf", "1e30", "soon"])
def test_command_rejects_bad_grace_periods(cli, capsys, value):
    with pytest.raises(SystemExit) as raised:
        cli("--grace-hours", value)

    assert raised.value.code == 2
    assert "--grace-hours: expected a number of hours from 1 to" in capsys.readouterr().err


@local_only
def test_command_fails_when_deletions_fail(cli, storage, client, auth_headers, monkeypatch, capsys):
    key = orphan(storage, user_id_of(auth_headers))
    backdate(storage, key, hours=48)

    def failing_delete(self, key):
        raise PermissionError("read-only")

    monkeypatch.setattr(type(storage), "delete", failing_delete)

    assert cli("--delete") == 1
    assert stored_keys(storage) == [key]
    out, err = capsys.readouterr()
    assert "Deleted 0 of them." in out
    assert "Could not delete 1 of them" in err


@local_only
def test_command_stops_when_storage_is_unavailable(cli, storage, monkeypatch, capsys):
    def unavailable(self):
        raise StorageUnavailableError("no route to host")
        yield

    monkeypatch.setattr(type(storage), "list_files", unavailable)

    assert cli() == 1
    assert "Storage is unavailable, stopping: no route to host" in capsys.readouterr().err
