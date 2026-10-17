import base64
import datetime
import hmac
import io
import socket
import ssl
import time
import uuid
from pathlib import Path

import jwt
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import event, select, text, update

from app.db import get_sessionmaker
from app.models import WebhookDelivery, WebhookEndpoint
from app.ratelimit import UNCHECKED, Limit, LimitState, RateLimitDecision
from app.storage import LocalStorage


def register(client: TestClient, username: str = "alice", password: str = "password123") -> dict:
    response = client.post("/register", json={"username": username, "password": password})
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def user_id_of(headers: dict) -> uuid.UUID:
    token = headers["Authorization"].removeprefix("Bearer ")
    return uuid.UUID(jwt.decode(token, options={"verify_signature": False})["sub"])


def share(client: TestClient, headers: dict, image_id: str, **body) -> dict:
    """Create a share link to an image (POST /images/{id}/share-links) and return it."""
    response = client.post(f"/images/{image_id}/share-links", headers=headers, json=body or None)
    assert response.status_code == 200, response.text
    return response.json()


def stored_keys(storage) -> list[str]:
    """Every key in a LocalStorage or S3Storage (temporary files included), sorted."""
    if isinstance(storage, LocalStorage):
        files = (path for path in storage.root.rglob("*") if path.is_file())
        return sorted(path.relative_to(storage.root).as_posix() for path in files)
    response = storage.client.list_objects_v2(Bucket=storage.bucket)
    return sorted(item["Key"] for item in response.get("Contents", []))


def make_image_bytes(
    fmt: str = "PNG", size: tuple[int, int] = (64, 48), mode: str = "RGB", color="red"
) -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, color).save(buffer, format=fmt)
    return buffer.getvalue()


class InMemoryVariantCache:
    """Stand-in for app.cache.VariantCache that keeps entries in a dict (items over
    `max_item_bytes`, if given, are only remembered, as there)."""

    def __init__(self, max_item_bytes: int | None = None) -> None:
        self.max_item_bytes = max_item_bytes
        self.entries: dict[uuid.UUID, dict[str, bytes]] = {}
        self.reads: list[tuple[uuid.UUID, str]] = []

    def get(self, image_id: uuid.UUID, variant: str) -> bytes | None:
        self.reads.append((image_id, variant))
        return self.entries.get(image_id, {}).get(variant)

    def set(self, image_id: uuid.UUID, variant: str, data: bytes) -> None:
        if self.max_item_bytes is not None and len(data) > self.max_item_bytes:
            variant, data = f"{variant}:too-large", b""
        self.entries.setdefault(image_id, {})[variant] = data

    def too_large(self, image_id: uuid.UUID, variant: str) -> bool:
        self.reads.append((image_id, f"{variant}:too-large"))
        return f"{variant}:too-large" in self.entries.get(image_id, {})

    def invalidate(self, image_id: uuid.UUID) -> None:
        self.entries.pop(image_id, None)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


MINUTE = Limit("minute", 30, 60)
HOUR = Limit("hour", 500, 3600)
ALLOWED = RateLimitDecision(True, (LimitState(MINUTE, 29, 60), LimitState(HOUR, 499, 3600)))
REJECTED = RateLimitDecision(False, (LimitState(MINUTE, 0, 13), LimitState(HOUR, 470, 2811)))


class FakeRateLimiter:
    """Stand-in for app.ratelimit.RateLimiter that records who was counted (and checked), and
    at what cost, and returns a canned decision."""

    def __init__(self, decision: RateLimitDecision = UNCHECKED) -> None:
        self.decision = decision
        self.hits: list[uuid.UUID] = []
        self.costs: list[int] = []
        self.checks: list[tuple[uuid.UUID, int]] = []

    def hit(self, user_id: uuid.UUID, cost: int = 1) -> RateLimitDecision:
        self.hits.append(user_id)
        self.costs.append(cost)
        return self.decision

    def check(self, user_id: uuid.UUID, cost: int = 1) -> RateLimitDecision:
        self.checks.append((user_id, cost))
        return self.decision


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


# --- Webhooks -------------------------------------------------------------------------------------

WEBHOOK_URL = "https://hooks.example.com/in?source=imgsvc"


def set_webhook(client: TestClient, headers: dict, url: str = WEBHOOK_URL) -> dict:
    """PUT /me/webhook, expecting it to create the webhook; returns the body (with the secret)."""
    response = client.put("/me/webhook", headers=headers, json={"url": url})
    assert response.status_code == 201, response.text
    return response.json()


def endpoint_of(user_id: uuid.UUID) -> WebhookEndpoint | None:
    with get_sessionmaker()() as db:
        return db.get(WebhookEndpoint, user_id)


def update_endpoint(user_id: uuid.UUID, **values) -> None:
    with get_sessionmaker()() as db:
        db.execute(
            update(WebhookEndpoint).where(WebhookEndpoint.user_id == user_id).values(**values)
        )
        db.commit()


def deliveries(user_id: uuid.UUID | None = None) -> list[WebhookDelivery]:
    """Webhook deliveries (a user's), oldest first."""
    query = select(WebhookDelivery).order_by(WebhookDelivery.created_at, WebhookDelivery.id)
    if user_id is not None:
        query = query.where(WebhookDelivery.user_id == user_id)
    with get_sessionmaker()() as db:
        return list(db.scalars(query))


def delivery_of(job_id) -> WebhookDelivery | None:
    with get_sessionmaker()() as db:
        return db.scalar(
            select(WebhookDelivery).where(WebhookDelivery.job_id == uuid.UUID(str(job_id)))
        )


def update_delivery(delivery_id: uuid.UUID, **values) -> None:
    with get_sessionmaker()() as db:
        db.execute(
            update(WebhookDelivery).where(WebhookDelivery.id == delivery_id).values(**values)
        )
        db.commit()


def seconds_until(moment) -> float:
    """Seconds from the database's clock to `moment`."""
    with get_sessionmaker()() as db:
        return (moment - db.scalar(select(text("now()")))).total_seconds()


def verify_webhook(secret: str, headers, body: bytes, tolerance: int = 300) -> bool:
    """The README's verification code, verbatim."""
    key = base64.b64decode(secret.removeprefix("whsec_"))
    msg_id, ts = headers["webhook-id"], headers["webhook-timestamp"]
    if abs(time.time() - int(ts)) > tolerance:
        return False
    expected = base64.b64encode(
        hmac.digest(key, f"{msg_id}.{ts}.".encode() + body, "sha256")
    ).decode()
    return any(
        hmac.compare_digest(expected, sig.removeprefix("v1,"))
        for sig in headers["webhook-signature"].split()
        if sig.startswith("v1,")
    )


class TestCA:
    """A throwaway certificate authority, and a certificate it issued for receiver.test."""

    __test__ = False  # Not a test class.
    HOST = "receiver.test"

    def __init__(self, directory: Path) -> None:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        now = datetime.datetime.now(datetime.UTC)
        ca_key = ec.generate_private_key(ec.SECP256R1())
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "imgsvc test CA")])
        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(ca_name)
            .issuer_name(ca_name)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
            )
            .sign(ca_key, hashes.SHA256())
        )
        key = ec.generate_private_key(ec.SECP256R1())
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, self.HOST)]))
            .issuer_name(ca_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(self.HOST)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                critical=False,
            )
            .sign(ca_key, hashes.SHA256())
        )
        self.ca_pem = ca_cert.public_bytes(serialization.Encoding.PEM).decode()
        self.cert_file = directory / "receiver.pem"
        self.key_file = directory / "receiver.key"
        self.cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        self.key_file.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )

    def server_context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert_file, self.key_file)
        return context

    def client_context(self) -> ssl.SSLContext:
        """Trusts this CA only, as the webhook client's default context trusts certifi's."""
        return ssl.create_default_context(cadata=self.ca_pem)

    @staticmethod
    def resolve(host: str, port: int) -> list[str]:
        """A resolver that knows receiver.test (and other names under .test) as 127.0.0.1."""
        if host.removesuffix(".").endswith(".test"):
            return ["127.0.0.1"]
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")


class RecordingStop:
    """Stands in for the worker's stop event: records how long each wait() was asked to last,
    returns at once, and is set after `waits` of them (or, so that a loop that stopped waiting
    fails instead of spinning forever, after many more checks)."""

    def __init__(self, waits: int) -> None:
        self.waits: list[float] = []
        self._limit = waits
        self._checks = 0

    def is_set(self) -> bool:
        self._checks += 1
        return len(self.waits) >= self._limit or self._checks > 10 * self._limit

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        return self.is_set()


# --- Signed URLs ----------------------------------------------------------------------------------


def create_url_signing_key(client: TestClient, headers: dict) -> dict:
    """POST /me/url-signing-key, expecting a new key; returns the body (with the key)."""
    response = client.post("/me/url-signing-key", headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def count_statements():
    """Counts the SQL statements run from here on: call the result to read the count."""
    statements = []
    engine = get_sessionmaker().kw["bind"]

    def count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    return lambda: (event.remove(engine, "before_cursor_execute", count), len(statements))[1]
