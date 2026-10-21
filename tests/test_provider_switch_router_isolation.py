"""The operator's provider switch file never reaches the decision-router tests.

The operator's switch file sits at ``~/.vnx/providers.json``. The conftest pins
``VNX_PROVIDER_SWITCH_FILE`` at import time and again per test, and ``provider_switch`` reads
the path on every decision (no module-level or import-time read), so a test never sees it.
Here a home whose switch file turns every family off is put in place of the operator's: the
control shows that file decides once the pin is gone, and the router tests, run in a fresh
pytest process with that home and without the pin, pass all the same.

The operator's real ``~/.vnx/providers.json`` is never read or written: ``HOME`` points at a
tmp directory for both checks.
"""

from __future__ import annotations

import json
import os
import site
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "scripts" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import provider_switch

ROUTER_TESTS = REPO / "tests" / "test_llm_decision_router.py"


@pytest.fixture
def operator_home(tmp_path: Path) -> Path:
    """A home whose ``~/.vnx/providers.json`` switches every family off."""
    home = tmp_path / "operator-home"
    switch = home / ".vnx" / "providers.json"
    switch.parent.mkdir(parents=True)
    switch.write_text(json.dumps({"version": 1, "global": {
        family: {"enabled": False, "changed_by": "operator", "reason": "every family off"}
        for family in provider_switch.SWITCHABLE_FAMILIES
    }}), encoding="utf-8")
    return home


def test_without_the_pin_the_operator_file_decides(operator_home, monkeypatch):
    """The control: that home's file is the one a decision reads once the pin is gone."""
    monkeypatch.setenv("HOME", str(operator_home))
    monkeypatch.delenv(provider_switch.SWITCH_FILE_ENV, raising=False)
    monkeypatch.delenv("VNX_OVERRIDE_PROVIDER_LOCAL", raising=False)

    assert provider_switch.switch_file_path() == operator_home / ".vnx" / "providers.json"
    decision = provider_switch.decide("ollama", consumer="isolation-control")
    assert not decision.allowed and decision.source == "global" and decision.family == "local"


def test_an_operator_file_that_switches_every_family_off_does_not_change_the_router_tests(
        operator_home):
    """No router test takes the switched-off path (the router logs it as ``OllamaAdapter not
    called``; no router test switches a family off itself), and the file passes."""
    env = {k: v for k, v in os.environ.items()
           if k != provider_switch.SWITCH_FILE_ENV and not k.startswith("PYTEST_")
           and not k.startswith(provider_switch.OVERRIDE_ENV_PREFIX)}
    env["HOME"] = str(operator_home)
    # The user site follows HOME; keep it where this interpreter found its packages.
    env["PYTHONUSERBASE"] = site.getuserbase()

    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(ROUTER_TESTS), "-q", "-p", "no:cacheprovider",
         "-o", "log_cli=true", "--log-cli-level=WARNING"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=300,
    )

    output = result.stdout + result.stderr
    assert "OllamaAdapter not called" not in output, output[-4000:]
    assert provider_switch.REASON_SWITCHED_OFF not in output, output[-4000:]
    summary = (result.stdout.strip().splitlines() or [""])[-1]
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert " passed" in summary and "failed" not in summary and "error" not in summary, summary
