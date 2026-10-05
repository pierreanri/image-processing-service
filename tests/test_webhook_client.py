"""Webhook URL and address rules, and the HTTP client (app/webhook_client.py), and signing
(app/webhooks.py). No database."""

import base64
import hmac
import ipaddress
import logging
import socket
import threading
import time
import uuid

import httpcore
import pytest

from app.webhook_client import (
    AddressNotAllowed,
    GuardedBackend,
    Outcome,
    address_allowed,
    check_webhook_url,
    post,
    tls_context,
)
from app.webhooks import WebhookSecrets, sign
from tests.receiver import Reply
from tests.utils import verify_webhook

# --- URLs -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "normalized"),
    [
        ("https://hooks.example.com/in?x=1", "https://hooks.example.com/in?x=1"),
        ("https://hooks.example.com:8443/", "https://hooks.example.com:8443/"),
        ("https://Hooks.Example.com", "https://hooks.example.com/"),
        ("https://a-b.c-d.example/x", "https://a-b.c-d.example/x"),
        ("https://xn--bcher-kva.example/", "https://xn--bcher-kva.example/"),
        (
            "https://hooks.example.com/in;token=abc?x=1",
            "https://hooks.example.com/in;token=abc?x=1",
        ),
        ("https://hooks.example.com/%20a?b=%26", "https://hooks.example.com/%20a?b=%26"),
        ("https://[2606:4700::1111]/", "https://[2606:4700::1111]/"),
        ("https://[2606:4700:0:0::1111]:443/", "https://[2606:4700::1111]:443/"),
        ("https://8.8.8.8/x", "https://8.8.8.8/x"),
        ("https://hooks.example.com/?", "https://hooks.example.com/"),
    ],
)
def test_accepted_urls_are_normalized(url, normalized):
    assert check_webhook_url(url, allow_private=False) == normalized


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("ftp://hooks.example.com/", "must start with https://"),
        ("http://hooks.example.com/", "must start with https://"),
        ("hooks.example.com/in", "must start with https://"),
        ("https://user:pass@hooks.example.com/", "user name or password"),
        ("https://user@hooks.example.com/", "user name or password"),
        ("https://@hooks.example.com/", "user name or password"),
        ("https://hooks.example.com\\@evil.example/", "backslashes"),
        ("https://hooks.example.com/#frag", "fragment"),
        ("https://hooks.example.com/#", "fragment"),
        ("https://hooks.example.com:0/", "invalid port"),
        ("https://hooks.example.com:70000/", "invalid port"),
        ("https://hooks.example.com:http/", "invalid port"),
        ("https:///path", "must have a host"),
        ("https://hooks.example.com/a b", "spaces"),
        ("https://hooks.example.com/\tb", "printable"),
        ("https://bücher.example/", "ASCII"),
        ("https://hooks.example.com/" + "a" * 2023, "at most 2048"),
        ("https://hooks.example.com./", "invalid host name"),
        ("https://-hooks.example.com/", "invalid host name"),
        ("https://hooks_1.example.com/", "invalid host name"),
        ("https://" + "a" * 64 + ".example/", "invalid host name"),
        ("https://2130706433/", "numeric host name"),
        ("https://127.1/", "numeric host name"),
        ("https://0x7f.1/", "numeric host name"),
        ("https://0x7f000001/", "numeric host name"),
        ("https://017.0.0.1/", "numeric host name"),
        ("https://hooks/", "public host name"),
        ("https://localhost/", "public host name"),
        ("https://a.localhost/", "public host name"),
        ("https://printer.local/", "public host name"),
        ("https://metadata.google.internal/", "public host name"),
        ("https://nas.home.arpa/", "public host name"),
        ("https://[fe80::1%25eth0]/", "zone"),
        ("https://[::1/", "is not a valid URL"),
        ("https://[1.2.3.4]/", "is not a valid URL"),
        ("https://127.0.0.1/", "non-public address"),
        ("https://10.1.2.3/", "non-public address"),
        ("https://169.254.169.254/", "non-public address"),
        ("https://[::1]/", "non-public address"),
        ("https://[::ffff:127.0.0.1]/", "non-public address"),
        ("https://[64:ff9b::a9fe:a9fe]/", "non-public address"),
        ("https://0.0.0.0/", "non-public address"),
    ],
)
def test_refused_urls_say_why(url, reason):
    with pytest.raises(ValueError, match=reason):
        check_webhook_url(url, allow_private=False)


def test_private_networks_are_allowed_in_development():
    allowed = check_webhook_url("http://127.0.0.1:9000/in", allow_private=True)

    assert allowed == "http://127.0.0.1:9000/in"
    for url in ("http://receiver/", "http://localhost:9000/", "http://10.0.0.5/"):
        assert check_webhook_url(url, allow_private=True) == url
    for url in ("http://169.254.169.254/", "http://[fd00:ec2::254]/", "http://2130706433/"):
        with pytest.raises(ValueError):
            check_webhook_url(url, allow_private=True)


# --- Addresses ------------------------------------------------------------------------------------

REFUSED = [
    "127.0.0.1",
    "127.255.0.1",
    "10.0.0.1",
    "172.16.0.1",
    "172.31.255.255",
    "192.168.1.1",
    "100.64.0.1",  # Carrier-grade NAT.
    "100.100.100.200",  # Alibaba metadata.
    "169.254.169.254",  # Metadata of most clouds.
    "169.254.170.2",  # ECS task metadata.
    "168.63.129.16",  # Azure (is_global).
    "192.0.0.192",  # Oracle metadata.
    "0.0.0.0",
    "0.1.2.3",
    "224.0.0.1",
    "239.255.255.250",
    "255.255.255.255",
    "240.0.0.1",
    "198.18.0.1",  # Benchmarking.
    "192.0.2.1",  # Documentation.
    "::",
    "::1",
    "::127.0.0.1",
    "::ffff:127.0.0.1",
    "::ffff:10.0.0.1",
    "64:ff9b::7f00:1",  # NAT64 of 127.0.0.1.
    "64:ff9b::a9fe:a9fe",  # NAT64 of 169.254.169.254.
    "64:ff9b:1::a00:1",
    "2002:7f00:1::",  # 6to4.
    "2001::1",  # Teredo.
    "fe80::1",
    "fec0::1",
    "fc00::1",
    "fd00:ec2::254",  # AWS metadata.
    "fd20:ce::254",  # GCP metadata.
    "ff02::1",
    "2001:db8::1",
]
ALLOWED = [
    "8.8.8.8",
    "1.1.1.1",
    "93.184.216.34",
    "::ffff:8.8.8.8",
    "64:ff9b::808:808",
    "2606:4700::1111",
]
ALWAYS_REFUSED = [
    "169.254.169.254",
    "169.254.170.2",
    "168.63.129.16",
    "100.100.100.200",
    "192.0.0.192",
    "fd00:ec2::254",
    "fd20:ce::254",
    "fe80::1",
    "0.0.0.0",
    "0.1.2.3",
    "::",
    "224.0.0.1",
    "ff02::1",
    "64:ff9b::a9fe:a9fe",
    "::ffff:169.254.169.254",
]


@pytest.mark.parametrize("address", REFUSED)
def test_non_public_addresses_are_refused(address):
    assert not address_allowed(ipaddress.ip_address(address), allow_private=False)


@pytest.mark.parametrize("address", ALLOWED)
def test_public_addresses_are_allowed(address):
    assert address_allowed(ipaddress.ip_address(address), allow_private=False)


@pytest.mark.parametrize("address", ALWAYS_REFUSED)
def test_metadata_and_unroutable_addresses_are_refused_even_in_development(address):
    assert not address_allowed(ipaddress.ip_address(address), allow_private=True)


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "192.168.1.1", "::1", "fc00::1"])
def test_private_addresses_are_allowed_in_development(address):
    assert address_allowed(ipaddress.ip_address(address), allow_private=True)


# --- Connecting -----------------------------------------------------------------------------------


class RecordingResolver:
    def __init__(self, *answers: list[str]) -> None:
        self.answers = list(answers)
        self.lookups: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> list[str]:
        self.lookups.append((host, port))
        return self.answers.pop(0)


class RecordingBackend(httpcore.NetworkBackend):
    """Records connections instead of making them; then answers with `response` (or refuses)."""

    def __init__(self, response: bytes | None = None) -> None:
        self.connects: list[tuple[str, int]] = []
        self.response = response
        self.streams: list[FakeStream] = []

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connects.append((host, port))
        if self.response is None:
            raise httpcore.ConnectError("refused")
        self.streams.append(FakeStream(self.response))
        return self.streams[-1]


class FakeStream(httpcore.NetworkStream):
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.written = b""
        self.tls_hostname = None

    def read(self, max_bytes, timeout=None):
        data, self.response = self.response[:max_bytes], self.response[max_bytes:]
        return data

    def write(self, buffer, timeout=None):
        self.written += buffer

    def close(self):
        pass

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.tls_hostname = server_hostname
        return self


def backend(resolver, inner, allow_private=False, timeout=5.0):
    return GuardedBackend(
        allow_private=allow_private,
        deadline=time.monotonic() + timeout,
        resolve=resolver,
        inner=inner,
    )


@pytest.mark.parametrize(
    "answer", [["10.0.0.1"], ["169.254.169.254"], ["::1"], ["8.8.8.8", "127.0.0.1"], []]
)
def test_names_resolving_to_refused_addresses_are_never_connected_to(answer):
    inner = RecordingBackend()

    with pytest.raises(AddressNotAllowed):
        backend(RecordingResolver(answer), inner).connect_tcp("hooks.example.com", 443)

    assert inner.connects == []


def test_the_checked_address_is_the_one_connected_to():
    """One lookup, of the absolute name: a second answer (DNS rebinding) is never asked for."""
    resolver = RecordingResolver(["93.184.216.34"], ["127.0.0.1"])
    inner = RecordingBackend(b"HTTP/1.1 204 No Content\r\n\r\n")

    stream = backend(resolver, inner).connect_tcp("hooks.example.com", 443)

    assert resolver.lookups == [("hooks.example.com.", 443)]
    assert inner.connects == [("93.184.216.34", 443)]
    stream.start_tls(tls_context(), "hooks.example.com")
    assert inner.streams[0].tls_hostname == "hooks.example.com"


def test_every_resolved_address_is_tried_in_turn():
    resolver = RecordingResolver(["8.8.8.8", "8.8.4.4", "8.8.8.8"])
    inner = RecordingBackend()

    with pytest.raises(httpcore.ConnectError):
        backend(resolver, inner).connect_tcp("hooks.example.com", 443)

    assert inner.connects == [("8.8.8.8", 443), ("8.8.4.4", 443)]


def test_ip_literals_are_not_looked_up():
    resolver = RecordingResolver()
    inner = RecordingBackend(b"")

    backend(resolver, inner).connect_tcp("8.8.8.8", 443)

    assert resolver.lookups == []
    assert inner.connects == [("8.8.8.8", 443)]


def test_the_sni_and_certificate_name_is_the_url_host(tls_receiver, test_ca):
    resolver = RecordingResolver(["127.0.0.1"])

    outcome = post(
        tls_receiver.url(),
        {},
        b"{}",
        timeout=5,
        allow_private=True,
        resolve=resolver,
        ssl_context=test_ca.client_context(),
    )

    assert outcome == Outcome(status_code=200)
    assert resolver.lookups == [("receiver.test.", tls_receiver.port)]
    assert tls_receiver.requests[0].headers["host"] == f"receiver.test:{tls_receiver.port}"


def test_a_certificate_for_another_name_fails_the_handshake(tls_receiver, test_ca):
    url = tls_receiver.url().replace("receiver.test", "other.test")

    outcome = post(
        url,
        {},
        b"{}",
        timeout=5,
        allow_private=True,
        resolve=test_ca.resolve,
        ssl_context=test_ca.client_context(),
    )

    assert outcome == Outcome(error="TLS handshake failed")
    assert tls_receiver.requests == []


def test_certificates_are_verified_against_public_cas(tls_receiver, test_ca):
    outcome = post(
        tls_receiver.url(), {}, b"{}", timeout=5, allow_private=True, resolve=test_ca.resolve
    )

    assert outcome == Outcome(error="TLS handshake failed")


def test_each_thread_has_its_own_tls_context():
    """httpcore sets the ALPN protocols of the context it is given on every connection."""
    contexts = []
    thread = threading.Thread(target=lambda: contexts.append(tls_context()))
    thread.start()
    thread.join()

    assert tls_context() is tls_context()
    assert contexts[0] is not tls_context()
    assert tls_context().check_hostname


def test_an_ipv6_host_header_is_bracketed():
    inner = RecordingBackend(b"HTTP/1.1 204 No Content\r\n\r\n")

    outcome = post(
        "http://[2606:4700::1111]:8080/in?x=1",
        {"webhook-id": "abc"},
        b'{"a":1}',
        timeout=5,
        allow_private=True,
        network=inner,
    )

    assert outcome == Outcome(status_code=204)
    assert inner.connects == [("2606:4700::1111", 8080)]
    request = inner.streams[0].written
    assert request.startswith(b"POST /in?x=1 HTTP/1.1\r\n")
    assert b"\r\nHost: [2606:4700::1111]:8080\r\n" in request
    assert b"\r\nwebhook-id: abc\r\n" in request
    assert b"\r\nContent-Type: application/json\r\n" in request
    assert b"\r\nUser-Agent: imgsvc-webhooks/1\r\n" in request
    assert request.endswith(b'\r\n\r\n{"a":1}')


# --- Sending --------------------------------------------------------------------------------------


def send(url, timeout=5.0, **kwargs) -> Outcome:
    return post(url, {"webhook-id": "x"}, b"{}", timeout=timeout, allow_private=True, **kwargs)


def test_the_body_and_headers_arrive(receiver):
    outcome = post(
        receiver.url("/in;token=abc?x=1"),
        {"webhook-id": "id-1", "webhook-signature": "v1,abc"},
        b'{"hello":"world"}',
        timeout=5,
        allow_private=True,
    )

    assert outcome == Outcome(status_code=200)
    [received] = receiver.requests
    assert (received.method, received.path) == ("POST", "/in;token=abc?x=1")
    assert received.body == b'{"hello":"world"}'
    assert received.headers["content-type"] == "application/json"
    assert received.headers["webhook-id"] == "id-1"
    assert received.headers["webhook-signature"] == "v1,abc"
    assert received.headers["host"] == f"127.0.0.1:{receiver.port}"


@pytest.mark.parametrize("status", [200, 201, 202, 204, 299])
def test_any_2xx_status_delivers(receiver, status):
    receiver.default = Reply(status=status)

    assert send(receiver.url()).delivered


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (301, "HTTP 301 (redirects are not followed)"),
        (304, "HTTP 304 (redirects are not followed)"),
        (400, "HTTP 400"),
        (401, "HTTP 401"),
        (410, "HTTP 410"),
        (500, "HTTP 500"),
        (503, "HTTP 503"),
    ],
)
def test_other_statuses_fail(receiver, status, error):
    receiver.default = Reply(status=status)

    outcome = send(receiver.url())

    assert outcome.delivered == (error is None)
    assert outcome.error == error
    assert outcome.status_code == status


def test_redirects_are_not_followed(receivers):
    first, second = receivers(), receivers()
    first.default = Reply(status=302, headers={"Location": second.url()})

    outcome = send(first.url())

    assert outcome == Outcome(status_code=302, error="HTTP 302 (redirects are not followed)")
    assert second.requests == []


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("120", 120),
        (" 7 ", 7),
        ("0", 0),
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),
        ("-5", None),
        ("1.5", None),
    ],
)
def test_retry_after_is_read_in_seconds(receiver, value, seconds):
    receiver.default = Reply(status=503, headers={"Retry-After": value})

    assert send(receiver.url()).retry_after == seconds


@pytest.mark.parametrize("tls", [False, True], ids=["http", "https"])
@pytest.mark.parametrize(
    "make_reply",
    [
        lambda: Reply(delay=3),
        lambda: Reply(hang=threading.Event()),
        lambda: Reply(trickle=0.05),
    ],
    ids=["slow", "hanging", "trickling"],
)
def test_slow_hanging_and_trickling_endpoints_end_at_the_deadline(
    receiver, tls_receiver, test_ca, tls, make_reply
):
    target = tls_receiver if tls else receiver
    target.default = make_reply()
    started = time.monotonic()

    outcome = send(
        target.url(), timeout=0.5, resolve=test_ca.resolve, ssl_context=test_ca.client_context()
    )

    assert outcome == Outcome(error="Timed out after 0.5s")
    assert 0.5 <= time.monotonic() - started < 1.5
    assert target.first_request.is_set()


def test_the_deadline_also_covers_the_tls_handshake(receivers, test_ca):
    """A server that accepts the connection and never answers the handshake."""
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        started = time.monotonic()

        outcome = send(
            f"https://receiver.test:{server.getsockname()[1]}/",
            timeout=0.5,
            resolve=test_ca.resolve,
            ssl_context=test_ca.client_context(),
        )

    assert outcome == Outcome(error="Timed out after 0.5s")
    assert time.monotonic() - started < 1.5


def test_large_responses_are_not_read(receiver):
    receiver.default = Reply(body_bytes=1024**3)
    started = time.monotonic()

    outcome = send(receiver.url())

    assert outcome == Outcome(status_code=200)
    assert time.monotonic() - started < 2


def test_invalid_responses_fail(receivers):
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()

        def answer():
            connection, _ = server.accept()
            with connection:
                connection.recv(65536)
                connection.sendall(b"NOT HTTP AT ALL\r\n\r\n")

        thread = threading.Thread(target=answer)
        thread.start()
        outcome = send(f"http://127.0.0.1:{server.getsockname()[1]}/")
        thread.join(5)

    assert outcome == Outcome(error="Invalid HTTP response")


def test_a_connection_closed_without_an_answer_is_lost():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()

        def hang_up():
            connection, _ = server.accept()
            connection.recv(65536)
            connection.close()

        thread = threading.Thread(target=hang_up)
        thread.start()
        outcome = send(f"http://127.0.0.1:{server.getsockname()[1]}/")
        thread.join(5)

    assert outcome.error in ("Connection lost", "Invalid HTTP response")
    assert not outcome.delivered


def test_errors_reveal_nothing_about_the_network(closed_port, caplog):
    """A refused connection, a name resolving to a private address and a name that doesn't
    resolve all read the same."""

    def no_such_name(host, port):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    outcomes = [
        post(f"http://127.0.0.1:{closed_port}/", {}, b"{}", timeout=5, allow_private=True),
        post(
            "https://hooks.example.com/",
            {},
            b"{}",
            timeout=5,
            allow_private=False,
            resolve=lambda host, port: ["10.0.0.7"],
        ),
        post(
            "https://hooks.example.com/",
            {},
            b"{}",
            timeout=5,
            allow_private=False,
            resolve=no_such_name,
        ),
    ]

    assert outcomes == [Outcome(error="Could not connect")] * 3
    # Operators see the refused address.
    assert "hooks.example.com refused: it resolves to 10.0.0.7" in caplog.text


def test_urls_that_are_no_longer_allowed_are_not_sent_to(receiver):
    outcome = post(receiver.url(), {}, b"{}", timeout=5, allow_private=False)

    assert outcome == Outcome(error="The URL must start with https://")
    assert receiver.connections == 0


@pytest.mark.parametrize("tls", [False, True], ids=["http", "https"])
def test_proxy_variables_are_ignored(
    receiver, tls_receiver, test_ca, closed_port, monkeypatch, tls
):
    proxy = f"http://127.0.0.1:{closed_port}"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, proxy)
        monkeypatch.setenv(name.lower(), proxy)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    target = tls_receiver if tls else receiver

    outcome = send(target.url(), resolve=test_ca.resolve, ssl_context=test_ca.client_context())

    assert outcome == Outcome(status_code=200)
    assert len(target.requests) == 1


def test_each_attempt_opens_its_own_connection(receiver):
    for _ in range(3):
        assert send(receiver.url()).delivered

    assert receiver.connections == 3


def test_refused_addresses_are_logged_for_operators_only(caplog):
    caplog.set_level(logging.WARNING)

    post(
        "https://hooks.example.com/",
        {},
        b"{}",
        timeout=5,
        allow_private=False,
        resolve=lambda host, port: ["8.8.8.8", "192.168.0.10"],
    )

    assert "resolves to 192.168.0.10" in caplog.text
    assert "8.8.8.8" not in caplog.text


# --- Signing --------------------------------------------------------------------------------------


def test_signatures_match_the_standard_webhooks_test_vector():
    key = base64.b64decode("MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw")

    signature = sign(key, "msg_p5jXN8AQM9LWM0D4loKWxJek", "1614265330", b'{"test": 2432232314}')

    assert signature == "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="


def test_secrets_are_derived_per_user_and_salt_under_their_own_label():
    secrets = WebhookSecrets("x" * 40)
    alice, bob = uuid.uuid4(), uuid.uuid4()
    salt, other_salt = b"s" * 16, b"t" * 16

    key = secrets.key(alice, salt)

    assert len(key) == 32
    label_key = hmac.digest(b"x" * 40, b"imgsvc webhook secrets v1", "sha256")
    assert key == hmac.digest(label_key, alice.bytes + salt, "sha256")
    assert key != hmac.digest(b"x" * 40, alice.bytes + salt, "sha256")
    assert len({key, secrets.key(bob, salt), secrets.key(alice, other_salt)}) == 3
    assert WebhookSecrets("y" * 40).key(alice, salt) != key
    assert secrets.secret(alice, salt) == "whsec_" + base64.b64encode(key).decode()
    assert len(WebhookSecrets.new_salt()) == 16


def test_signed_headers_verify_with_the_readme_code(clock):
    clock.now = time.time()
    secrets = WebhookSecrets("x" * 40, clock=clock)
    user, event = uuid.uuid4(), uuid.uuid4()
    new, old = b"n" * 16, b"o" * 16
    body = b'{"id":"1"}'

    headers = secrets.headers(event, body, user, (new, old))

    assert headers["webhook-id"] == str(event)
    assert headers["webhook-timestamp"] == str(int(clock.now))
    assert len(headers["webhook-signature"].split(" ")) == 2
    assert verify_webhook(secrets.secret(user, new), headers, body)
    assert verify_webhook(secrets.secret(user, old), headers, body)
    assert not verify_webhook(secrets.secret(user, b"z" * 16), headers, body)
    assert not verify_webhook(secrets.secret(user, new), headers, body + b" ")
    clock.advance(301)
    stale = secrets.headers(event, body, user, (new,))
    clock.advance(-301)
    assert not verify_webhook(secrets.secret(user, new), stale, body)
