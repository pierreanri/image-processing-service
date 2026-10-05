"""Which webhook URLs and addresses may be used.

Users choose where workers send requests, so a webhook could otherwise point workers at the
service's own network: its database, a cloud's instance metadata service, an admin interface.
URLs are checked when they are registered and before every attempt, without resolving them (a
lookup then would prove nothing: the name can resolve elsewhere by the time of the request). The
addresses a name resolves to are checked when connecting.
"""

import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

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
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


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
