#!/usr/bin/env python3
"""Tests for the dashboard mutation-auth guard (audit critical #1).

Dispatch-ID: 20260627-audit-dashboard-auth

Covers the loopback-bind classifier, the same-origin (CSRF) check, and _mutation_forbidden across
loopback/non-loopback + token-configured/absent + same/cross-origin.
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "dashboard"))

import serve_dashboard as sd  # noqa: E402


class _FakeHandler:
    """Minimal stand-in: the guard only touches handler.headers.get(...)."""
    def __init__(self, headers=None):
        self.headers = headers or {}


@pytest.fixture(autouse=True)
def _reset():
    orig = (sd._LOOPBACK_BIND, sd._DASHBOARD_TOKEN)
    yield
    sd._LOOPBACK_BIND, sd._DASHBOARD_TOKEN = orig


# ---------------------------------------------------------------------------
# bind classifier
# ---------------------------------------------------------------------------

def test_bind_is_loopback():
    assert sd._bind_is_loopback("127.0.0.1") is True
    assert sd._bind_is_loopback("::1") is True
    assert sd._bind_is_loopback("localhost") is True
    assert sd._bind_is_loopback("0.0.0.0") is False
    assert sd._bind_is_loopback("::") is False
    assert sd._bind_is_loopback("192.168.1.5") is False


# ---------------------------------------------------------------------------
# same-origin / CSRF
# ---------------------------------------------------------------------------

def test_origin_same_site():
    assert sd._origin_same_site(_FakeHandler({})) is True  # no Origin (curl)
    assert sd._origin_same_site(_FakeHandler({"Origin": "http://127.0.0.1:4173", "Host": "127.0.0.1:4173"})) is True
    assert sd._origin_same_site(_FakeHandler({"Origin": "http://evil.example", "Host": "127.0.0.1:4173"})) is False


# ---------------------------------------------------------------------------
# _mutation_forbidden
# ---------------------------------------------------------------------------

def test_loopback_no_token_same_origin_allowed():
    sd._LOOPBACK_BIND, sd._DASHBOARD_TOKEN = True, ""
    assert sd._mutation_forbidden(_FakeHandler({})) is None


def test_cross_origin_rejected_even_on_loopback():
    sd._LOOPBACK_BIND, sd._DASHBOARD_TOKEN = True, ""
    err = sd._mutation_forbidden(_FakeHandler({"Origin": "http://evil.example", "Host": "127.0.0.1:4173"}))
    assert err and "cross-origin" in err


def test_non_loopback_without_token_disables_mutations():
    sd._LOOPBACK_BIND, sd._DASHBOARD_TOKEN = False, ""
    err = sd._mutation_forbidden(_FakeHandler({}))
    assert err and "VNX_DASHBOARD_TOKEN" in err


def test_token_required_when_configured():
    sd._LOOPBACK_BIND, sd._DASHBOARD_TOKEN = True, "s3cret"
    assert sd._mutation_forbidden(_FakeHandler({})) is not None  # missing token
    assert sd._mutation_forbidden(_FakeHandler({"X-VNX-Dashboard-Token": "wrong"})) is not None
    assert sd._mutation_forbidden(_FakeHandler({"X-VNX-Dashboard-Token": "s3cret"})) is None


def test_non_loopback_with_valid_token_allowed():
    sd._LOOPBACK_BIND, sd._DASHBOARD_TOKEN = False, "s3cret"
    assert sd._mutation_forbidden(_FakeHandler({"X-VNX-Dashboard-Token": "s3cret"})) is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
