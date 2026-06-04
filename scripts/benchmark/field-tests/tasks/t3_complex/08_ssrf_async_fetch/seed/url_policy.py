"""SSRF-safe URL policy validator.

Rejects unsafe URLs BEFORE they reach any HTTP client. Two layers:

1. Lexical checks — scheme allowlist, raw control characters (CRLF
   injection), userinfo evasion, missing host, localhost name variants,
   decimal/hex encoded IP literals, plain IP literals.
2. DNS resolution — hostnames are resolved via ``socket.getaddrinfo``
   and every returned address is classified with the ``ipaddress``
   module (partial DNS-rebinding defense: a public name whose records
   point into private space is rejected).

The validator never performs an HTTP request itself.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

_ALLOWED_SCHEMES = frozenset({"http", "https"})

# Hostnames that are localhost by convention, before any DNS lookup.
_LOCALHOST_NAMES = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})

# Cloud metadata endpoints (AWS IMDS v4 + v6). Checked before the
# generic link-local/private classification so the reason is specific.
_METADATA_ADDRESSES = frozenset({
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("fd00:ec2::254"),
})

_DECIMAL_HOST_RE = re.compile(r"\d+\Z")
_HEX_HOST_RE = re.compile(r"0[xX][0-9a-fA-F]+\Z")

# Raw control characters checked on the unparsed URL: Python >= 3.10
# urlsplit silently strips \r \n \t (bpo-43882), so parsing first would
# hide the injection attempt.
_CONTROL_CHARS = ("\r", "\n", "\t", "\x00")


class URLPolicyViolation(Exception):
    """Raised when a URL violates the SSRF policy.

    Carries a machine-greppable ``reason`` (taxonomy prefix such as
    ``scheme:``, ``private:``, ``metadata:``, ``localhost:``,
    ``encoded:``, ``dns_private:``, ``dns_error:``, ``no_host:``,
    ``crlf:``, ``userinfo:``) and the offending ``url``.
    """

    def __init__(self, reason: str, url: str):
        self.reason = reason
        self.url = url
        super().__init__(f"{reason} (url={url!r})")


class URLPolicy:
    """Policy gate that validates URLs against the SSRF threat model."""

    def validate(self, url: str) -> None:
        """Raise :class:`URLPolicyViolation` with a clear reason if unsafe.

        Two-step:
        1. Lexical check (scheme, hostname, encoded-IP)
        2. DNS resolution + IP range check
        """
        if not isinstance(url, str) or not url.strip():
            raise URLPolicyViolation("no_host: empty or non-string URL", url)

        for char in _CONTROL_CHARS:
            if char in url:
                raise URLPolicyViolation(
                    "crlf: control character %r in URL (header injection)"
                    % char,
                    url,
                )

        parsed = urlparse(url)

        scheme = (parsed.scheme or "").lower()
        if scheme not in _ALLOWED_SCHEMES:
            raise URLPolicyViolation(
                f"scheme: '{scheme or '<none>'}' not allowed "
                "(only http/https)",
                url,
            )

        if parsed.username is not None or parsed.password is not None:
            raise URLPolicyViolation(
                "userinfo: credentials in URL can disguise the real host",
                url,
            )

        host = parsed.hostname
        if not host:
            raise URLPolicyViolation("no_host: URL has no hostname", url)
        host = host.rstrip(".")
        if not host:
            raise URLPolicyViolation("no_host: URL has no hostname", url)

        if host in _LOCALHOST_NAMES or host.endswith(".localhost"):
            raise URLPolicyViolation(
                f"localhost: hostname '{host}' is a localhost variant", url
            )

        encoded = self._decode_numeric_host(host, url)
        if encoded is not None:
            label = self._classify_ip(encoded)
            if label is not None:
                raise URLPolicyViolation(
                    f"encoded: host '{host}' decodes to {encoded} ({label})",
                    url,
                )
            return  # numeric literal decoding to a public IP — no DNS step

        literal = self._parse_ip_literal(host)
        if literal is not None:
            label = self._classify_ip(literal)
            if label is not None:
                raise URLPolicyViolation(label, url)
            return  # public IP literal — no DNS step

        for resolved in self._resolve(host, url):
            label = self._classify_ip(resolved)
            if label is not None:
                raise URLPolicyViolation(
                    f"dns_private: hostname '{host}' resolves to "
                    f"{resolved} ({label})",
                    url,
                )

    @staticmethod
    def _classify_ip(
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
    ) -> str | None:
        """Return a violation label for a disallowed address, else None."""
        if ip.version == 6:
            mapped = ip.ipv4_mapped
            if mapped is not None:
                ip = mapped
        if ip in _METADATA_ADDRESSES:
            return f"metadata: {ip} is a cloud metadata endpoint"
        if ip.is_unspecified:
            return f"localhost: {ip} is the unspecified address (localhost-equivalent)"
        if ip.is_loopback:
            return f"private: {ip} is a loopback address (localhost)"
        if ip.is_link_local:
            return f"private: {ip} is a link-local address"
        if ip.is_private:
            return f"private: {ip} is in a private address range"
        if ip.is_multicast or ip.is_reserved:
            return f"private: {ip} is a reserved/multicast address"
        return None

    @staticmethod
    def _parse_ip_literal(
        host: str,
    ) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        try:
            return ipaddress.ip_address(host)
        except ValueError:
            return None

    @staticmethod
    def _decode_numeric_host(
        host: str, url: str
    ) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        """Decode all-decimal / 0x-hex hostnames into an IP address.

        ``http://2130706433/`` and ``http://0x7f000001/`` are both
        127.0.0.1. Returns None when the host is not a numeric literal;
        raises (fail closed) when it is numeric but not a valid address.
        """
        if _DECIMAL_HOST_RE.fullmatch(host):
            value = int(host, 10)
        elif _HEX_HOST_RE.fullmatch(host):
            value = int(host, 16)
        else:
            return None
        try:
            return ipaddress.ip_address(value)
        except ValueError as exc:
            raise URLPolicyViolation(
                f"encoded: host '{host}' is a numeric literal but not a "
                "valid IP address",
                url,
            ) from exc

    @staticmethod
    def _resolve(
        host: str, url: str
    ) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        """Resolve via socket.getaddrinfo and return all addresses.

        A hostname that does not resolve is NOT a policy violation: it
        reaches nothing, so it carries no SSRF risk at validation time —
        the HTTP client fails at connect. The rebinding TOCTOU window
        (resolve-then-swap) exists with or without this pass-through and
        is owned by the DNS-pinning layer, not this validator.
        """
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except socket.gaierror:
            return []

        addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        for family, _socktype, _proto, _canonname, sockaddr in infos:
            if family not in (socket.AF_INET, socket.AF_INET6):
                continue
            raw = str(sockaddr[0]).split("%", 1)[0]  # strip IPv6 scope-id
            try:
                addresses.append(ipaddress.ip_address(raw))
            except ValueError as exc:
                raise URLPolicyViolation(
                    f"dns_error: unparseable resolved address {raw!r} "
                    f"for '{host}'",
                    url,
                ) from exc

        return addresses
