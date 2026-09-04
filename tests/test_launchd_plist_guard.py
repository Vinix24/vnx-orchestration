"""tests/test_launchd_plist_guard.py — OI-1621 regression guard.

OI-1621 measured two real, currently-live defects:

  1. ``scripts/launchd/com.vnx.gate-obligation-runner.plist`` was not
     well-formed XML — a literal ``--`` inside an XML comment (invalid on
     every XML parser, not just a strict one).
  2. The SessionStart PATH-parity hook flagged
     ``com.vnx.gate-obligation-runner.vnx-dev`` and ``com.vnx.ledger-health``
     as broken: both resolve ``/usr/bin/python3`` (3.9) against this
     project's ``requires-python`` (``>=3.11,<3.14``) — but that was the
     SCANNER's fault (``discover_launchd_consumers`` hardcoded
     ``BACKGROUND_PATH`` for every launchd consumer instead of honoring the
     plist's own declared ``EnvironmentVariables.PATH``, which both jobs set
     to a homebrew-first PATH precisely to avoid the Xcode-CLT stub).

This module is the guard against both regressing: it walks every plist
TEMPLATE this repo ships under ``scripts/launchd/`` (not the installed
copies) and asserts (a) well-formed XML, (b) any interpreter a template's
``ProgramArguments`` resolves to falls inside this project's
``requires-python`` -- read from ``pyproject.toml``, never hardcoded here.

The narrow scanner-plumbing regression tests for defect 2 live alongside
``discover_launchd_consumers``/``scan_consumers`` in ``tests/test_path_parity.py``;
this file is the broader, whole-directory sweep plus the XML check.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import path_parity  # noqa: E402

LAUNCHD_TEMPLATES_DIR = REPO_ROOT / "scripts" / "launchd"


def _real_requires_python() -> Optional[str]:
    requires_python = path_parity.parse_requires_python(REPO_ROOT / "pyproject.toml")
    assert requires_python, "pyproject.toml must declare requires-python for this guard to mean anything"
    return requires_python


# ─────────────────────────────────────────────────────────────────────────
# Defect 1 — well-formed XML, every plist in the directory, always
# deterministic (no environment/host dependency at all).
# ─────────────────────────────────────────────────────────────────────────


def test_real_repo_launchd_plists_are_well_formed_xml() -> None:
    plists = sorted(LAUNCHD_TEMPLATES_DIR.glob("*.plist"))
    assert len(plists) >= 6, f"expected the known launchd templates, found {len(plists)}: {plists}"

    result = path_parity.check_repo_launchd_templates(LAUNCHD_TEMPLATES_DIR, REPO_ROOT, _real_requires_python())
    assert result["xml_errors"] == [], result["xml_errors"]


# ─────────────────────────────────────────────────────────────────────────
# check_repo_launchd_templates — the guard function itself, proven against
# synthetic fixtures so it can be shown to actually fail, not just report
# green because nothing was checked (nul-is-eerst-een-meetfout).
# ─────────────────────────────────────────────────────────────────────────

_INVALID_XML_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <!-- a literal -- inside a comment, forbidden by XML -->
  <key>Label</key><string>com.vnx.broken</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>-c</string><string>echo hi</string></array>
</dict>
</plist>
"""


def _plist_shaped_like_gate_obligation_runner(env_path: Optional[str]) -> str:
    env_block = (
        f"""
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>{env_path}</string>
  </dict>"""
        if env_path
        else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.vnx.fake-runner</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-c</string>
    <string>cd ${{VNX_HOME}} &amp;&amp; exec python3 fake_script.py</string>
  </array>{env_block}
</dict>
</plist>
"""


def test_check_repo_launchd_templates_reports_invalid_xml(tmp_path: Path) -> None:
    templates_dir = tmp_path / "launchd"
    templates_dir.mkdir()
    (templates_dir / "com.vnx.broken.plist").write_text(_INVALID_XML_PLIST, encoding="utf-8")

    result = path_parity.check_repo_launchd_templates(templates_dir, tmp_path, ">=3.11,<3.14")

    assert result["ok"] is False
    assert len(result["xml_errors"]) == 1
    assert result["xml_errors"][0]["file"] == "com.vnx.broken.plist"


def test_check_repo_launchd_templates_flags_out_of_range_interpreter(tmp_path: Path, monkeypatch) -> None:
    """Negative control: a template whose bare python3 falls back to the
    launchd default PATH (no declared EnvironmentVariables.PATH at all)
    must turn the guard red when that default resolves an out-of-range
    interpreter — the exact shape of the real OI-1621 scanner bug, now
    reproduced as a genuine template defect instead of a scanner defect."""
    templates_dir = tmp_path / "launchd"
    templates_dir.mkdir()
    (templates_dir / "com.vnx.fake-runner.plist").write_text(
        _plist_shaped_like_gate_obligation_runner(env_path=None), encoding="utf-8"
    )
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def fake_which(name, path=None):
        assert name == "python3"
        assert path == path_parity.BACKGROUND_PATH
        return "/usr/bin/python3.9"

    monkeypatch.setattr(path_parity.shutil, "which", fake_which)

    result = path_parity.check_repo_launchd_templates(templates_dir, repo_root, ">=3.11,<3.14")

    assert result["ok"] is False
    assert result["xml_errors"] == []
    assert len(result["mismatches"]) == 1
    mismatch = result["mismatches"][0]
    assert mismatch["kind"] == "template_interpreter_out_of_range"
    assert mismatch["file"] == "com.vnx.fake-runner.plist"
    assert mismatch["version"] == "3.9"


def test_check_repo_launchd_templates_passes_when_interpreter_in_range(tmp_path: Path, monkeypatch) -> None:
    """Positive control paired with the negative one above: a template that
    DOES declare its own homebrew-first PATH resolves an in-range
    interpreter and the guard stays green."""
    templates_dir = tmp_path / "launchd"
    templates_dir.mkdir()
    (templates_dir / "com.vnx.fake-runner.plist").write_text(
        _plist_shaped_like_gate_obligation_runner(env_path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"),
        encoding="utf-8",
    )
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def fake_which(name, path=None):
        assert name == "python3"
        assert path == "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        return "/opt/homebrew/opt/python@3.12/bin/python3.12"

    monkeypatch.setattr(path_parity.shutil, "which", fake_which)

    result = path_parity.check_repo_launchd_templates(templates_dir, repo_root, ">=3.11,<3.14")

    assert result["ok"] is True
    assert result["mismatches"] == []
    assert result["consumers"][0]["in_range"] is True


def test_check_repo_launchd_templates_missing_directory_is_not_ok(tmp_path: Path) -> None:
    result = path_parity.check_repo_launchd_templates(tmp_path / "no-such-dir", tmp_path, ">=3.11,<3.14")
    assert result["ok"] is False
    assert result["xml_errors"] != []


# ─────────────────────────────────────────────────────────────────────────
# The real repo directory, end-to-end, with a controlled (monkeypatched)
# interpreter resolution — proves the fixed templates + fixed scanner
# together clear the guard, without depending on what happens to be
# installed on whichever machine runs this suite.
# ─────────────────────────────────────────────────────────────────────────


def test_real_repo_launchd_templates_clear_the_guard(monkeypatch) -> None:
    requires_python = _real_requires_python()

    def fake_which(name, path=None):
        if name != "python3":
            return None
        if path == path_parity.BACKGROUND_PATH:
            # The Xcode-CLT stub every declared-PATH template exists to avoid.
            return "/usr/bin/python3.9"
        return "/opt/homebrew/opt/python@3.12/bin/python3.12"

    monkeypatch.setattr(path_parity.shutil, "which", fake_which)

    result = path_parity.check_repo_launchd_templates(LAUNCHD_TEMPLATES_DIR, REPO_ROOT, requires_python)

    assert result["xml_errors"] == []
    assert result["mismatches"] == [], result["mismatches"]
    assert result["ok"] is True

    # nul-is-eerst-een-meetfout: prove the scan actually found and judged the
    # two OI-1621 consumers, not that it silently checked nothing.
    #
    # com.vnx.gate-obligation-runner's Label carries the literal, unsubstituted
    # "${VNX_PROJECT_ID}" placeholder as of OI-1509/OI-1510 (golf 3): the Label
    # is now per-project-scoped, same as its EnvironmentVariables entry always
    # was, so two projects installing this template resolve to different
    # launchd Labels instead of colliding. check_repo_launchd_templates reads
    # the template's Label raw (it only substitutes ${VNX_HOME} inside
    # ProgramArguments for the interpreter check, never the Label), so the
    # value this scan reports is that unsubstituted placeholder form.
    gate_obligation_runner_label = "com.vnx.gate-obligation-runner.${VNX_PROJECT_ID}"
    checked_labels = {
        c["label"] for c in result["consumers"] if c.get("relevant") and c.get("in_range") is not None
    }
    assert gate_obligation_runner_label in checked_labels
    assert "com.vnx.ledger-health" in checked_labels
    for c in result["consumers"]:
        if c["label"] in (gate_obligation_runner_label, "com.vnx.ledger-health"):
            assert c["in_range"] is True, c
