"""Adversarial test suite for the SSRF URLPolicy validator.

10 adversarial threat classes (each must raise URLPolicyViolation with
an identifying token in ``reason``) + 3 positive public-URL tests.
DNS is monkeypatched on ``socket.getaddrinfo`` so the suite is
deterministic and offline-safe; the validator itself still calls
``socket.getaddrinfo`` (asserted explicitly in the rebinding test).
"""
from __future__ import annotations

import socket

import pytest

from url_policy import URLPolicy, URLPolicyViolation

PUBLIC_IP = "93.184.216.34"  # example.com


def _addrinfo(ip: str) -> list:
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 0))]


@pytest.fixture
def policy() -> URLPolicy:
    return URLPolicy()


@pytest.fixture
def public_dns(monkeypatch):
    """Make every hostname resolve to a public IP (deterministic tests)."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return _addrinfo(PUBLIC_IP)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


def _assert_violation(policy: URLPolicy, url: str, token: str) -> None:
    with pytest.raises(URLPolicyViolation) as excinfo:
        policy.validate(url)
    assert token in excinfo.value.reason, (
        f"expected token {token!r} in reason {excinfo.value.reason!r}"
    )
    assert excinfo.value.url == url


# --- 1. Private IP ranges (RFC1918, link-local, IPv6 ULA, loopback) ---
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.0.0.1/admin",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://169.254.10.10/",
        "http://[fc00::1]/",
        "http://[::1]/",
    ],
)
def test_rejects_private_ip_ranges(policy, url):
    _assert_violation(policy, url, "private")


# --- 2. Cloud metadata endpoints ---
@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://[fd00:ec2::254]/latest/meta-data/",
    ],
)
def test_rejects_cloud_metadata_endpoints(policy, url):
    _assert_violation(policy, url, "metadata")


# --- 3. Non-HTTP schemes ---
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://evil.example.com:70/_payload",
        "ftp://evil.example.com/exfil",
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
    ],
)
def test_rejects_non_http_schemes(policy, url):
    _assert_violation(policy, url, "scheme")


# --- 4. Localhost variants ---
@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://ip6-localhost/",
        "http://0.0.0.0/",
        "http://[::]/",
    ],
)
def test_rejects_localhost_variants(policy, url):
    _assert_violation(policy, url, "localhost")


# --- 5. Decimal-encoded IP ---
def test_rejects_decimal_encoded_ip(policy):
    with pytest.raises(URLPolicyViolation) as excinfo:
        policy.validate("http://2130706433/")
    assert "encoded" in excinfo.value.reason
    assert "127.0.0.1" in excinfo.value.reason  # must decode, not regex-match


# --- 6. Hex-encoded IP ---
def test_rejects_hex_encoded_ip(policy):
    with pytest.raises(URLPolicyViolation) as excinfo:
        policy.validate("http://0x7f000001/")
    assert "encoded" in excinfo.value.reason
    assert "127.0.0.1" in excinfo.value.reason


# --- 7. Public hostname whose DNS resolves to private (rebinding) ---
def test_rejects_hostname_resolving_to_private(policy, monkeypatch):
    calls = []

    def fake_getaddrinfo(host, port, *args, **kwargs):
        calls.append(host)
        return _addrinfo("10.0.0.5")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    url = "http://rebind.example.com/internal"
    with pytest.raises(URLPolicyViolation) as excinfo:
        policy.validate(url)
    assert "dns" in excinfo.value.reason
    assert "10.0.0.5" in excinfo.value.reason
    assert calls == ["rebind.example.com"]  # validator really resolved


# --- 8. URL with no host ---
def test_rejects_url_without_host(policy):
    _assert_violation(policy, "http:///path", "host")


# --- 9. CRLF injection ---
def test_rejects_crlf_injection(policy):
    _assert_violation(policy, "http://example.com/\r\nHost:evil.com", "crlf")


# --- 10. Userinfo evasion ---
def test_rejects_userinfo_evasion(policy):
    _assert_violation(policy, "http://example.com@127.0.0.1/", "userinfo")


# --- Positive: public URLs pass cleanly ---
def test_validate_public_http_url_allowed(policy, public_dns):
    policy.validate("https://example.com/")


def test_validate_public_with_path_query_allowed(policy, public_dns):
    policy.validate("https://api.example.com/v1/data?q=1")


def test_validate_subdomain_allowed(policy, public_dns):
    policy.validate("https://blog.example.com/")


# --- Documented behavior: unresolvable hostname is not a violation ---
def test_unresolvable_hostname_passes_through(policy, monkeypatch):
    """NXDOMAIN reaches nothing → no SSRF risk at validation time.

    The HTTP client fails at connect; the rebinding TOCTOU window is
    owned by the DNS-pinning layer (full PR #100), not this validator.
    """

    def fake_getaddrinfo(host, port, *args, **kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    policy.validate("https://does-not-exist.example.com/")
