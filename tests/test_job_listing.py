import base64
import uuid

import pytest
from sqlalchemy import event, text, update

from app.db import get_sessionmaker
from app.models import Job
from tests.utils import make_image_bytes, register


def upload(client, headers) -> dict:
    files = {"file": ("a.png", make_image_bytes(), "image/png")}
    response = client.post("/images", headers=headers, files=files)
    assert response.status_code == 201, response.text
    return response.json()


def submit(client, headers, image_id, transformations=None) -> dict:
    response = client.post(
        f"/images/{image_id}/transform",
        headers={**headers, "Prefer": "respond-async"},
        json={"transformations": transformations or {"flip": True}},
    )
    assert response.status_code == 202, response.text
    return response.json()


def submit_jobs(client, headers, count) -> list[str]:
    """Queue `count` jobs on one image; returns their ids, oldest first."""
    image = upload(client, headers)
    return [submit(client, headers, image["id"])["id"] for _ in range(count)]


def list_jobs(client, headers, **params) -> dict:
    response = client.get("/jobs", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def all_pages(client, headers, **params) -> list[list[str]]:
    """The ids on every page, following next_cursor to the end."""
    pages = []
    cursor = None
    while True:
        body = list_jobs(client, headers, **params, **({"cursor": cursor} if cursor else {}))
        pages.append([job["id"] for job in body["items"]])
        cursor = body["next_cursor"]
        if cursor is None:
            return pages


# Any valid id, the same in every run (so are the test ids that contain it).
SOME_ID = "3f2b8c1e-5d4a-4b6f-9c0d-1e2f3a4b5c6d"


def make_cursor(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def test_jobs_are_listed_newest_first_across_pages(client, auth_headers):
    ids = submit_jobs(client, auth_headers, 5)

    assert all_pages(client, auth_headers, limit=2) == [ids[4:2:-1], ids[2:0:-1], ids[:1]]
    assert all_pages(client, auth_headers) == [ids[::-1]]


def test_a_full_last_page_has_no_cursor(client, auth_headers):
    ids = submit_jobs(client, auth_headers, 4)

    assert all_pages(client, auth_headers, limit=2) == [ids[3:1:-1], ids[1::-1]]


def test_pages_do_not_shift_when_jobs_are_added_between_them(client, auth_headers):
    ids = submit_jobs(client, auth_headers, 4)
    first = list_jobs(client, auth_headers, limit=2)
    newer = submit_jobs(client, auth_headers, 2)

    second = list_jobs(client, auth_headers, limit=2, cursor=first["next_cursor"])

    assert [job["id"] for job in second["items"]] == ids[1::-1]
    assert second["next_cursor"] is None
    assert [job["id"] for job in list_jobs(client, auth_headers, limit=2)["items"]] == newer[::-1]


def test_jobs_with_the_same_creation_time_are_paged_by_id(client, auth_headers):
    ids = submit_jobs(client, auth_headers, 5)
    with get_sessionmaker()() as db:
        moment = db.get(Job, uuid.UUID(ids[0])).created_at
        db.execute(update(Job).values(created_at=moment))
        db.commit()

    pages = all_pages(client, auth_headers, limit=2)

    assert [len(page) for page in pages] == [2, 2, 1]
    assert [job_id for page in pages for job_id in page] == sorted(ids, reverse=True)


def test_jobs_can_be_filtered_by_status(client, auth_headers, worker):
    image = upload(client, auth_headers)
    succeeded = submit(client, auth_headers, image["id"])["id"]
    failed = submit(client, auth_headers, image["id"], {"crop": {"width": 999, "height": 1}})["id"]
    assert worker.run_once() and worker.run_once()
    queued = submit(client, auth_headers, image["id"])["id"]

    def ids(*statuses):
        return [job["id"] for job in list_jobs(client, auth_headers, status=statuses)["items"]]

    assert ids("succeeded") == [succeeded]
    assert ids("failed") == [failed]
    assert ids("queued") == [queued]
    assert ids("running") == []
    assert ids("succeeded", "failed") == [failed, succeeded]
    assert ids("queued", "succeeded", "queued") == [queued, succeeded]
    assert ids() == [queued, failed, succeeded]


def test_listed_jobs_are_described_like_get_job(client, auth_headers, worker):
    image = upload(client, auth_headers)
    succeeded = submit(client, auth_headers, image["id"])["id"]
    failed = submit(client, auth_headers, image["id"], {"crop": {"width": 999, "height": 1}})["id"]
    assert worker.run_once() and worker.run_once()
    queued = submit(client, auth_headers, image["id"])["id"]

    items = list_jobs(client, auth_headers)["items"]

    for job_id, item in zip((queued, failed, succeeded), items, strict=True):
        assert item == client.get(f"/jobs/{job_id}", headers=auth_headers).json()
    assert items[2]["result"]["id"] is not None
    assert items[1]["error"]["status_code"] == 422
    assert items[0]["result"] is None and items[0]["error"] is None


def test_an_older_job_that_finishes_late_is_found_by_listing_unfinished_jobs(
    client, auth_headers, worker
):
    """The reconciliation recipe in the README: the jobs you haven't heard about are the ones
    still listed as queued or running; once one is no longer there, it has finished (GET it)."""
    older, newer = submit_jobs(client, auth_headers, 2)
    # The older job waits (as after a storage outage) while the newer one runs.
    with get_sessionmaker()() as db:
        db.execute(
            update(Job)
            .where(Job.id == uuid.UUID(older))
            .values(available_at=text("now() + interval '1 hour'"))
        )
        db.commit()
    assert worker.run_once()

    def unfinished():
        return [
            job["id"]
            for job in list_jobs(client, auth_headers, status=["queued", "running"])["items"]
        ]

    assert unfinished() == [older]
    finished = list_jobs(client, auth_headers, status=["succeeded", "failed"])["items"]
    assert [job["id"] for job in finished] == [newer]

    with get_sessionmaker()() as db:
        db.execute(update(Job).where(Job.id == uuid.UUID(older)).values(available_at=text("now()")))
        db.commit()
    assert worker.run_once()

    assert unfinished() == []
    assert client.get(f"/jobs/{older}", headers=auth_headers).json()["status"] == "succeeded"


def test_only_your_own_jobs_are_listed(client, auth_headers):
    mine = submit_jobs(client, auth_headers, 1)
    bob = register(client, "bob")
    theirs = submit_jobs(client, bob, 2)

    assert all_pages(client, auth_headers) == [mine]
    assert all_pages(client, bob, limit=1) == [theirs[1:], theirs[:1]]
    assert list_jobs(client, register(client, "carol")) == {"items": [], "next_cursor": None}


def test_listing_jobs_requires_a_token(client):
    assert client.get("/jobs").status_code == 401


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"status": "done"},
        {"status": ["queued", "finished"]},
    ],
)
def test_invalid_listing_parameters_are_rejected(client, auth_headers, params):
    assert client.get("/jobs", headers=auth_headers, params=params).status_code == 422


@pytest.mark.parametrize(
    "cursor",
    [
        "not base64!",
        "@@@@",
        base64.b64encode(b"\xff\xfe,x").decode(),
        make_cursor("no comma here"),
        make_cursor("2026-10-05T12:00:00,") + "x",
        make_cursor(f"2026-10-05T12:00:00,{SOME_ID}"),  # No time zone.
        make_cursor(f"yesterday,{SOME_ID}"),
        make_cursor("2026-10-05T12:00:00+00:00,not-a-uuid"),
        make_cursor(f"2026-10-05T12:00:00+00:00,{SOME_ID},extra"),
        "",
    ],
)
def test_malformed_cursors_are_rejected(client, auth_headers, cursor):
    response = client.get("/jobs", headers=auth_headers, params={"cursor": cursor})

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid cursor"}


def test_a_cursor_from_elsewhere_only_moves_the_starting_point(client, auth_headers):
    ids = submit_jobs(client, auth_headers, 2)
    future = make_cursor(f"2999-01-01T00:00:00+00:00,{uuid.uuid4()}")
    past = make_cursor(f"2000-01-01T00:00:00+00:00,{uuid.uuid4()}")

    assert [job["id"] for job in list_jobs(client, auth_headers, cursor=future)["items"]] == (
        ids[::-1]
    )
    assert list_jobs(client, auth_headers, cursor=past)["items"] == []


def listing_plan(client, headers, **params) -> str:
    """The query plan of the listing GET /jobs makes with `params` (with a cursor)."""
    first = list_jobs(client, headers, limit=1)
    engine = get_sessionmaker().kw["bind"]
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "FROM jobs" in statement:
            statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        list_jobs(client, headers, cursor=first["next_cursor"], **params)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    [(statement, parameters)] = statements
    with engine.connect() as conn:
        # Within the connection's transaction, which is rolled back.
        conn.exec_driver_sql("SET LOCAL enable_seqscan = off")
        return str(conn.exec_driver_sql(f"EXPLAIN (FORMAT JSON) {statement}", parameters).scalar())


def test_the_listing_uses_the_owner_index(client, auth_headers):
    submit_jobs(client, auth_headers, 2)

    assert "ix_jobs_owner_id_created_at" in listing_plan(client, auth_headers)
    assert "ix_jobs_owner_id_created_at" in listing_plan(client, auth_headers, status="failed")


@pytest.mark.parametrize("statuses", [["queued", "running"], ["queued"], ["running"]])
def test_listing_unfinished_jobs_reads_only_those(client, auth_headers, statuses):
    """What catching up on webhook events lists: not the user's whole history."""
    submit_jobs(client, auth_headers, 2)

    assert "ix_jobs_owner_unfinished" in listing_plan(client, auth_headers, status=statuses)
