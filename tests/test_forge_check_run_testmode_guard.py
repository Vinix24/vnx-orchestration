#!/usr/bin/env python3
"""OI-1675: publish_check_run weigert te posten in testmodus zonder opt-in.

Dekt ALLEEN de fail-closed guard — niet het transport erachter
(tests/test_forge_check_run_client.py dekt dat, met zijn eigen autouse
opt-in-fixture). Zet VNX_FORGE_ALLOW_TEST_POST NERGENS op moduleniveau: het
hele punt van dit bestand is het DEFAULT-gedrag onder pytest bewijzen.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Tuple

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import forge_check_run as fcr  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Schone lei ongeacht wat een andere testmodule (of de ambient shell)
    heeft gezet."""
    monkeypatch.delenv(fcr.TEST_POST_OPT_IN_ENV, raising=False)


def _install_spy_api_request(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[str, str]]:
    """Registreert elke aanroep die de HTTP-seam bereikt; moet leeg blijven
    wanneer de guard vuurt. Geeft NOOIT een echt antwoord terug — een gat in
    de guard laat dit falen in plaats van stil te beantwoorden zoals een
    echte server zou doen."""
    calls: List[Tuple[str, str]] = []

    def fake_request(method, url, **kwargs):  # noqa: ANN001
        calls.append((method, url))
        raise AssertionError(
            f"_api_request bereikt tijdens een guard-test: {method} {url} — "
            "de guard had moeten weigeren vóór de netwerk-seam werd geraakt"
        )

    monkeypatch.setattr(fcr, "_api_request", fake_request)
    return calls


def test_publish_check_run_refuses_without_opt_in_under_pytest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROOD op de huidige code: er is nog geen guard, dus deze aanroep loopt
    gewoon door naar owner/repo-resolutie en credential/netwerkcode."""
    calls = _install_spy_api_request(monkeypatch)

    with pytest.raises(fcr.ForgeTestPostRefused) as excinfo:
        fcr.publish_check_run("a" * 40, "vnx-gate/review", "success", "test")

    assert calls == [], "geen uitgaande aanroep mag gebeuren wanneer de guard weigert"
    message = str(excinfo.value)
    assert "vnx-gate/review" in message, "de weigering moet de check noemen"
    assert ("a" * 40)[:12] in message, "de weigering moet de sha noemen"
    assert fcr.TEST_POST_OPT_IN_ENV in message


def test_publish_check_run_works_with_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Met de expliciete opt-in draait de echte body — tegen een gemockte
    _api_request, nooit een echte."""
    monkeypatch.setenv(fcr.TEST_POST_OPT_IN_ENV, "1")
    monkeypatch.setattr(fcr, "_installation_token_with_freshness", lambda **k: ("ghs_x", True))
    monkeypatch.setattr(fcr, "resolve_owner_repo", lambda project_root=None: "o/r")
    recorded: List[Tuple[str, str]] = []

    def fake_request(method, url, *, headers, payload=None, timeout=30):  # noqa: ANN001
        recorded.append((method, url))
        return 201, '{"id": 1, "conclusion": "success"}'

    monkeypatch.setattr(fcr, "_api_request", fake_request)

    result = fcr.publish_check_run("b" * 40, "vnx-gate/review", "success", "ok")

    assert result == {"id": 1, "conclusion": "success"}
    assert len(recorded) == 1, "alleen de check-run-POST, gemockt, geen token-exchange nodig hier"


def test_refusal_names_the_check_and_the_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_spy_api_request(monkeypatch)
    with pytest.raises(fcr.ForgeTestPostRefused) as excinfo:
        fcr.publish_check_run("c" * 40, "vnx-gate/glm_gate", "failure", "x")
    message = str(excinfo.value)
    assert "vnx-gate/glm_gate" in message
    assert ("c" * 40)[:12] in message
