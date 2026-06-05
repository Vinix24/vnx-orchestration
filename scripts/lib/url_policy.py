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

The validator is policy-only: it never opens an HTTP connection.

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
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Iterable, Union
from urllib.parse import urlparse

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]

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
    why the URL was rejected (e.g. ``private_ip:10.0.0.5``).
    """

    def __init__(self, reason: str, url: str) -> None:
        super().__init__(f"{reason}: {url}")
        self.reason = reason
        self.url = url


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

        if hostname in LOCALHOST_HOSTNAMES:
            raise URLPolicyViolation(
                f"localhost_hostname:{hostname}", url
            )

        normalized = self._normalize_encoded_ipv4(hostname)
        if normalized is not None:
            self._check_ip(ipaddress.ip_address(normalized), url)
            return

        try:
            literal_ip = ipaddress.ip_address(hostname)
        except ValueError:
            literal_ip = None

        if literal_ip is not None:
            self._check_ip(literal_ip, url)
            return

        infos = socket.getaddrinfo(hostname, None)

        if not infos:
            raise URLPolicyViolation("dns_resolution_empty", url)

        for info in infos:
            addr = info[4][0]
            try:
                resolved_ip = ipaddress.ip_address(addr)
            except ValueError as exc:
                raise URLPolicyViolation(
                    f"dns_returned_invalid_ip:{addr}", url
                ) from exc
            self._check_ip(resolved_ip, url)

    def _check_ip(self, ip: IPAddress, url: str) -> None:
        # Order matters: more specific labels fire before the catch-all
        # ``is_private`` because Python classifies 0.0.0.0, link-local, and
        # several reserved blocks as private as well.
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
        if ip.is_private:
            raise URLPolicyViolation(f"private_ip:{ip_str}", url)

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
