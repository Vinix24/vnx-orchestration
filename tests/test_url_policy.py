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

from url_policy import (
    URLPolicy,
    URLPolicyViolation,
    open_pinned_connection,
)


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
    assert "non_global_ip" in exc.value.reason
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
    assert "non_global_ip" in exc.value.reason
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


def test_11_cgnat_rejected(policy: URLPolicy) -> None:
    """F1 — 100.64.0.0/10 (CGNAT) is not ``is_private`` but is non-global."""
    for host in ("100.64.0.1", "100.127.255.255"):
        with pytest.raises(URLPolicyViolation) as exc:
            policy.validate(f"http://{host}/")
        assert "non_global_ip" in exc.value.reason, host
        assert host in exc.value.reason, host


def test_11b_public_ip_still_passes_cgnat_fix(
    policy: URLPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1 regression guard: a real public IP must still validate cleanly."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        _fake_getaddrinfo({"public.example.com": ["93.184.216.34"]}),
    )
    policy.validate("http://93.184.216.34/")
    policy.validate("https://public.example.com/")


# ---------------------------------------------------------------------------
# F2 — DNS-rebinding TOCTOU (validate_and_pin)
# ---------------------------------------------------------------------------


def test_12_validate_and_pin_returns_vetted_ip(
    policy: URLPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        _fake_getaddrinfo({"api.example.com": ["93.184.216.34"]}),
    )
    target = policy.validate_and_pin("https://api.example.com/v1")
    assert target.hostname == "api.example.com"
    assert target.pinned_ip == "93.184.216.34"
    assert target.port == 443
    assert target.scheme == "https"
    assert target.url == "https://api.example.com/v1"


def test_13_validate_and_pin_rejects_unsafe_target(
    policy: URLPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        _fake_getaddrinfo({"sneaky.example.com": ["10.20.30.40"]}),
    )
    with pytest.raises(URLPolicyViolation):
        policy.validate_and_pin("https://sneaky.example.com/")


def test_14_pin_survives_dns_rebind(
    policy: URLPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates the classic TOCTOU: the resolver answers with a public IP
    at validate-time, then flips to a private IP on the next lookup (as a
    rebind attacker's authoritative DNS server would for a low-TTL record).
    A caller connecting via the pinned target must still reach the public
    IP that was actually vetted, never the rebound private one.
    """
    calls = {"n": 0}

    def rebinding_resolver(host, *_args, **_kwargs):
        calls["n"] += 1
        addr = "93.184.216.34" if calls["n"] == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (addr, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", rebinding_resolver)

    target = policy.validate_and_pin("http://rebind.example.com/")
    assert target.pinned_ip == "93.184.216.34"

    # A later, independent resolution of the same host now returns the
    # rebound private IP — proving the window exists...
    second_lookup = socket.getaddrinfo("rebind.example.com", None)
    assert second_lookup[0][4][0] == "127.0.0.1"

    # ...but the pinned target is untouched: it never re-resolves.
    assert target.pinned_ip == "93.184.216.34"

    connect_calls = []

    class _FakeSocket:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    def fake_create_connection(address, *_args, **_kwargs):
        connect_calls.append(address)
        return _FakeSocket()

    conn = open_pinned_connection(target, timeout=1.0)
    monkeypatch.setattr(conn, "_create_connection", fake_create_connection)
    conn.connect()

    # The socket connects to the pinned IP, never to a fresh DNS answer —
    # even though "rebind.example.com" now resolves to 127.0.0.1.
    assert connect_calls == [("93.184.216.34", 80)]
    assert conn.host == "rebind.example.com"


def test_15_https_pin_connects_by_ip_but_verifies_original_hostname(
    policy: URLPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTTPS pinning must dial the pinned IP while still presenting the
    original hostname for TLS SNI + certificate verification — otherwise
    the connection either fails cert validation or (worse) silently skips
    it."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        _fake_getaddrinfo({"secure.example.com": ["93.184.216.34"]}),
    )
    target = policy.validate_and_pin("https://secure.example.com/")

    connect_calls = []
    wrap_calls = []

    class _FakeSocket:
        pass

    def fake_create_connection(address, *_args, **_kwargs):
        connect_calls.append(address)
        return _FakeSocket()

    def fake_wrap_socket(sock, server_hostname=None, **_kwargs):
        wrap_calls.append(server_hostname)
        return sock

    conn = open_pinned_connection(target, timeout=1.0)
    monkeypatch.setattr(conn, "_create_connection", fake_create_connection)
    monkeypatch.setattr(conn._context, "wrap_socket", fake_wrap_socket)
    conn.connect()

    assert connect_calls == [("93.184.216.34", 443)]
    assert wrap_calls == ["secure.example.com"]


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
