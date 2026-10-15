"""R4 (OI-2051): a broken plist is a handled error, not an ExpatError traceback.

plistlib raises ``xml.parsers.expat.ExpatError`` on malformed XML, which is none
of InvalidFileException / ValueError / OSError. Both readers below must treat it
exactly like the errors they already handle. Tmp plists only: no real
~/Library/LaunchAgents, no launchctl.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT / "scripts" / "launchd", REPO_ROOT / "scripts" / "lib", REPO_ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import launchd_project_scope as lps
from launchd_test_support import make_central_engine

BROKEN_XML = '<?xml version="1.0"?><plist><dict><key>Label</key><string>com.vnx.x</dict></plist>'
PLIST_NAME = "com.vnx.gate-obligation-runner"


def test_read_template_label_returns_none_on_broken_xml(tmp_path):
    plist = tmp_path / "broken.plist"
    plist.write_text(BROKEN_XML, encoding="utf-8")
    assert lps._read_template_label(plist) is None


def test_read_template_label_still_reads_valid_plist(tmp_path):
    plist = tmp_path / "ok.plist"
    plist.write_text(
        '<?xml version="1.0"?><plist><dict><key>Label</key>'
        "<string>com.vnx.ok</string></dict></plist>",
        encoding="utf-8",
    )
    assert lps._read_template_label(plist) == "com.vnx.ok"


def test_install_launchd_agent_broken_template_is_a_handled_error(tmp_path, monkeypatch):
    from vnx_cli.commands import init_cmd

    engine_root = tmp_path / "engine"
    make_central_engine(engine_root)
    launchd_dir = engine_root / "scripts" / "launchd"
    launchd_dir.mkdir(parents=True)
    (launchd_dir / f"{PLIST_NAME}.plist").write_text(BROKEN_XML, encoding="utf-8")
    monkeypatch.setattr(init_cmd._engine, "engine_root", lambda: engine_root)

    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    with pytest.raises(RuntimeError, match="could not read a Label"):
        init_cmd._install_launchd_agent(str(engine_root), PLIST_NAME)
    assert not list((fake_home / "Library" / "LaunchAgents").glob("*.plist"))
