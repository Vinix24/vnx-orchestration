"""Adversarial test suite for the SSRF-safe URLPolicy validator.

10 adversarial cases (one per threat model entry) + 3 positive cases.
"""

from __future__ import annotations

import os
import socket
import sys

import pytest

# Ensure the sibling url_policy module is importable regardless of pytest's
# rootdir detection.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from url_policy import URLPolicy, URLPolicyViolation  # noqa: E402


@pytest.fixture
def policy() -> URLPolicy:
    return URLPolicy()


def _fake_resolver(ip: str):
    """Return a getaddrinfo replacement that always resolves to ``ip``."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return fake_getaddrinfo


# ---------------------------------------------------------------------------
# 10 adversarial cases — each maps 1:1 to a threat-model entry.
# ---------------------------------------------------------------------------


def test_reject_private_ipv4_loopback(policy):
    """Threat 1 — private IPv4 loopback range (127.0.0.0/8)."""
    with pytest.raises(URLPolicyViolation) as exc_info:
        policy.validate("http://127.0.0.1/admin")
    assert exc_info.value.reason == "localhost"
    assert exc_info.value.url == "http://127.0.0.1/admin"


def test_reject_aws_metadata_endpoint(policy):
    """Threat 2 — AWS/GCP/Azure IMDS at 169.254.169.254."""
    with pytest.raises(URLPolicyViolation) as exc_info:
        policy.validate("http://169.254.169.254/latest/meta-data/iam/")
    assert exc_info.value.reason == "metadata"


def test_reject_non_http_scheme_file(policy):
    """Threat 3 — non-HTTP scheme (file://)."""
    with pytest.raises(URLPolicyViolation) as exc_info:
        policy.validate("file:///etc/passwd")
    assert exc_info.value.reason == "scheme"


def test_reject_localhost_hostname(policy):
    """Threat 4 — localhost / 0.0.0.0 / ip6-localhost variants."""
    with pytest.raises(URLPolicyViolation) as exc_info:
        policy.validate("http://localhost:8080/")
    assert exc_info.value.reason == "localhost"


def test_reject_decimal_encoded_ip(policy):
    """Threat 5 — decimal-encoded IPv4 (2130706433 == 127.0.0.1)."""
    with pytest.raises(URLPolicyViolation) as exc_info:
        policy.validate("http://2130706433/")
    assert exc_info.value.reason == "encoded"


def test_reject_hex_encoded_ip(policy):
    """Threat 6 — hex-encoded IPv4 (0x7f000001 == 127.0.0.1)."""
    with pytest.raises(URLPolicyViolation) as exc_info:
        policy.validate("http://0x7f000001/")
    assert exc_info.value.reason == "encoded"


def test_reject_dns_resolves_to_private(policy, monkeypatch):
    """Threat 7 — public hostname whose A record points at RFC1918 space."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolver("10.0.0.42"))
    with pytest.raises(URLPolicyViolation) as exc_info:
        policy.validate("http://internal-service.example.com/health")
    assert exc_info.value.reason == "dns_private"


def test_reject_url_with_no_host(policy):
    """Threat 8 — URL with no host component."""
    with pytest.raises(URLPolicyViolation) as exc_info:
        policy.validate("http:///path/only")
    assert exc_info.value.reason == "no_host"


def test_reject_crlf_injection(policy):
    """Threat 9 — CRLF injection inside the URL string."""
    with pytest.raises(URLPolicyViolation) as exc_info:
        policy.validate("http://example.com/\r\nHost:evil.com")
    assert exc_info.value.reason == "crlf"


def test_reject_userinfo_evasion(policy):
    """Threat 10 — userinfo trick to hide the real host (example.com@127.0.0.1)."""
    with pytest.raises(URLPolicyViolation) as exc_info:
        policy.validate("http://example.com@127.0.0.1/")
    assert exc_info.value.reason == "userinfo"


# ---------------------------------------------------------------------------
# 3 positive cases — legitimate public URLs must pass cleanly.
# ---------------------------------------------------------------------------


def test_validate_public_http_url_allowed(policy, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolver("93.184.216.34"))
    policy.validate("https://example.com/")


def test_validate_public_with_path_query_allowed(policy, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolver("93.184.216.34"))
    policy.validate("https://api.example.com/v1/data?q=1")


def test_validate_subdomain_allowed(policy, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolver("93.184.216.34"))
    policy.validate("https://blog.example.com/")
