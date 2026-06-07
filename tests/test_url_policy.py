"""test_url_policy.py — Adversarial test suite for scripts/lib/url_policy.py.

Covers the SSRF threat taxonomy enumerated in the URLPolicy docstring:
private IPs, cloud metadata, non-HTTP schemes, localhost variants,
encoded IPs, DNS-resolves-to-private, missing host, CRLF injection,
userinfo evasion. Plus positive tests for genuinely public URLs.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from url_policy import URLPolicy, URLPolicyViolation


@pytest.fixture
def policy() -> URLPolicy:
    return URLPolicy()


def _fake_getaddrinfo(mapping: dict[str, list[str]]):
    """Build a getaddrinfo replacement keyed on hostname."""

    def _resolver(host, *_args, **_kwargs):
        addrs = mapping.get(host)
        if not addrs:
            raise socket.gaierror(socket.EAI_NONAME, "name not resolved")
        infos = []
        for addr in addrs:
            family = socket.AF_INET6 if ":" in addr else socket.AF_INET
            infos.append((family, socket.SOCK_STREAM, 0, "", (addr, 0)))
        return infos

    return _resolver


# ---------------------------------------------------------------------------
# Adversarial cases (10 — each maps to a numbered threat in the dispatch)
# ---------------------------------------------------------------------------


def test_01_private_ipv4_rejected(policy: URLPolicy) -> None:
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("http://10.0.0.5/admin")
    assert "private_ip" in exc.value.reason
    assert "10.0.0.5" in exc.value.reason


def test_01b_ipv6_loopback_rejected(policy: URLPolicy) -> None:
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("http://[::1]/")
    assert "loopback_ip" in exc.value.reason


def test_02_aws_metadata_ipv4_rejected(policy: URLPolicy) -> None:
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("http://169.254.169.254/latest/meta-data/")
    assert "cloud_metadata_ip" in exc.value.reason
    assert "169.254.169.254" in exc.value.reason


def test_03_non_http_scheme_rejected(policy: URLPolicy) -> None:
    for url in (
        "file:///etc/passwd",
        "gopher://example.com/",
        "ftp://example.com/x",
        "javascript:alert(1)",
        "data:text/plain,hello",
    ):
        with pytest.raises(URLPolicyViolation) as exc:
            policy.validate(url)
        assert "disallowed_scheme" in exc.value.reason, url


def test_04_localhost_hostname_rejected(policy: URLPolicy) -> None:
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("http://localhost/")
    assert "localhost_hostname" in exc.value.reason

    # 0.0.0.0 is the IPv4 unspecified address; treated as a localhost variant.
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("http://0.0.0.0/")
    assert "unspecified_ip" in exc.value.reason


def test_05_decimal_encoded_ipv4_rejected(policy: URLPolicy) -> None:
    # 2130706433 == 127.0.0.1
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("http://2130706433/")
    assert "loopback_ip" in exc.value.reason
    assert "127.0.0.1" in exc.value.reason


def test_06_hex_encoded_ipv4_rejected(policy: URLPolicy) -> None:
    # 0x7f000001 == 127.0.0.1
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("http://0x7f000001/")
    assert "loopback_ip" in exc.value.reason
    assert "127.0.0.1" in exc.value.reason


def test_07_dns_resolves_to_private_rejected(
    policy: URLPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Public hostname whose A record points into RFC1918 must be rejected."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        _fake_getaddrinfo({"sneaky.example.com": ["10.20.30.40"]}),
    )
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("https://sneaky.example.com/path")
    assert "private_ip" in exc.value.reason
    assert "10.20.30.40" in exc.value.reason


def test_08_missing_host_rejected(policy: URLPolicy) -> None:
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("http:///just/a/path")
    assert "missing_host" in exc.value.reason


def test_09_crlf_injection_rejected(policy: URLPolicy) -> None:
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("http://example.com/\r\nHost:evil.com")
    assert "control_character_in_url" in exc.value.reason


def test_10_userinfo_evasion_rejected(policy: URLPolicy) -> None:
    """http://example.com@127.0.0.1/ — real host is 127.0.0.1."""
    with pytest.raises(URLPolicyViolation) as exc:
        policy.validate("http://example.com@127.0.0.1/admin")
    assert "loopback_ip" in exc.value.reason
    assert "127.0.0.1" in exc.value.reason


# ---------------------------------------------------------------------------
# Positive cases (3 — clean public URLs)
# ---------------------------------------------------------------------------


def test_validate_public_http_url_allowed(
    policy: URLPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        _fake_getaddrinfo({"example.com": ["93.184.216.34"]}),
    )
    policy.validate("https://example.com/")


def test_validate_public_with_path_query_allowed(
    policy: URLPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        _fake_getaddrinfo({"api.example.com": ["93.184.216.34"]}),
    )
    policy.validate("https://api.example.com/v1/data?q=1")


def test_validate_subdomain_allowed(
    policy: URLPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        _fake_getaddrinfo({"blog.example.com": ["93.184.216.34"]}),
    )
    policy.validate("https://blog.example.com/")
