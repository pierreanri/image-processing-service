"""Which webhook URLs and addresses may be used.

Users choose where workers send requests, so a webhook could otherwise point workers at the
service's own network: its database, a cloud's instance metadata service, an admin interface.
URLs are checked when they are registered and before every attempt, without resolving them (a
lookup then would prove nothing: the name can resolve elsewhere by the time of the request). The
addresses a name resolves to are checked when connecting.

Requests are sent with httpcore (the only module using it) through GuardedBackend: it resolves the
host once, refuses it unless every address is allowed, and connects to an address it checked, so
a name can't resolve to a public address for the check and a private one for the connection. No
redirect is followed and no proxy used (httpcore does neither), and no connection is reused. One
deadline bounds each attempt as a whole, so a server can't hold a sender by trickling its answer,
and the response's body is never read.
"""

import ipaddress
import logging
import re
import socket
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import certifi
import httpcore

logger = logging.getLogger(__name__)

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

MAX_URL_LENGTH = 2048

# Refused even with WEBHOOK_ALLOW_PRIVATE_NETWORKS: cloud instance metadata services (AWS,
# GCP, Azure, Alibaba, Oracle), which hand out credentials, and addresses no service listens on.
_METADATA_ADDRESSES = frozenset(
    ipaddress.ip_address(address)
    for address in (
        "168.63.129.16",  # Azure (is_global)
        "100.100.100.200",  # Alibaba
        "192.0.0.192",  # Oracle
        "fd00:ec2::254",  # AWS (IPv6)
        "fd20:ce::254",  # GCP (IPv6)
    )
)
_THIS_NETWORK = ipaddress.ip_network("0.0.0.0/8")
# IPv6 prefixes that embed or reach IPv4 addresses, or that ipaddress reports as global though
# they aren't routed on the internet.
_NOT_PUBLIC = tuple(
    ipaddress.ip_network(network)
    for network in (
        "2001::/32",  # Teredo
        "2002::/16",  # 6to4
        "fec0::/10",  # Deprecated site-local (is_global)
        "64:ff9b:1::/48",  # Local-use NAT64
    )
)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")

_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# A last label like these makes the name a legacy spelling of an IPv4 address (2130706433,
# 127.1, 0x7f.1), which some resolvers and HTTP clients turn into that address.
_NUMERIC_LABEL = re.compile(r"[0-9]+|0x[0-9a-f]*")
_LOCAL_NAMES = ("localhost",)
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")


def address_allowed(ip: IPAddress, allow_private: bool) -> bool:
    """Whether webhooks may be sent to `ip`: a public unicast address (any address but the
    always-refused ones, with `allow_private`)."""
    if isinstance(ip, ipaddress.IPv6Address):
        # An IPv4 address carried in IPv6 is judged as itself, so ::ffff:127.0.0.1 is loopback,
        # while a DNS64 network's 64:ff9b::808:808 still reaches the public 8.8.8.8.
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in _NAT64:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if (
        ip.is_link_local  # Including 169.254.169.254, most clouds' metadata service.
        or ip.is_multicast
        or ip.is_unspecified
        or ip in _THIS_NETWORK
        or ip in _METADATA_ADDRESSES
    ):
        return False
    if allow_private:
        return True
    return ip.is_global and not ip.is_reserved and not any(ip in net for net in _NOT_PUBLIC)


def check_webhook_url(url: str, allow_private: bool) -> str:
    """The normalized form of a webhook URL; ValueError says why it can't be used. Only https
    URLs (and http with `allow_private`) of public hosts are allowed. No DNS lookup is made."""
    if len(url) > MAX_URL_LENGTH:
        raise ValueError(f"must be at most {MAX_URL_LENGTH} characters long")
    if not url.isascii() or not url.isprintable() or " " in url or "\\" in url:
        raise ValueError(
            "must be printable ASCII without spaces or backslashes (percent-encode other "
            "characters, and give internationalized host names in their xn-- form)"
        )
    try:
        parts = urlsplit(url)
    except ValueError:  # Such as unbalanced brackets around an IPv6 address.
        raise ValueError("is not a valid URL") from None
    scheme = parts.scheme.lower()
    if scheme != "https" and not (scheme == "http" and allow_private):
        raise ValueError("must start with https://")
    if "#" in url:
        raise ValueError("must not have a fragment (#...)")
    if "@" in parts.netloc:
        raise ValueError("must not contain a user name or password")
    try:
        port = parts.port
    except ValueError:
        port = 0
    if port == 0:
        raise ValueError("has an invalid port")
    host = parts.hostname
    if not host:
        raise ValueError("must have a host")

    if parts.netloc.startswith("["):
        try:
            ip = ipaddress.IPv6Address(host)
        except ValueError:
            raise ValueError("has an invalid IPv6 address") from None
        if ip.scope_id is not None:
            raise ValueError("must not have an IPv6 zone (%...)")
        host = f"[{ip}]"
    else:
        try:
            ip = ipaddress.IPv4Address(host)
        except ValueError:
            ip = None
            _check_host_name(host, allow_private)
    if ip is not None and not address_allowed(ip, allow_private):
        raise ValueError(
            "must not point to a private, loopback, link-local or otherwise non-public address"
        )

    netloc = host if port is None else f"{host}:{port}"
    normalized = urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))
    # Adding the "/" of an empty path can make it longer.
    if len(normalized) > MAX_URL_LENGTH:
        raise ValueError(f"must be at most {MAX_URL_LENGTH} characters long")
    return normalized


def _check_host_name(host: str, allow_private: bool) -> None:
    labels = host.split(".")
    if len(host) > 253 or not all(_LABEL.fullmatch(label) for label in labels):
        raise ValueError(
            "has an invalid host name (letters, digits and hyphens, without a trailing dot; "
            "internationalized names in their xn-- form)"
        )
    if _NUMERIC_LABEL.fullmatch(labels[-1]):
        raise ValueError("has a numeric host name: give an IPv4 address as four decimal numbers")
    if allow_private:
        return
    if len(labels) == 1 or host in _LOCAL_NAMES or host.endswith(_LOCAL_SUFFIXES):
        raise ValueError("must have a public host name")


# --- Sending ------------------------------------------------------------------------------------

USER_AGENT = "imgsvc-webhooks/1"
# The only errors users are shown (as last_error): never what a receiver, a resolver or the
# network said, which could reveal what lies inside the service's network.
COULD_NOT_CONNECT = "Could not connect"
TLS_FAILED = "TLS handshake failed"
CONNECTION_LOST = "Connection lost"
INVALID_RESPONSE = "Invalid HTTP response"
INTERNAL_ERROR = "Internal error"

# Resolves a host name to numeric addresses (getaddrinfo, by default).
Resolver = Callable[[str, int], list[str]]


@dataclass(frozen=True)
class Outcome:
    """The outcome of one attempt to deliver an event."""

    # The response's status; None if there was none.
    status_code: int | None = None
    # Why the attempt failed (one of the messages above, or the status); None if it succeeded.
    error: str | None = None
    # Seconds the endpoint asked to wait (a Retry-After header in seconds), if it failed.
    retry_after: int | None = None
    # The attempt failed because of this service, not the endpoint.
    internal: bool = False

    @property
    def delivered(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300


class AddressNotAllowed(httpcore.ConnectError):
    """The host has no address, or one that webhooks may not be sent to."""


class TLSFailed(httpcore.ConnectError):
    """The TLS handshake failed, such as for a certificate that isn't valid for the host."""


def system_resolver(host: str, port: int) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


class GuardedBackend(httpcore.NetworkBackend):
    """Connects only to allowed addresses, and gives every stream the attempt's deadline (a
    time.monotonic() value)."""

    def __init__(
        self,
        *,
        allow_private: bool,
        deadline: float,
        resolve: Resolver = system_resolver,
        inner: httpcore.NetworkBackend | None = None,
    ) -> None:
        self._allow_private = allow_private
        self._deadline = deadline
        self._resolve = resolve
        self._inner = inner or httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: object = None,
    ) -> httpcore.NetworkStream:
        try:
            ipaddress.ip_address(host)
            addresses = [host]
        except ValueError:
            # Absolute, so that resolv.conf's search domains are never tried. Not in development,
            # where it would skip /etc/hosts too (and names such as host.docker.internal).
            name = host if self._allow_private else host + "."
            try:
                addresses = self._resolve(name, port)
            except OSError:
                raise AddressNotAllowed(f"{host} could not be resolved") from None
        ips = list(dict.fromkeys(ipaddress.ip_address(address) for address in addresses))
        refused = [ip for ip in ips if not address_allowed(ip, self._allow_private)]
        if not ips or refused:
            # Never connect to any of them: a name with a private address may be an attack.
            logger.warning(
                "Webhook host %s refused: it resolves to %s",
                host,
                ", ".join(map(str, refused)) or "no address",
            )
            raise AddressNotAllowed(f"{host} resolves to an address that is not allowed")
        error: httpcore.ConnectError | httpcore.ConnectTimeout | None = None
        for ip in ips:
            remaining = _remaining(self._deadline, httpcore.ConnectTimeout)
            try:
                stream = self._inner.connect_tcp(
                    str(ip),
                    port,
                    timeout=remaining if timeout is None else min(timeout, remaining),
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                error = exc
                continue
            return DeadlineStream(stream, self._deadline)
        raise error

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


class DeadlineStream(httpcore.NetworkStream):
    """A stream whose reads and writes, including the TLS handshake's, end by a deadline."""

    def __init__(self, inner: httpcore.NetworkStream, deadline: float) -> None:
        self._inner = inner
        self._deadline = deadline

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._inner.read(max_bytes, self._timeout(timeout, httpcore.ReadTimeout))

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._inner.write(buffer, self._timeout(timeout, httpcore.WriteTimeout))

    def close(self) -> None:
        self._inner.close()

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        try:
            stream = self._inner.start_tls(
                ssl_context, server_hostname, self._timeout(timeout, httpcore.ConnectTimeout)
            )
        except httpcore.ConnectError as exc:
            raise TLSFailed(str(exc)) from exc
        # The stream every later read and write goes to: it keeps the deadline.
        return DeadlineStream(stream, self._deadline)

    def get_extra_info(self, info: str) -> object:
        return self._inner.get_extra_info(info)

    def _timeout(self, timeout: float | None, error: type[Exception]) -> float:
        remaining = _remaining(self._deadline, error)
        return remaining if timeout is None else min(timeout, remaining)


def _remaining(deadline: float, error: type[Exception]) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise error("The attempt's deadline passed")
    return remaining


_contexts = threading.local()


def tls_context() -> ssl.SSLContext:
    """This thread's TLS context: certificates verified against certifi's CAs, TLS 1.2 or
    later. One per thread, as httpcore changes the context it is given on every connection."""
    context = getattr(_contexts, "context", None)
    if context is None:
        context = _contexts.context = ssl.create_default_context(cafile=certifi.where())
    return context


def post(
    url: str,
    headers: dict[str, str],
    body: bytes,
    *,
    timeout: float,
    allow_private: bool,
    resolve: Resolver = system_resolver,
    ssl_context: ssl.SSLContext | None = None,
    network: httpcore.NetworkBackend | None = None,
) -> Outcome:
    """POST a JSON `body` to a webhook `url`, within `timeout` seconds in all; only the
    response's status and headers are read. Tests may give the resolver, the TLS context and the
    backend that connects to the checked addresses."""
    deadline = time.monotonic() + timeout
    try:
        # Again: the settings may have changed since the URL was set.
        url = check_webhook_url(url, allow_private)
    except ValueError as exc:
        return Outcome(error=f"The URL {exc}"[:100])
    parts = urlsplit(url)
    # Built from its parts: parsing the URL again would drop ";parameters" from its path.
    request_url = httpcore.URL(
        scheme=parts.scheme,
        host=parts.hostname,
        port=parts.port,
        target=parts.path + (f"?{parts.query}" if parts.query else ""),
    )
    request_headers = [
        # httpcore would leave the brackets off an IPv6 address.
        ("Host", parts.netloc),
        ("User-Agent", USER_AGENT),
        ("Content-Type", "application/json"),
        *headers.items(),
    ]
    backend = GuardedBackend(
        allow_private=allow_private, deadline=deadline, resolve=resolve, inner=network
    )
    timeouts = dict.fromkeys(("connect", "read", "write", "pool"), timeout)
    try:
        with (
            httpcore.ConnectionPool(
                ssl_context=ssl_context or tls_context(),
                network_backend=backend,
                max_connections=1,
                max_keepalive_connections=0,
                retries=0,
            ) as pool,
            pool.stream(
                "POST",
                request_url,
                headers=request_headers,
                content=body,
                extensions={"timeout": timeouts},
            ) as response,
        ):
            # Leaving without reading the body closes the connection.
            status_code = response.status
            retry_after = _retry_after(response.headers)
    except httpcore.TimeoutException:
        return Outcome(error=f"Timed out after {timeout:g}s")
    except TLSFailed:
        return Outcome(error=TLS_FAILED)
    except httpcore.ConnectError:
        return Outcome(error=COULD_NOT_CONNECT)
    except httpcore.NetworkError:
        return Outcome(error=CONNECTION_LOST)
    except httpcore.ProtocolError:
        return Outcome(error=INVALID_RESPONSE)
    if 200 <= status_code < 300:
        return Outcome(status_code=status_code)
    error = f"HTTP {status_code}"
    if 300 <= status_code < 400:
        error += " (redirects are not followed)"
    return Outcome(status_code=status_code, error=error, retry_after=retry_after)


def _retry_after(headers: list[tuple[bytes, bytes]]) -> int | None:
    """A Retry-After header in seconds (an HTTP date is ignored)."""
    for name, value in headers:
        if name.lower() == b"retry-after":
            value = value.strip()
            if not value.isdigit():
                return None
            # Python refuses to convert over 4300 digits, and over 9 is far beyond any pause.
            value = value.lstrip(b"0") or b"0"
            return int(value) if len(value) <= 9 else 10**9
    return None
