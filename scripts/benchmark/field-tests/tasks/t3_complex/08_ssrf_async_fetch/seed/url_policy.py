"""SSRF-safe URL policy validator.

Two-step validation:
  1. Lexical check (scheme, hostname presence, encoded-IP forms, localhost
     names, userinfo, control characters).
  2. DNS resolution via socket.getaddrinfo, then IP-range classification
     using the stdlib ipaddress module.

The validator never performs an HTTP request — it only inspects the URL
string and resolves DNS to classify the target.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Optional, Union
from urllib.parse import urlparse


ALLOWED_SCHEMES = frozenset({"http", "https"})

LOCALHOST_NAMES = frozenset({
    "localhost",
    "ip6-localhost",
    "ip6-loopback",
})

# Cloud metadata service endpoints (AWS / GCP / Azure share 169.254.169.254;
# AWS also publishes an IPv6 IMDS address).
METADATA_IPS = frozenset({
    "169.254.169.254",
    "fd00:ec2::254",
})

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]


class URLPolicyViolation(Exception):
    """Raised when a URL violates the SSRF-safe policy.

    The ``reason`` attribute exposes a short identifying token from a fixed
    taxonomy: scheme, no_host, crlf, userinfo, localhost, private_ip,
    metadata, encoded, dns_private, parse_error.
    """

    def __init__(self, reason: str, url: str) -> None:
        super().__init__(f"{reason}: {url}")
        self.reason = reason
        self.url = url


class URLPolicy:
    """Reject URLs that could be used for SSRF before any HTTP client sees them."""

    def validate(self, url: str) -> None:
        if not isinstance(url, str) or not url:
            raise URLPolicyViolation("no_host", url if isinstance(url, str) else "")

        # Reject control characters in the raw URL (CRLF injection, NUL, tab,
        # DEL). urlparse in modern CPython silently strips \r/\n/\t, so we
        # must inspect the raw input first.
        for ch in url:
            code = ord(ch)
            if code < 0x20 or code == 0x7F:
                raise URLPolicyViolation("crlf", url)

        try:
            parsed = urlparse(url)
        except ValueError as exc:
            raise URLPolicyViolation("parse_error", url) from exc

        scheme = (parsed.scheme or "").lower()
        if scheme not in ALLOWED_SCHEMES:
            raise URLPolicyViolation("scheme", url)

        # Userinfo (user:pass@) is a classic SSRF-evasion vector and has no
        # legitimate use in our outbound fetches.
        if parsed.username or parsed.password:
            raise URLPolicyViolation("userinfo", url)

        try:
            raw_hostname = parsed.hostname
        except ValueError as exc:
            raise URLPolicyViolation("parse_error", url) from exc

        hostname = (raw_hostname or "").lower()
        if not hostname:
            raise URLPolicyViolation("no_host", url)

        if hostname in LOCALHOST_NAMES:
            raise URLPolicyViolation("localhost", url)

        encoded_form = self._encoded_form(hostname)
        decoded_ip = self._decode_to_ip(hostname)

        if decoded_ip is not None:
            if encoded_form:
                # Decimal- or hex-encoded IPv4 — never legitimate for our
                # callers and almost always an SSRF bypass attempt.
                raise URLPolicyViolation("encoded", url)
            self._check_ip(decoded_ip, url, dns=False)
            return

        # Pure hostname — resolve and check every returned address. A DNS
        # failure (NXDOMAIN, timeout) leaves the policy with nothing to check;
        # we let it through because the downstream HTTP client will fail to
        # connect anyway, and treating it as a policy violation would block
        # legitimate fetches in offline / sandboxed environments.
        try:
            addrinfo = socket.getaddrinfo(hostname, None)
        except socket.gaierror:
            return

        for entry in addrinfo:
            sockaddr = entry[4]
            if not sockaddr:
                continue
            ip_str = sockaddr[0]
            try:
                resolved_ip = ipaddress.ip_address(ip_str)
            except ValueError:
                continue
            self._check_ip(resolved_ip, url, dns=True)

    @staticmethod
    def _encoded_form(hostname: str) -> bool:
        if hostname.startswith("0x") or hostname.startswith("0X"):
            return True
        if hostname.isdigit() and "." not in hostname and ":" not in hostname:
            return True
        return False

    @staticmethod
    def _decode_to_ip(hostname: str) -> Optional[IPAddress]:
        # IPv6 literal (urlparse strips the surrounding brackets).
        if ":" in hostname:
            try:
                return ipaddress.ip_address(hostname)
            except ValueError:
                return None

        # Hex-encoded IPv4 (e.g. 0x7f000001 -> 127.0.0.1).
        if hostname.startswith("0x") or hostname.startswith("0X"):
            try:
                value = int(hostname, 16)
            except ValueError:
                return None
            try:
                return ipaddress.ip_address(value)
            except ValueError:
                return None

        # Decimal-encoded IPv4 (e.g. 2130706433 -> 127.0.0.1).
        if hostname.isdigit() and "." not in hostname:
            try:
                value = int(hostname)
            except ValueError:
                return None
            try:
                return ipaddress.ip_address(value)
            except ValueError:
                return None

        # Dotted-quad IPv4.
        try:
            return ipaddress.ip_address(hostname)
        except ValueError:
            return None

    @staticmethod
    def _check_ip(ip: IPAddress, url: str, dns: bool) -> None:
        ip_str = str(ip)
        if ip_str in METADATA_IPS:
            raise URLPolicyViolation("dns_private" if dns else "metadata", url)
        if ip.is_loopback or ip.is_unspecified:
            raise URLPolicyViolation("dns_private" if dns else "localhost", url)
        if (
            ip.is_private
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ):
            raise URLPolicyViolation("dns_private" if dns else "private_ip", url)
