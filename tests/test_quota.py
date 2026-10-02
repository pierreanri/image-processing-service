import threading
import time
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import event, func, select, text, update
from sqlalchemy.exc import IntegrityError

from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.jobs import claim_next, run_job
from app.main import app
from app.models import Image, Job, User
from app.quota import UNLIMITED, main, parse_size
from tests.utils import make_image_bytes, register, share, stored_keys, user_id_of

DATA = make_image_bytes()  # every upload in this module is this PNG
SIZE = len(DATA)


@pytest.fixture(params=["local", "s3"])
def storage(request):
    """Every test in this module runs against both storage backends (S3 is moto's in-memory S3)."""
    return request.getfixturevalue(f"{request.param}_storage")


@pytest.fixture
def settings(client):
    """The settings the API and the worker use, which a test may change."""
    settings = get_settings().model_copy()
    app.dependency_overrides[get_settings] = lambda: settings
    return settings


def upload(client, headers, data=DATA):
    return client.post("/images", headers=headers, files={"file": ("a.png", data, "image/png")})


def uploaded(client, headers) -> dict:
    response = upload(client, headers)
    assert response.status_code == 201, response.text
    return response.json()


def transform(client, headers, image_id, prefer=None, transformations=None):
    return client.post(
        f"/images/{image_id}/transform",
        headers={**headers, **({"Prefer": prefer} if prefer else {})},
        json={"transformations": transformations or {"flip": True}},
    )


def set_own_limit(username: str, limit: int | None) -> None:
    with get_sessionmaker()() as db:
        db.execute(update(User).where(User.username == username).values(storage_quota_bytes=limit))
        db.commit()


def image_count() -> int:
    with get_sessionmaker()() as db:
        return db.scalar(select(func.count()).select_from(Image))


def recording_saves(storage) -> list[str]:
    """Records the keys of the files stored from now on."""
    saved = []
    real_save = storage.save

    def save(key, data, *, content_type):
        saved.append(key)
        real_save(key, data, content_type=content_type)

    storage.save = save
    return saved


def full_detail(used: int, limit: int, size: int) -> str:
    return (
        f"Storage quota exceeded: {used} of your {limit} bytes are used and this image takes "
        f"{size}; delete images to make room"
    )


# --- Uploads --------------------------------------------------------------------------------------


def test_uploads_are_refused_once_they_no_longer_fit(client, auth_headers, storage, settings):
    settings.storage_quota_bytes = 2 * SIZE
    uploaded(client, auth_headers)
    uploaded(client, auth_headers)  # exactly at the limit
    saved = recording_saves(storage)

    response = upload(client, auth_headers)

    assert response.status_code == 403
    assert response.json() == {"detail": full_detail(2 * SIZE, 2 * SIZE, SIZE)}
    assert "retry-after" not in response.headers
    assert saved == []  # refused before storing anything
    assert image_count() == 2
    assert len(stored_keys(storage)) == 2


def test_deleting_images_makes_room(client, auth_headers, settings):
    settings.storage_quota_bytes = SIZE
    image = uploaded(client, auth_headers)
    assert upload(client, auth_headers).status_code == 403

    assert client.delete(f"/images/{image['id']}", headers=auth_headers).status_code == 204

    assert upload(client, auth_headers).status_code == 201


def test_a_default_of_zero_means_no_limit(client, auth_headers, settings):
    settings.storage_quota_bytes = 0

    for _ in range(3):
        uploaded(client, auth_headers)

    storage = client.get("/me", headers=auth_headers).json()["storage"]
    assert storage == {"used_bytes": 3 * SIZE, "limit_bytes": None, "available_bytes": None}


def test_quotas_are_per_user(client, auth_headers, settings):
    settings.storage_quota_bytes = SIZE
    bob = register(client, "bob")
    uploaded(client, auth_headers)

    assert upload(client, bob).status_code == 201
    assert upload(client, auth_headers).status_code == 403


@pytest.mark.parametrize(
    ("own_limit", "uploads_allowed"),
    [(3 * SIZE, 3), (SIZE, 1), (0, 0), (UNLIMITED, 5), (None, 2)],
    ids=["larger", "smaller", "frozen", "unlimited", "default"],
)
def test_a_users_own_limit_replaces_the_default(
    client, auth_headers, settings, own_limit, uploads_allowed
):
    settings.storage_quota_bytes = 2 * SIZE
    set_own_limit("alice", own_limit)

    statuses = [upload(client, auth_headers).status_code for _ in range(5)]

    assert statuses == [201] * uploads_allowed + [403] * (5 - uploads_allowed)


def test_a_full_account_can_still_read_share_and_delete(client, auth_headers, settings):
    image = uploaded(client, auth_headers)
    set_own_limit("alice", 0)  # now over the limit

    assert client.get("/images", headers=auth_headers).json()["total"] == 1
    assert client.get(f"/images/{image['id']}", headers=auth_headers).status_code == 200
    assert client.get(image["url"], headers=auth_headers).status_code == 200
    converted = client.get(image["url"], params={"format": "webp"}, headers=auth_headers)
    assert converted.status_code == 200
    link = share(client, auth_headers, image["id"])
    assert client.get(link["url"]).status_code == 200
    assert client.delete(f"/images/{image['id']}", headers=auth_headers).status_code == 204


# --- Transformations ------------------------------------------------------------------------------


@pytest.mark.parametrize("prefer", [None, "respond-async"])
def test_a_full_account_is_refused_before_rendering_or_rate_limiting(
    client, auth_headers, storage, settings, rate_limiter, prefer
):
    settings.storage_quota_bytes = SIZE
    image = uploaded(client, auth_headers)
    storage.read = lambda key: pytest.fail("nothing should be rendered")

    response = transform(client, auth_headers, image["id"], prefer=prefer)

    assert response.status_code == 403
    assert response.json() == {
        "detail": f"Storage quota exceeded: you are using {SIZE} of your {SIZE} bytes; "
        "delete images to make room"
    }
    assert rate_limiter.hits == []
    with get_sessionmaker()() as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_a_result_that_does_not_fit_is_refused_and_counts(
    client, auth_headers, storage, settings, rate_limiter
):
    image = uploaded(client, auth_headers)
    settings.storage_quota_bytes = SIZE + 10  # room, but not for a whole image
    saved = recording_saves(storage)

    response = transform(client, auth_headers, image["id"], transformations={"rotate": 90})

    assert response.status_code == 403
    assert response.json()["detail"].startswith(
        f"Storage quota exceeded: {SIZE} of your {SIZE + 10} bytes are used and this image takes"
    )
    assert rate_limiter.hits == [user_id_of(auth_headers)]  # like a transformation that fails
    assert saved == []
    assert image_count() == 1


def test_a_result_that_fits_is_stored(client, auth_headers, settings):
    image = uploaded(client, auth_headers)
    settings.storage_quota_bytes = 3 * SIZE

    response = transform(client, auth_headers, image["id"])

    assert response.status_code == 201
    used = client.get("/me", headers=auth_headers).json()["storage"]["used_bytes"]
    assert used == SIZE + response.json()["size_bytes"]


# --- Background jobs ------------------------------------------------------------------------------


def queued_job(client, headers) -> dict:
    image = uploaded(client, headers)
    response = transform(client, headers, image["id"], prefer="respond-async")
    assert response.status_code == 202, response.text
    return response.json()


def job_error(client, headers, job) -> dict:
    polled = client.get(job["url"], headers=headers).json()
    assert polled["status"] == "failed", polled
    return polled["error"]


def test_a_job_fails_if_the_account_filled_up_before_it_ran(
    client, auth_headers, storage, settings, worker
):
    job = queued_job(client, auth_headers)
    set_own_limit("alice", SIZE)  # full now
    worker._settings = settings
    storage.read = lambda key: pytest.fail("nothing should be rendered")

    worker.run_once()

    assert job_error(client, auth_headers, job) == {
        "status_code": 403,
        "detail": f"Storage quota exceeded: you are using {SIZE} of your {SIZE} bytes; "
        "delete images to make room",
    }


def test_a_job_whose_result_does_not_fit_fails_without_storing_it(
    client, auth_headers, storage, settings, worker
):
    job = queued_job(client, auth_headers)
    set_own_limit("alice", SIZE + 10)
    worker._settings = settings
    saved = recording_saves(storage)

    worker.run_once()

    error = job_error(client, auth_headers, job)
    assert error["status_code"] == 403
    assert "this image takes" in error["detail"]
    assert saved == []


def test_a_job_whose_result_no_longer_fits_when_it_completes_fails(
    client, auth_headers, storage, settings, worker
):
    job = queued_job(client, auth_headers)
    worker._settings = settings
    real_save = storage.save

    def save_then_fill_the_account(key, data, *, content_type):
        real_save(key, data, content_type=content_type)
        set_own_limit("alice", SIZE)  # e.g. another upload took the rest meanwhile

    storage.save = save_then_fill_the_account

    worker.run_once()

    assert job_error(client, auth_headers, job)["status_code"] == 403
    assert len(stored_keys(storage)) == 1  # the result's file was discarded
    assert image_count() == 1


def test_a_job_that_fits_succeeds(client, auth_headers, settings, worker):
    job = queued_job(client, auth_headers)
    settings.storage_quota_bytes = 3 * SIZE
    worker._settings = settings

    worker.run_once()

    assert client.get(job["url"], headers=auth_headers).json()["status"] == "succeeded"


def wait_for_a_lock_wait(timeout: float = 5) -> bool:
    """Until some session of the test database waits for a row lock."""
    engine = get_sessionmaker().kw["bind"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with engine.connect() as probe:
            waiting = probe.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
        if waiting:
            return True
        time.sleep(0.01)
    return False


def test_a_job_that_waited_on_the_quota_lock_past_its_lease_cannot_finish(
    client, auth_headers, storage, settings
):
    """The lease is checked against the time of the statement, not of the transaction's start,
    which may be long before if the transaction waited for the owner's row."""
    job = queued_job(client, auth_headers)
    sessions = get_sessionmaker()
    with sessions() as db:
        claim = claim_next(db, 300)
    holder = sessions()
    holder.execute(select(User.id).where(User.username == "alice").with_for_update(key_share=True))
    runner = threading.Thread(target=run_job, args=(sessions, storage, settings, claim))
    try:
        runner.start()
        assert wait_for_a_lock_wait()  # the completing transaction has started, and waits
        with sessions() as db:
            # The lease runs out while it waits.
            db.execute(
                update(Job)
                .where(Job.id == uuid.UUID(job["id"]))
                .values(available_at=func.clock_timestamp())
            )
            db.commit()
        time.sleep(0.05)
    finally:
        holder.rollback()
        holder.close()
        runner.join(10)

    with sessions() as db:
        assert db.get(Job, uuid.UUID(job["id"])).status == "running"
    assert len(stored_keys(storage)) == 1  # its result was discarded
    assert image_count() == 1


# --- Concurrent additions -------------------------------------------------------------------------


def test_concurrent_uploads_cannot_overfill_an_account(client, auth_headers, storage, settings):
    """Each upload fits alone, not both: the second one's decisive check waits for the first
    to commit, then sees its image."""
    settings.storage_quota_bytes = 2 * SIZE - 1
    engine = get_sessionmaker().kw["bind"]
    state = {"paused": False}
    second = {}

    def upload_second():
        second["response"] = upload(client, auth_headers)

    other = threading.Thread(target=upload_second)

    def after_statement(conn, cursor, statement, parameters, context, executemany):
        if "FOR NO KEY UPDATE" in statement:
            conn.info["quota_locked"] = True
        elif "sum(images.size_bytes)" in statement and conn.info.pop("quota_locked", False):
            if state["paused"]:
                return
            # The first upload has checked its quota under the lock and not inserted yet: start
            # the second one, and let the first go on once the second waits for the lock.
            state["paused"] = True
            other.start()
            assert wait_for_a_lock_wait()

    event.listen(engine, "after_cursor_execute", after_statement)
    try:
        first = upload(client, auth_headers)
    finally:
        event.remove(engine, "after_cursor_execute", after_statement)
        if other.ident:
            other.join(10)

    assert state["paused"]
    statuses = sorted([first.status_code, second["response"].status_code])
    assert statuses == [201, 403]
    assert image_count() == 1
    assert len(stored_keys(storage)) == 1  # the refused upload's file was discarded


# --- GET /me --------------------------------------------------------------------------------------


def test_me_shows_the_account_and_its_storage(client, auth_headers, settings):
    settings.storage_quota_bytes = 10 * SIZE
    uploaded(client, auth_headers)

    response = client.get("/me", headers=auth_headers)

    assert response.status_code == 200
    me = response.json()
    assert me["id"] == str(user_id_of(auth_headers))
    assert me["username"] == "alice"
    assert me["created_at"]
    assert me["storage"] == {
        "used_bytes": SIZE,
        "limit_bytes": 10 * SIZE,
        "available_bytes": 9 * SIZE,
    }


def test_me_shows_no_room_left_when_over_the_limit(client, auth_headers):
    uploaded(client, auth_headers)
    uploaded(client, auth_headers)
    set_own_limit("alice", SIZE)

    storage = client.get("/me", headers=auth_headers).json()["storage"]

    assert storage == {"used_bytes": 2 * SIZE, "limit_bytes": SIZE, "available_bytes": 0}


def test_me_needs_a_token(client):
    response = client.get("/me")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_openapi_documents_quotas():
    paths = app.openapi()["paths"]

    assert "get" in paths["/me"]
    assert "403" in paths["/images"]["post"]["responses"]
    assert "403" in paths["/images/{image_id}/transform"]["post"]["responses"]


# --- Settings and schema --------------------------------------------------------------------------


def test_the_default_quota_is_one_gibibyte_and_never_negative():
    secret = "test-secret-that-is-at-least-32-characters-long"

    assert Settings(_env_file=None, jwt_secret=secret).storage_quota_bytes == 1024**3
    with pytest.raises(ValidationError):
        Settings(_env_file=None, jwt_secret=secret, storage_quota_bytes=-1)


def test_own_limits_below_unlimited_are_rejected_by_the_database(client, auth_headers):
    with pytest.raises(IntegrityError):
        set_own_limit("alice", -2)


# --- The operator command -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", 0),
        ("1048576", 1048576),
        ("500MB", 500_000_000),
        ("5gb", 5_000_000_000),
        ("5 GB", 5_000_000_000),
        ("2GiB", 2 * 1024**3),
        ("1.5KB", 1500),
        ("1.5KiB", 1536),
        ("10B", 10),
        ("1TiB", 1024**4),
        ("unlimited", UNLIMITED),
        ("UNLIMITED", UNLIMITED),
        ("0GB", 0),
    ],
)
def test_sizes_are_parsed(value, expected):
    assert parse_size(value) == expected


@pytest.mark.parametrize(
    "value",
    ["", "-5GB", "1e9", "5G", "5 gigabytes", "0.5B", "0.0001KB", "1.5", "GB", "9999999TB", "nan"],
)
def test_bad_sizes_are_rejected(value):
    with pytest.raises(Exception, match="size such as|less than one byte|too large|whole number"):
        parse_size(value)


def test_set_unset_and_show(client, auth_headers, settings, capsys):
    uploaded(client, auth_headers)
    alice = user_id_of(auth_headers)
    default = get_settings().storage_quota_bytes

    assert main(["set", "alice", "5GB"]) == 0
    out = capsys.readouterr().out
    assert out == (
        f"alice ({alice}): using {SIZE} bytes; limit 5000000000 bytes (4.7 GiB, own limit); "
        f"{5_000_000_000 - SIZE} bytes left\n"
    )
    assert upload(client, auth_headers).status_code == 201

    assert main(["set", "alice", "unlimited"]) == 0
    assert capsys.readouterr().out.endswith(f"using {2 * SIZE} bytes; no limit (own limit)\n")

    assert main(["set", "alice", "100"]) == 0
    assert capsys.readouterr().out.endswith(
        f"limit 100 bytes (own limit); over the limit by {2 * SIZE - 100} bytes: can't add images\n"
    )
    assert upload(client, auth_headers).status_code == 403

    assert main(["unset", "alice"]) == 0
    assert capsys.readouterr().out.endswith(
        f"limit {default} bytes (1.0 GiB, the default); {default - 2 * SIZE} bytes left\n"
    )
    assert upload(client, auth_headers).status_code == 201


def test_show_lists_every_user_by_usage(client, auth_headers, capsys):
    bob = register(client, "bob")
    register(client, "carol")
    uploaded(client, bob)
    uploaded(client, bob)
    uploaded(client, auth_headers)

    assert main(["show"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert [line.split(" ")[0] for line in lines] == ["bob", "alice", "carol"]
    assert f"using {2 * SIZE} bytes" in lines[0]
    assert "using 0 bytes" in lines[2]


def test_unknown_users_are_an_error(client, capsys):
    for command in (["show", "nobody"], ["set", "nobody", "1GB"], ["unset", "nobody"]):
        assert main(command) == 1
        assert capsys.readouterr().err == "No user named 'nobody'\n"


def test_usernames_starting_with_a_dash_need_a_double_dash(client, capsys):
    register(client, "-dash")

    assert main(["set", "--", "-dash", "1MB"]) == 0
    assert "limit 1000000 bytes (976.6 KiB, own limit)" in capsys.readouterr().out


def test_bad_command_lines_exit_2(client, capsys):
    for command in ([], ["set", "alice"], ["set", "alice", "lots"], ["frobnicate"]):
        with pytest.raises(SystemExit) as raised:
            main(command)
        assert raised.value.code == 2


def test_sizes_are_exact_decimals():
    # Through a float, 0.3 TB would be 299999999999 or 300000000000.00006 bytes.
    assert parse_size("0.3TB") == 300_000_000_000
    assert parse_size("1.1GB") == 1_100_000_000
