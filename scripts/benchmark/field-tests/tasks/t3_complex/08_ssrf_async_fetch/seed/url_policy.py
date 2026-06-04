"""SSRF-safe URL validator. Rejects unsafe URLs before they hit any HTTP client."""
from __future__ import annotations

import ipaddress
import socket
import urllib.parse
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from typing import Optional, Tuple, Union

_ALLOWED_SCHEMES = frozenset({"http", "https"})

# Hostnames that are unambiguously local regardless of DNS
_LOCALHOST_NAMES = frozenset({"localhost", "ip6-localhost", "0.0.0.0", "::"})

# Explicit cloud-metadata hosts (subset of link-local, called out separately for clarity)
_METADATA_HOSTS = frozenset({"169.254.169.254", "fd00:ec2::254"})

# All private/reserved ranges that must never be contacted
_PRIVATE_NETWORKS = [
    ip_network("127.0.0.0/8"),       # loopback
    ip_network("10.0.0.0/8"),        # RFC1918
    ip_network("172.16.0.0/12"),     # RFC1918
    ip_network("192.168.0.0/16"),    # RFC1918
    ip_network("169.254.0.0/16"),    # link-local / APIPA / IMDS
    ip_network("100.64.0.0/10"),     # CGNAT
    ip_network("0.0.0.0/8"),         # "this" network
    ip_network("::1/128"),           # IPv6 loopback
    ip_network("fc00::/7"),          # IPv6 ULA (includes fd00::/8)
    ip_network("::/128"),            # IPv6 unspecified
]


class URLPolicyViolation(Exception):
    """Raised when a URL fails SSRF security policy validation."""

    def __init__(self, reason: str, url: str) -> None:
        super().__init__(f"{reason}: {url!r}")
        self.reason = reason
        self.url = url


def _is_private_ip(addr: Union[IPv4Address, IPv6Address]) -> bool:
    return (
        addr.is_loopback
        or addr.is_link_local
        or addr.is_unspecified
        or any(addr in net for net in _PRIVATE_NETWORKS)
    )


def _decode_to_ip(
    hostname: str,
) -> Tuple[Optional[Union[IPv4Address, IPv6Address]], Optional[str]]:
    """Decode hostname to an IP address, including decimal and hex variants.

    Returns (ip_addr, encoding) where encoding is None for standard notation,
    'decimal' for integer-encoded, 'hex' for 0x-encoded. Returns (None, None)
    when the hostname is not an IP literal.
    """
    clean = hostname.strip("[]")  # strip IPv6 brackets

    # Standard IP notation (IPv4 dotted or IPv6 colon)
    try:
        return ip_address(clean), None
    except ValueError:
        pass

    # Hex-encoded: 0x7f000001
    if clean.lower().startswith("0x"):
        try:
            return ip_address(int(clean, 16)), "hex"
        except ValueError:
            pass

    # Decimal-encoded: 2130706433 (== 127.0.0.1)
    if clean.isdigit():
        try:
            return ip_address(int(clean)), "decimal"
        except ValueError:
            pass

    return None, None


class URLPolicy:
    def validate(self, url: str) -> None:
        """Raises URLPolicyViolation with a clear reason if URL is unsafe.

        Step 1 — Lexical: scheme, CRLF, host presence, userinfo, metadata,
                          localhost names, encoded IP literals.
        Step 2 — DNS:     resolve hostname and check every returned address.
        """
        # CRLF injection check must happen before urlparse (raw string)
        if "\r" in url or "\n" in url:
            raise URLPolicyViolation("crlf_injection", url)

        parsed = urllib.parse.urlparse(url)

        # Scheme whitelist: only http and https
        scheme = (parsed.scheme or "").lower()
        if scheme not in _ALLOWED_SCHEMES:
            raise URLPolicyViolation(f"disallowed_scheme:{scheme}", url)

        # Host must be present and non-empty
        hostname = parsed.hostname or ""
        if not hostname:
            raise URLPolicyViolation("no_host", url)

        # Userinfo (@-credentials) is a known evasion vector; reject outright
        if parsed.username is not None:
            raise URLPolicyViolation("userinfo_evasion", url)

        hostname_lower = hostname.lower()

        # Cloud metadata endpoints (named explicitly for clear audit trail)
        if hostname_lower in _METADATA_HOSTS:
            raise URLPolicyViolation(f"cloud_metadata:{hostname}", url)

        # Known localhost name variants
        if hostname_lower in _LOCALHOST_NAMES:
            raise URLPolicyViolation(f"localhost_variant:{hostname}", url)

        # Decode IP literals (handles decimal / hex encoding)
        addr, encoding = _decode_to_ip(hostname)
        if addr is not None and _is_private_ip(addr):
            if encoding:
                raise URLPolicyViolation(
                    f"encoded_private_ip:{encoding}:{addr}", url
                )
            raise URLPolicyViolation(f"private_ip:{addr}", url)

        # DNS resolution + private-IP check
        # Fail-open on NXDOMAIN/timeout: cannot verify but cannot confirm private.
        try:
            results = socket.getaddrinfo(hostname, None)
        except socket.gaierror:
            results = []

        for result in results:
            addr_str = result[4][0]
            try:
                resolved = ip_address(addr_str)
                if _is_private_ip(resolved):
                    raise URLPolicyViolation(f"dns_private_ip:{resolved}", url)
            except ValueError:
                pass
