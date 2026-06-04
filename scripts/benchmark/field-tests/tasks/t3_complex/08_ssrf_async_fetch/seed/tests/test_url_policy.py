"""Adversarial test suite for URLPolicy — 10 threat cases + 3 positive cases."""
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


# ===== Adversarial tests (10 threats) =====

def test_private_ip_loopback(policy: URLPolicy) -> None:
    """Threat 1: private IP range — loopback 127.0.0.1."""
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("http://127.0.0.1/")
    assert "private" in exc.value.reason


def test_cloud_metadata_endpoint(policy: URLPolicy) -> None:
    """Threat 2: AWS IMDS cloud metadata endpoint."""
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("http://169.254.169.254/latest/meta-data/")
    assert "metadata" in exc.value.reason


def test_non_http_scheme_file(policy: URLPolicy) -> None:
    """Threat 3: non-HTTP scheme — file://."""
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("file:///etc/passwd")
    assert "scheme" in exc.value.reason


def test_non_http_scheme_javascript(policy: URLPolicy) -> None:
    """Threat 3 (variant): non-HTTP scheme — javascript:."""
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("javascript:alert(1)")
    assert "scheme" in exc.value.reason


def test_localhost_variant(policy: URLPolicy) -> None:
    """Threat 4: localhost name variant."""
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("http://localhost/admin")
    assert "localhost" in exc.value.reason


def test_decimal_encoded_ip(policy: URLPolicy) -> None:
    """Threat 5: decimal-encoded IP — 2130706433 == 127.0.0.1."""
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("http://2130706433/")
    assert "encoded" in exc.value.reason


def test_hex_encoded_ip(policy: URLPolicy) -> None:
    """Threat 6: hex-encoded IP — 0x7f000001 == 127.0.0.1."""
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("http://0x7f000001/")
    assert "encoded" in exc.value.reason


def test_dns_resolves_to_private(
    policy: URLPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Threat 7: DNS-rebinding partial defense — hostname resolves to RFC1918."""
    def _mock_getaddrinfo(host: str, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _mock_getaddrinfo)
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("http://evil-internal.example.com/")
    assert "dns_private" in exc.value.reason


def test_no_host(policy: URLPolicy) -> None:
    """Threat 8: URL with empty host component."""
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("http:///path")
    assert "host" in exc.value.reason


def test_crlf_injection(policy: URLPolicy) -> None:
    """Threat 9: CRLF injection to inject a second Host header."""
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("http://example.com/\r\nHost:evil.com")
    assert "crlf" in exc.value.reason


def test_userinfo_evasion(policy: URLPolicy) -> None:
    """Threat 10: userinfo prefix masks true destination (example.com@127.0.0.1)."""
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("http://example.com@127.0.0.1/")
    assert "userinfo" in exc.value.reason


# ===== Positive tests (public URLs must pass) =====

def test_validate_public_http_url_allowed(
    policy: URLPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """https://example.com/ must not be blocked."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )
    policy.validate("https://example.com/")


def test_validate_public_with_path_query_allowed(
    policy: URLPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """https://api.example.com/v1/data?q=1 must not be blocked."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )
    policy.validate("https://api.example.com/v1/data?q=1")


def test_validate_subdomain_allowed(
    policy: URLPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """https://blog.example.com/ must not be blocked when DNS returns a public IP."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )
    policy.validate("https://blog.example.com/")
