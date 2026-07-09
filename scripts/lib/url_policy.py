"""url_policy.py — SSRF-safe URL validator.

Rejects URLs pointing to private networks, cloud metadata endpoints,
non-HTTP schemes, encoded IP forms, or otherwise unsafe targets BEFORE
any HTTP client touches them.

Two-step validation:
  1. Lexical — scheme allow-list, hostname presence, CR/LF/NUL rejection,
     encoded-IPv4 normalization (decimal, hex), localhost name list.
  2. DNS resolution + IP-range check — every address returned by
     ``socket.getaddrinfo`` is range-checked. Catches a DNS record that
     points to RFC1918 / loopback / link-local / metadata space.

``URLPolicy`` itself is policy-only: it never opens an HTTP connection.
Callers that need to fetch the validated URL should use
:meth:`URLPolicy.validate_and_pin` + :func:`open_pinned_connection` (below)
to avoid re-resolving the hostname after it has been vetted.

Threats covered (SSRF taxonomy):
- Private IPv4 (RFC1918), IPv4 loopback, link-local, unspecified, multicast
- IPv6 ULA (fc00::/7), IPv6 loopback (::1), link-local, unspecified
- Cloud metadata: 169.254.169.254 (AWS/GCP/Azure IPv4), fd00:ec2::254 (AWS IPv6)
- Localhost aliases: localhost, ip6-localhost, ip6-loopback
- Encoded IPv4: decimal integer (2130706433) and hex (0x7f000001)
- Non-HTTP(S) schemes: file, gopher, ftp, javascript, data, ...
- Userinfo-evasion: http://example.com@127.0.0.1/ (urlparse extracts the
  trailing host correctly; we then range-check the IP)
- CRLF / NUL injection in the raw URL string
- Missing-host URLs: http:///path

Fixed in OI-222 (1.0.1 SSRF-hardening):
- F1: the final range check is ``not ip.is_global`` (was ``ip.is_private``,
  which let CGNAT (100.64.0.0/10) and other non-global-but-not-private
  ranges through).
- F2: DNS-rebinding TOCTOU. ``validate_and_pin`` resolves + range-checks a
  URL and returns a :class:`PinnedTarget` carrying the vetted IP; callers
  connect to that IP directly (see :func:`open_pinned_connection`) instead
  of letting the HTTP client re-resolve the hostname, which could by then
  point somewhere unsafe.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
from dataclasses import dataclass
from typing import Iterable, Union
from urllib.parse import urlparse

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]

_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}

ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

LOCALHOST_HOSTNAMES: frozenset[str] = frozenset(
    {
        "localhost",
        "ip6-localhost",
        "ip6-loopback",
    }
)

# Cloud metadata IPs. Both are already covered by link-local / IPv6 ULA
# checks; we name them so the violation reason is unambiguous.
METADATA_IPS: frozenset[str] = frozenset(
    {
        "169.254.169.254",
        "fd00:ec2::254",
    }
)


class URLPolicyViolation(Exception):
    """Raised by :class:`URLPolicy` when a URL fails the SSRF policy.

    The ``reason`` attribute is a short, machine-friendly token identifying
    why the URL was rejected (e.g. ``non_global_ip:10.0.0.5``).
    """

    def __init__(self, reason: str, url: str) -> None:
        super().__init__(f"{reason}: {url}")
        self.reason = reason
        self.url = url


@dataclass(frozen=True)
class PinnedTarget:
    """Result of :meth:`URLPolicy.validate_and_pin`.

    ``pinned_ip`` is the vetted address the caller MUST connect to (see
    :func:`open_pinned_connection`). ``hostname`` is the original,
    unresolved host — kept for the Host header and TLS SNI so the fetch
    still looks like a normal request to ``hostname``, it just skips the
    second DNS lookup that a rebind attack would exploit.
    """

    url: str
    hostname: str
    pinned_ip: str
    port: int
    scheme: str


class URLPolicy:
    """SSRF-safe URL policy.

    Use :meth:`validate` to assert that a URL is safe to fetch. The method
    raises :class:`URLPolicyViolation` on the first failure; otherwise it
    returns ``None``.

    The validator never opens an HTTP connection. It does perform DNS
    resolution via :func:`socket.getaddrinfo` so that hostnames whose
    A/AAAA records point to private space are rejected.
    """

    def __init__(
        self,
        allowed_schemes: Iterable[str] = ALLOWED_SCHEMES,
    ) -> None:
        self._allowed_schemes = frozenset(
            scheme.lower() for scheme in allowed_schemes
        )

    def validate(self, url: str) -> None:
        """Validate ``url``; raise :class:`URLPolicyViolation` if unsafe."""
        self._validate_impl(url)

    def validate_and_pin(self, url: str) -> PinnedTarget:
        """Validate ``url`` and pin it to the vetted IP.

        Same checks as :meth:`validate`, but returns a :class:`PinnedTarget`
        instead of ``None``. Use this instead of ``validate`` whenever the
        caller is about to open a connection: fetching by ``pinned_ip``
        (see :func:`open_pinned_connection`) closes the DNS-rebinding TOCTOU
        window between this check and the actual HTTP request.
        """
        hostname, ip, port, scheme = self._validate_impl(url)
        return PinnedTarget(
            url=url,
            hostname=hostname,
            pinned_ip=str(ip),
            port=port,
            scheme=scheme,
        )

    def _validate_impl(self, url: str) -> tuple[str, IPAddress, int, str]:
        """Shared validation core. Returns ``(hostname, ip, port, scheme)``.

        For a hostname that resolves to multiple addresses, every address is
        range-checked (any violation rejects the whole URL) and the first
        resolved address is returned as the pin candidate, matching the
        order ``getaddrinfo`` itself would try.
        """
        if not isinstance(url, str) or not url:
            raise URLPolicyViolation("empty_url", str(url))

        if any(ch in url for ch in ("\r", "\n", "\x00")):
            raise URLPolicyViolation("control_character_in_url", url)

        parsed = urlparse(url)

        scheme = (parsed.scheme or "").lower()
        if scheme not in self._allowed_schemes:
            raise URLPolicyViolation(
                f"disallowed_scheme:{scheme or 'missing'}", url
            )

        try:
            hostname = parsed.hostname
        except ValueError as exc:
            raise URLPolicyViolation(
                f"malformed_host:{exc!s}", url
            ) from exc

        if not hostname:
            raise URLPolicyViolation("missing_host", url)

        hostname = hostname.lower()
        port = parsed.port or _DEFAULT_PORTS.get(scheme, 0)

        if hostname in LOCALHOST_HOSTNAMES:
            raise URLPolicyViolation(
                f"localhost_hostname:{hostname}", url
            )

        normalized = self._normalize_encoded_ipv4(hostname)
        if normalized is not None:
            ip = ipaddress.ip_address(normalized)
            self._check_ip(ip, url)
            return hostname, ip, port, scheme

        try:
            literal_ip = ipaddress.ip_address(hostname)
        except ValueError:
            literal_ip = None

        if literal_ip is not None:
            self._check_ip(literal_ip, url)
            return hostname, literal_ip, port, scheme

        infos = socket.getaddrinfo(hostname, None)

        if not infos:
            raise URLPolicyViolation("dns_resolution_empty", url)

        resolved_ips: list[IPAddress] = []
        for info in infos:
            addr = info[4][0]
            try:
                resolved_ip = ipaddress.ip_address(addr)
            except ValueError as exc:
                raise URLPolicyViolation(
                    f"dns_returned_invalid_ip:{addr}", url
                ) from exc
            self._check_ip(resolved_ip, url)
            resolved_ips.append(resolved_ip)

        return hostname, resolved_ips[0], port, scheme

    def _check_ip(self, ip: IPAddress, url: str) -> None:
        # Order matters: more specific labels fire before the catch-all
        # ``not ip.is_global`` because Python classifies 0.0.0.0, link-local,
        # and several reserved blocks as non-global as well.
        ip_str = str(ip)
        if ip_str in METADATA_IPS:
            raise URLPolicyViolation(f"cloud_metadata_ip:{ip_str}", url)
        if ip.is_unspecified:
            raise URLPolicyViolation(f"unspecified_ip:{ip_str}", url)
        if ip.is_loopback:
            raise URLPolicyViolation(f"loopback_ip:{ip_str}", url)
        if ip.is_link_local:
            raise URLPolicyViolation(f"link_local_ip:{ip_str}", url)
        if ip.is_multicast:
            raise URLPolicyViolation(f"multicast_ip:{ip_str}", url)
        if ip.is_reserved:
            raise URLPolicyViolation(f"reserved_ip:{ip_str}", url)
        # Catch-all: rejects RFC1918 private space, CGNAT (100.64.0.0/10),
        # and any other non-routable range not already named above.
        if not ip.is_global:
            raise URLPolicyViolation(f"non_global_ip:{ip_str}", url)

    @staticmethod
    def _normalize_encoded_ipv4(hostname: str) -> str | None:
        """Decode hex/decimal integer hostnames to dotted-quad IPv4.

        Returns ``None`` when ``hostname`` is not an encoded IPv4 integer.
        """
        host = hostname.strip()
        if not host:
            return None

        if host.startswith(("0x", "0X")):
            try:
                value = int(host, 16)
            except ValueError:
                return None
            return URLPolicy._int_to_ipv4(value)

        if host.isdigit():
            try:
                value = int(host, 10)
            except ValueError:
                return None
            return URLPolicy._int_to_ipv4(value)

        return None

    @staticmethod
    def _int_to_ipv4(value: int) -> str | None:
        if value < 0 or value > 0xFFFFFFFF:
            return None
        return str(ipaddress.IPv4Address(value))


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """``HTTPConnection`` that connects to a fixed IP instead of resolving
    ``host`` again. ``host`` is still used for the ``Host`` header."""

    def __init__(self, pinned_ip: str, hostname: str, port: int, **kwargs) -> None:
        super().__init__(hostname, port, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = self._create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address
        )
        if self._tunnel_host:
            self._tunnel()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """``HTTPSConnection`` that connects to a fixed IP instead of resolving
    ``host`` again, then TLS-wraps using ``host`` for SNI and certificate
    verification so the cert check still matches the original hostname."""

    def __init__(self, pinned_ip: str, hostname: str, port: int, **kwargs) -> None:
        super().__init__(hostname, port, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        sock = self._create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address
        )
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
            sock = self.sock
        server_hostname = self._tunnel_host if self._tunnel_host else self.host
        self.sock = self._context.wrap_socket(sock, server_hostname=server_hostname)


def open_pinned_connection(
    target: PinnedTarget, timeout: float = 10.0
) -> http.client.HTTPConnection:
    """Open an HTTP(S) connection to ``target.pinned_ip``.

    The ``Host`` header (and, for HTTPS, TLS SNI + certificate verification)
    still uses ``target.hostname`` — the request looks identical to a normal
    fetch of that hostname, it just skips the second DNS lookup a rebind
    attack relies on. Callers must obtain ``target`` from
    :meth:`URLPolicy.validate_and_pin`, not construct one by hand.

    Usage::

        target = URLPolicy().validate_and_pin(url)
        conn = open_pinned_connection(target)
        conn.request("GET", urlparse(target.url).path or "/")
        resp = conn.getresponse()
    """
    if target.scheme == "https":
        return _PinnedHTTPSConnection(
            target.pinned_ip, target.hostname, target.port, timeout=timeout
        )
    return _PinnedHTTPConnection(
        target.pinned_ip, target.hostname, target.port, timeout=timeout
    )
