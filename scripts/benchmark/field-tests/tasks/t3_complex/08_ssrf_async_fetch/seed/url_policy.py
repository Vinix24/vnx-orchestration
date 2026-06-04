"""SSRF-safe URL validator with two-step policy enforcement.

Step 1 — Lexical check: scheme, hostname, encoded-IP detection, CRLF/userinfo attacks.
Step 2 — DNS resolution: resolve hostname via socket.getaddrinfo and verify resolved
IPs are not in private/blocked ranges.

Does NOT make any HTTP request itself. Purely stdlib policy + DNS resolution.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Private / blocked ranges
# ---------------------------------------------------------------------------

_PRIVATE_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("169.254.0.0/16"),
    ipaddress.IPv4Network("0.0.0.0/8"),
    ipaddress.IPv6Network("fc00::/7"),
    ipaddress.IPv6Network("::1/128"),
]

_CLOUD_METADATA: frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address] = frozenset({
    ipaddress.IPv4Address("169.254.169.254"),
    ipaddress.IPv6Address("fd00:ec2::254"),
})

_BLOCKED_HOSTNAMES: frozenset[str] = frozenset({
    "localhost",
    "ip6-localhost",
    "0.0.0.0",
})

_BLOCKED_SCHEMES: frozenset[str] = frozenset({
    "file", "gopher", "ftp", "javascript", "data",
})


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class URLPolicyViolation(Exception):
    """Raised when a URL fails the security policy check.

    Attributes:
        reason: Human-readable explanation of the violation.
        url: The URL that was rejected.
    """

    def __init__(self, reason: str, url: str) -> None:
        self.reason = reason
        self.url = url
        super().__init__(f"URL policy violation: {reason} (url: {url})")


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

class URLPolicy:
    """SSRF-safe URL validator.

    Two-step validation:
    1. Lexical check — scheme, hostname, encoded-IP detection, CRLF/userinfo attacks.
    2. DNS resolution — resolve hostname via ``socket.getaddrinfo`` and verify every
       resolved address falls outside private/blocked ranges.

    Usage::

        policy = URLPolicy()
        policy.validate("https://example.com/path")       # No exception
        policy.validate("http://127.0.0.1/admin")         # Raises URLPolicyViolation
    """

    def validate(self, url: str) -> None:
        """Validate *url* against the SSRF policy.

        Raises :class:`URLPolicyViolation` with a descriptive ``reason`` if the
        URL is unsafe.  Returns ``None`` when the URL passes all checks.
        """
        # Step 0: CRLF injection anywhere in the raw URL string
        if "\r" in url or "\n" in url:
            raise URLPolicyViolation("CRLF injection detected in URL", url)

        # Step 1: Parse
        parsed = urlparse(url)

        # Scheme check
        scheme = parsed.scheme.lower()
        if not scheme:
            raise URLPolicyViolation("URL has no scheme", url)
        if scheme not in ("http", "https"):
            if scheme in _BLOCKED_SCHEMES:
                raise URLPolicyViolation(f"blocked scheme: {scheme}", url)
            raise URLPolicyViolation(f"non-HTTP scheme: {scheme}", url)

        # Userinfo evasion — reject any URL carrying credentials in the authority
        if parsed.username is not None or "@" in parsed.netloc:
            raise URLPolicyViolation("userinfo in URL (potential evasion)", url)

        # Hostname (urllib strips brackets, port, and userinfo)
        hostname = parsed.hostname
        if not hostname:
            raise URLPolicyViolation("URL has no host", url)

        # Step 2: Lexical hostname check
        self._check_hostname(hostname, url)

        # Step 3: DNS resolution check
        self._check_dns(hostname, url)

    # -- lexical checks -----------------------------------------------------

    def _check_hostname(self, hostname: str, url: str) -> None:
        """Run all lexical hostname checks."""
        hostname_lower = hostname.lower()

        # Named blocked hostnames
        if hostname_lower in _BLOCKED_HOSTNAMES:
            raise URLPolicyViolation(f"blocked hostname: {hostname_lower}", url)

        # Direct IP literal
        ip = self._parse_ip(hostname)
        if ip is not None:
            self._check_ip(ip, url)
            return

        # Decimal / hex / octal encoded IP
        encoded_ip = self._decode_numeric_hostname(hostname)
        if encoded_ip is not None:
            self._check_ip(encoded_ip, url)

    # -- IP helpers ---------------------------------------------------------

    @staticmethod
    def _parse_ip(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        """Try to interpret *hostname* as an IPv4 or IPv6 address literal."""
        try:
            return ipaddress.ip_address(hostname)
        except ValueError:
            return None

    @staticmethod
    def _decode_numeric_hostname(hostname: str) -> ipaddress.IPv4Address | None:
        """Try to decode a decimal / hex / octal integer hostname into an IPv4 address.

        Examples:
        * ``2130706433`` → 127.0.0.1 (decimal)
        * ``0x7f000001`` → 127.0.0.1 (hex)
        * ``017700000001`` → 127.0.0.1 (octal)
        """
        try:
            low = hostname.lower()
            if low.startswith("0x") and all(c in "0123456789abcdef" for c in low[2:]):
                value = int(hostname, 16)
            elif (
                hostname.startswith("0")
                and len(hostname) > 1
                and all(c in "01234567" for c in hostname)
            ):
                value = int(hostname, 8)
            elif hostname.isdigit():
                value = int(hostname)
            else:
                return None

            return ipaddress.IPv4Address(value)
        except (ValueError, ipaddress.AddressValueError):
            return None

    # -- IP range check -----------------------------------------------------

    @staticmethod
    def _check_ip(
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address, url: str
    ) -> None:
        """Raise :class:`URLPolicyViolation` if *ip* falls in a blocked range."""
        if ip in _CLOUD_METADATA:
            raise URLPolicyViolation(f"cloud metadata endpoint: {ip}", url)

        if ip.is_loopback:
            raise URLPolicyViolation(f"loopback address: {ip}", url)
        if ip.is_unspecified:
            raise URLPolicyViolation(f"unspecified address: {ip}", url)
        if ip.is_link_local:
            raise URLPolicyViolation(f"link-local address: {ip}", url)

        for net in _PRIVATE_NETWORKS:
            if ip in net:
                raise URLPolicyViolation(f"private IP range: {ip}", url)

    # -- DNS resolution -----------------------------------------------------

    def _check_dns(self, hostname: str, url: str) -> None:
        """Resolve *hostname* via ``socket.getaddrinfo`` and verify every
        resolved address is public."""
        try:
            addrinfo = socket.getaddrinfo(
                hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM
            )
        except socket.gaierror:
            return

        for _family, _type, _proto, _cname, sockaddr in addrinfo:
            ip_str = sockaddr[0]
            ip = self._parse_ip(ip_str)
            if ip is None:
                continue

            if ip in _CLOUD_METADATA:
                raise URLPolicyViolation(
                    f"DNS resolves to metadata endpoint: {hostname} -> {ip}", url
                )

            if ip.is_loopback:
                raise URLPolicyViolation(
                    f"DNS resolves to loopback: {hostname} -> {ip}", url
                )
            if ip.is_unspecified:
                raise URLPolicyViolation(
                    f"DNS resolves to unspecified: {hostname} -> {ip}", url
                )
            if ip.is_link_local:
                raise URLPolicyViolation(
                    f"DNS resolves to link-local: {hostname} -> {ip}", url
                )

            for net in _PRIVATE_NETWORKS:
                if ip in net:
                    raise URLPolicyViolation(
                        f"DNS resolves to private IP: {hostname} -> {ip}", url
                    )
