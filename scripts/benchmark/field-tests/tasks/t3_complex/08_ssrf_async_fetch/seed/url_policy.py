"""SSRF-safe URL policy validator."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class URLPolicyViolation(Exception):
    """Raised when a URL fails SSRF policy checks."""

    def __init__(self, reason: str, url: str) -> None:
        self.reason = reason
        self.url = url
        super().__init__(f"{reason}: {url}")


_ALLOWED_SCHEMES = frozenset({"http", "https"})
_LOCALHOST_NAMES = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})

_CLOUD_METADATA_V4 = ipaddress.ip_address("169.254.169.254")
_CLOUD_METADATA_V6_NET = ipaddress.ip_network("fd00:ec2::/32")

# Explicit RFC-defined private/reserved ranges (version-independent)
_PRIVATE_NETS_V4 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
)

_PRIVATE_NETS_V6 = (
    ipaddress.ip_network("fc00::/7"),    # ULA (includes fd00::/8)
    ipaddress.ip_network("::1/128"),     # loopback
    ipaddress.ip_network("fe80::/10"),   # link-local
    ipaddress.ip_network("::/128"),      # unspecified
    ipaddress.ip_network("fd00:ec2::/32"),  # AWS IMDS v6
)


class URLPolicy:
    """Validates URLs for SSRF safety before any HTTP request is made.

    Two-step validation:
    1. Lexical checks (scheme, host presence, CRLF, encoded IPs)
    2. DNS resolution + IP range classification
    """

    def validate(self, url: str) -> None:
        """Raises URLPolicyViolation with a clear reason if the URL is unsafe."""
        # CRLF check before any parsing to catch header-injection attempts
        if "\r" in url or "\n" in url:
            raise URLPolicyViolation("crlf_injection", url)

        parsed = urlparse(url)

        scheme = parsed.scheme.lower()
        if scheme not in _ALLOWED_SCHEMES:
            raise URLPolicyViolation(f"scheme_not_allowed:{scheme}", url)

        # Userinfo in netloc is an evasion vector (http://trusted@evil.host/)
        if parsed.username is not None:
            raise URLPolicyViolation("userinfo_evasion", url)

        hostname = parsed.hostname
        if not hostname:
            raise URLPolicyViolation("no_host", url)

        if hostname.lower() in _LOCALHOST_NAMES:
            raise URLPolicyViolation("localhost", url)

        ip_addr = self._decode_ip(hostname)
        if ip_addr is not None:
            self._reject_if_private(ip_addr, url)
        else:
            self._check_dns(hostname, url)

    def _decode_ip(
        self, hostname: str
    ) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        """Decode hostname as IP, handling standard, decimal, and hex encoding."""
        try:
            return ipaddress.ip_address(hostname)
        except ValueError:
            pass
        # Decimal-encoded (2130706433) and hex-encoded (0x7f000001) IPv4
        try:
            num = int(hostname, 0)
            if 0 <= num <= 0xFFFFFFFF:
                return ipaddress.ip_address(num)
        except (ValueError, OverflowError):
            pass
        return None

    def _reject_if_private(
        self,
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
        url: str,
        *,
        from_dns: bool = False,
    ) -> None:
        """Raise URLPolicyViolation if ip falls in any blocked range."""

        def _raise(reason: str) -> None:
            raise URLPolicyViolation(
                "dns_resolves_to_private" if from_dns else reason, url
            )

        if isinstance(ip, ipaddress.IPv4Address):
            if ip == _CLOUD_METADATA_V4:
                _raise("cloud_metadata")
            for net in _PRIVATE_NETS_V4:
                if ip in net:
                    _raise("private_ip")
        else:
            if ip in _CLOUD_METADATA_V6_NET:
                _raise("cloud_metadata")
            for net in _PRIVATE_NETS_V6:
                if ip in net:
                    _raise("private_ip")

    def _check_dns(self, hostname: str, url: str) -> None:
        """Resolve hostname and reject if any resolved IP is in a private range."""
        try:
            results = socket.getaddrinfo(hostname, None)
        except socket.gaierror:
            # Fail-open: unresolvable hostname cannot be pre-screened; the
            # HTTP client's connection attempt will fail independently.
            return

        for _family, _socktype, _proto, _canonname, sockaddr in results:
            addr_str = sockaddr[0]
            try:
                ip = ipaddress.ip_address(addr_str)
            except ValueError:
                continue
            self._reject_if_private(ip, url, from_dns=True)
