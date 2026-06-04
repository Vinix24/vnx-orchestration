"""Adversarial test suite for URLPolicy SSRF validator.

13 tests: 10 adversarial (one per threat vector) + 3 positive.
"""

from __future__ import annotations

import socket

import pytest

from url_policy import URLPolicy, URLPolicyViolation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def policy() -> URLPolicy:
    return URLPolicy()


def _mock_public_dns(monkeypatch, ip: str = "93.184.216.34") -> None:
    """Monkeypatch getaddrinfo to return a single public IPv4 address."""

    def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


# ---------------------------------------------------------------------------
# Adversarial tests — threat 1: private IP ranges
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,network", [
    ("http://127.0.0.1/admin", "127.0.0.0/8"),
    ("http://10.1.2.3/api", "10.0.0.0/8"),
    ("http://172.16.99.1/", "172.16.0.0/12"),
    ("http://192.168.0.1/", "192.168.0.0/16"),
    ("http://[::1]/secret", "::1/128"),
    ("http://[fd12:3456:789a:1::1]/", "fc00::/7"),
])
def test_private_ip_range_rejected(policy, url, network):
    """Any URL whose host is a private / loopback IP must be rejected."""
    with pytest.raises(URLPolicyViolation) as excinfo:
        policy.validate(url)
    reason_lower = excinfo.value.reason.lower()
    assert any(token in reason_lower for token in ("private", "loopback", "unspecified"))


# ---------------------------------------------------------------------------
# Adversarial tests — threat 2: cloud metadata endpoints
# ---------------------------------------------------------------------------

def test_cloud_metadata_ipv4_rejected(policy):
    with pytest.raises(URLPolicyViolation) as excinfo:
        policy.validate("http://169.254.169.254/latest/meta-data/")
    assert "metadata" in excinfo.value.reason.lower()


def test_cloud_metadata_ipv6_rejected(policy):
    with pytest.raises(URLPolicyViolation) as excinfo:
        policy.validate("http://[fd00:ec2::254]/")
    assert "metadata" in excinfo.value.reason.lower()


# ---------------------------------------------------------------------------
# Adversarial tests — threat 3: non-HTTP schemes
# ---------------------------------------------------------------------------

def test_non_http_scheme_file_rejected(policy):
    with pytest.raises(URLPolicyViolation) as excinfo:
        policy.validate("file:///etc/passwd")
    assert "scheme" in excinfo.value.reason.lower()


def test_non_http_scheme_javascript_rejected(policy):
    with pytest.raises(URLPolicyViolation) as excinfo:
        policy.validate("javascript:alert(1)")
    assert "scheme" in excinfo.value.reason.lower()


# ---------------------------------------------------------------------------
# Adversarial tests — threat 4: localhost variants
# ---------------------------------------------------------------------------

def test_localhost_hostname_rejected(policy):
    with pytest.raises(URLPolicyViolation) as excinfo:
        policy.validate("http://localhost:8080/")
    reason_lower = excinfo.value.reason.lower()
    assert "hostname" in reason_lower or "blocked" in reason_lower


def test_unspecified_zero_rejected(policy):
    with pytest.raises(URLPolicyViolation) as excinfo:
        policy.validate("http://0.0.0.0/")
    reason_lower = excinfo.value.reason.lower()
    assert any(token in reason_lower for token in ("unspecified", "hostname", "blocked"))


# ---------------------------------------------------------------------------
# Adversarial tests — threat 5: decimal-encoded IPs
# ---------------------------------------------------------------------------

def test_decimal_encoded_ip_rejected(policy):
    """2130706433 == 127.0.0.1"""
    with pytest.raises(URLPolicyViolation) as excinfo:
        policy.validate("http://2130706433/")
    reason_lower = excinfo.value.reason.lower()
    assert "loopback" in reason_lower or "private" in reason_lower


# ---------------------------------------------------------------------------
# Adversarial tests — threat 6: hex-encoded IPs
# ---------------------------------------------------------------------------

def test_hex_encoded_ip_rejected(policy):
    """0x7f000001 == 127.0.0.1"""
    with pytest.raises(URLPolicyViolation) as excinfo:
        policy.validate("http://0x7f000001/")
    reason_lower = excinfo.value.reason.lower()
    assert "loopback" in reason_lower or "private" in reason_lower


# ---------------------------------------------------------------------------
# Adversarial tests — threat 7: DNS-resolves-to-private
# ---------------------------------------------------------------------------

def test_dns_resolves_to_private_rejected(policy, monkeypatch):
    """DNS-rebinding partial defense: hostname resolves to RFC1918 address."""

    def mock_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", mock_getaddrinfo)

    with pytest.raises(URLPolicyViolation) as excinfo:
        policy.validate("http://safe.internal/admin")
    reason = excinfo.value.reason
    assert "private" in reason.lower() or "DNS" in reason


# ---------------------------------------------------------------------------
# Adversarial tests — threat 8: URL with no host
# ---------------------------------------------------------------------------

def test_no_host_rejected(policy):
    with pytest.raises(URLPolicyViolation) as excinfo:
        policy.validate("http:///path")
    assert "no host" in excinfo.value.reason.lower()


# ---------------------------------------------------------------------------
# Adversarial tests — threat 9: CRLF injection
# ---------------------------------------------------------------------------

def test_crlf_injection_rejected(policy):
    with pytest.raises(URLPolicyViolation) as excinfo:
        policy.validate("http://example.com/\r\nHost:evil.com")
    assert "CRLF" in excinfo.value.reason


# ---------------------------------------------------------------------------
# Adversarial tests — threat 10: userinfo evasion
# ---------------------------------------------------------------------------

def test_userinfo_evasion_rejected(policy):
    with pytest.raises(URLPolicyViolation) as excinfo:
        policy.validate("http://example.com@127.0.0.1/")
    assert "userinfo" in excinfo.value.reason.lower()


# ---------------------------------------------------------------------------
# Positive tests — public URLs must pass cleanly
# ---------------------------------------------------------------------------

def test_validate_public_http_url_allowed(policy, monkeypatch):
    _mock_public_dns(monkeypatch)
    policy.validate("https://example.com/")


def test_validate_public_with_path_query_allowed(policy, monkeypatch):
    _mock_public_dns(monkeypatch)
    policy.validate("https://api.example.com/v1/data?q=1")


def test_validate_subdomain_allowed(policy, monkeypatch):
    _mock_public_dns(monkeypatch)
    policy.validate("https://blog.example.com/")
