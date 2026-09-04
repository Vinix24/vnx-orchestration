#!/usr/bin/env python3
"""Tests for the SMTP password resolution in send_digest_email.py.

Covers the 2026-09-03 fix: VNX_SMTP_PASS was removed from the operator's
~/.zshrc export (it leaked to every worker process) and moved into the
macOS keychain. send_email() must fall back to the keychain when the env
var is empty, without ever shelling out to `security` in tests.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import send_digest_email  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_smtp_env(monkeypatch):
    """Start every test from a known-empty SMTP env so leakage from the
    real environment (or a prior test) can't mask a broken fallback."""
    for var in ("VNX_DIGEST_EMAIL", "VNX_SMTP_PASS", "VNX_SMTP_USER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("VNX_DIGEST_EMAIL", "ops@example.com")


def test_env_var_wins_keychain_not_consulted(monkeypatch):
    monkeypatch.setenv("VNX_SMTP_PASS", "env-secret")
    keychain = mock.Mock(side_effect=AssertionError("keychain should not be called"))
    monkeypatch.setattr(send_digest_email, "_read_smtp_pass_from_keychain", keychain)

    assert send_digest_email.send_email("subject", "body", dry_run=True) is True
    keychain.assert_not_called()


def test_empty_env_falls_back_to_keychain(monkeypatch):
    monkeypatch.setattr(
        send_digest_email, "_read_smtp_pass_from_keychain", lambda: "keychain-secret"
    )
    captured = {}

    class _FakeServer:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def ehlo(self):
            pass

        def starttls(self):
            pass

        def login(self, user, password):
            captured["password"] = password

        def send_message(self, msg):
            pass

    monkeypatch.setattr(
        send_digest_email.smtplib, "SMTP", lambda *a, **k: _FakeServer()
    )

    assert send_digest_email.send_email("subject", "body", dry_run=False) is True
    assert captured["password"] == "keychain-secret"


def test_keychain_missing_item_returns_empty_string_no_raise(monkeypatch):
    import subprocess

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=44, stdout="", stderr="not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert send_digest_email._read_smtp_pass_from_keychain() == ""


def test_keychain_security_binary_absent_returns_empty_string_no_raise(monkeypatch):
    import subprocess

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("security: command not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert send_digest_email._read_smtp_pass_from_keychain() == ""


def test_env_and_keychain_both_empty_fails_with_existing_error(monkeypatch, capsys):
    monkeypatch.setattr(send_digest_email, "_read_smtp_pass_from_keychain", lambda: "")

    result = send_digest_email.send_email("subject", "body", dry_run=False)

    assert result is False
    err = capsys.readouterr().err
    assert "VNX_SMTP_PASS not set" in err
