"""Adversarial test suite for URLPolicy SSRF validator.

10 adversarial tests (one per threat) + 3 positive tests = 13 total.
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from url_policy import URLPolicy, URLPolicyViolation


@pytest.fixture
def policy() -> URLPolicy:
    return URLPolicy()


@pytest.fixture
def public_dns(monkeypatch):
    """Patch getaddrinfo to return a public IP for any hostname."""

    def _resolve(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _resolve)


# ── Adversarial tests (10) ────────────────────────────────────────────────────


def test_rejects_private_ip_loopback(policy):
    """Threat 1: loopback address 127.0.0.1 (127.0.0.0/8)."""
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("http://127.0.0.1/")
    assert "private" in exc.value.reason


def test_rejects_cloud_metadata_endpoint(policy):
    """Threat 2: AWS IMDS endpoint 169.254.169.254."""
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("http://169.254.169.254/")
    assert "cloud_metadata" in exc.value.reason or "metadata" in exc.value.reason


def test_rejects_file_scheme(policy):
    """Threat 3: non-HTTP scheme — file://."""
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("file:///etc/passwd")
    assert "scheme" in exc.value.reason


def test_rejects_localhost_name(policy):
    """Threat 4: localhost name variant."""
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("http://localhost/")
    assert "localhost" in exc.value.reason


def test_rejects_decimal_encoded_ip(policy):
    """Threat 5: decimal-encoded IP 2130706433 == 127.0.0.1."""
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("http://2130706433/")
    assert "private" in exc.value.reason


def test_rejects_hex_encoded_ip(policy):
    """Threat 6: hex-encoded IP 0x7f000001 == 127.0.0.1."""
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("http://0x7f000001/")
    assert "private" in exc.value.reason


def test_rejects_dns_resolves_to_private(monkeypatch, policy):
    """Threat 7: public hostname whose A record points to RFC1918 (DNS-rebinding guard)."""

    def _mock_resolve(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _mock_resolve)

    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("http://internal.example.com/")
    assert "dns" in exc.value.reason


def test_rejects_url_with_no_host(policy):
    """Threat 8: URL with an empty host component (http:///path)."""
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("http:///path")
    assert "no_host" in exc.value.reason


def test_rejects_crlf_injection(policy):
    """Threat 9: CRLF sequence in URL enables HTTP header injection."""
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("http://example.com/\r\nHost:evil.com")
    assert "crlf" in exc.value.reason


def test_rejects_userinfo_evasion(policy):
    """Threat 10: userinfo field hides real destination (http://trusted@evil.host/)."""
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("http://example.com@127.0.0.1/")
    assert "userinfo" in exc.value.reason


# ── Positive tests (3) ────────────────────────────────────────────────────────


def test_validate_public_http_url_allowed(policy, public_dns):
    """Public HTTPS URL must pass without raising."""
    policy.validate("https://example.com/")


def test_validate_public_with_path_query_allowed(policy, public_dns):
    """Public URL with path and query string must pass without raising."""
    policy.validate("https://api.example.com/v1/data?q=1")


def test_validate_subdomain_allowed(policy, public_dns):
    """Subdomain of a public domain must pass without raising."""
    policy.validate("https://blog.example.com/")
