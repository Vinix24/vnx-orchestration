#!/usr/bin/env python3
"""Tests for the ``claude-cli`` doctor check and ``scripts/probe_claude_cli.sh``.

The check WARNs when the installed ``claude --version`` is not the version of the
newest row in ``docs/operations/CLAUDE_CLI_MEASUREMENTS.md``, and when that row is
not a measurement (outcome ja/nee plus the probe's sha256). The probe runs against
a fake ``claude`` on PATH in a temp dir: no real CLI is ever started. The doctor
function is looked up inside each test so the red run on the old code is an
assertion failure, not a collection error.
"""

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PROBE = REPO / "scripts" / "probe_claude_cli.sh"
SHA = "a" * 64
HEADER = (
    "# Claude CLI measurements\n\n"
    "| Date | claude --version | Cell B outcome | Probe output sha256 |\n"
    "|---|---|---|---|\n"
)


def _check(engine_root: Path, installed):
    import vnx_cli.commands.doctor as doctor

    fn = getattr(doctor, "_check_claude_cli", None)
    assert fn is not None, "doctor has no _check_claude_cli"
    return fn(engine_root=engine_root, installed=installed)


def _engine_with_rows(tmp_path: Path, *rows: str) -> Path:
    doc = tmp_path / "docs" / "operations" / "CLAUDE_CLI_MEASUREMENTS.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(HEADER + "".join(r + "\n" for r in rows), encoding="utf-8")
    return tmp_path


def _row(version: str, outcome: str = "ja", sha: str = SHA, date: str = "2026-11-07") -> str:
    return f"| {date} | {version} | {outcome} | {sha} |"


# ---- doctor check ---------------------------------------------------------


def test_version_mismatch_warns_and_names_the_probe(tmp_path):
    engine = _engine_with_rows(tmp_path, _row("1.2.3"))
    result = _check(engine, "1.2.4")
    assert result.status == "WARN"
    assert "scripts/probe_claude_cli.sh" in result.detail
    assert "1.2.3" in result.detail and "1.2.4" in result.detail


def test_matching_version_with_hash_passes(tmp_path):
    engine = _engine_with_rows(tmp_path, _row("1.2.3"))
    assert _check(engine, "1.2.3").status == "PASS"


def test_nee_outcome_with_hash_is_still_a_measurement(tmp_path):
    engine = _engine_with_rows(tmp_path, _row("1.2.3", outcome="nee"))
    assert _check(engine, "1.2.3").status == "PASS"


@pytest.mark.parametrize("sha", ["-", "", "abc", "A" * 64, "g" * 64, "a" * 63])
def test_row_without_a_valid_probe_hash_warns(tmp_path, sha):
    engine = _engine_with_rows(tmp_path, _row("1.2.3", sha=sha))
    result = _check(engine, "1.2.3")
    assert result.status == "WARN"
    assert "sha256" in result.detail
    assert "scripts/probe_claude_cli.sh" in result.detail


@pytest.mark.parametrize("outcome", ["niet gemeten: timeout after 120s", "", "maybe", "JA"])
def test_row_with_a_non_decisive_outcome_warns(tmp_path, outcome):
    engine = _engine_with_rows(tmp_path, _row("1.2.3", outcome=outcome))
    assert _check(engine, "1.2.3").status == "WARN"


def test_only_the_newest_row_counts(tmp_path):
    engine = _engine_with_rows(tmp_path, _row("1.2.3"), _row("1.2.4", sha="-"))
    assert _check(engine, "1.2.3").status == "WARN"
    assert _check(engine, "1.2.4").status == "WARN"


def test_header_only_doc_warns(tmp_path):
    engine = _engine_with_rows(tmp_path)
    result = _check(engine, "1.2.3")
    assert result.status == "WARN"
    assert "no measurement row" in result.detail


def test_missing_doc_warns(tmp_path):
    result = _check(tmp_path, "1.2.3")
    assert result.status == "WARN"
    assert "scripts/probe_claude_cli.sh" in result.detail


def test_undecodable_doc_warns(tmp_path):
    engine = _engine_with_rows(tmp_path)
    (engine / "docs" / "operations" / "CLAUDE_CLI_MEASUREMENTS.md").write_bytes(b"\xff\xfe\x00")
    assert _check(engine, "1.2.3").status == "WARN"


def test_unreadable_installed_version_is_not_a_mismatch(tmp_path, monkeypatch):
    import vnx_cli.commands.doctor as doctor

    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    engine = _engine_with_rows(tmp_path, _row("1.2.3"))
    result = doctor._check_claude_cli(engine_root=engine)
    assert result.status == "PASS"
    assert "nothing to compare" in result.detail


def test_installed_version_comes_from_claude_dash_dash_version(tmp_path, monkeypatch):
    import vnx_cli.commands.doctor as doctor

    fake = _fake_claude(tmp_path / "bin", version="9.8.7")
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
    assert doctor._claude_cli_version() == "9.8.7"


def test_vnx_doctor_lists_the_claude_cli_check(tmp_path, monkeypatch, capsys):
    import argparse
    import json

    import vnx_cli.commands.doctor as doctor

    monkeypatch.setenv("HOME", str(tmp_path))
    doctor.vnx_doctor(argparse.Namespace(project_dir=str(tmp_path), json=True, strict=False))
    names = [c["name"] for c in json.loads(capsys.readouterr().out)["checks"]]
    assert "claude-cli" in names


# ---- probe script ---------------------------------------------------------


def _fake_claude(bin_dir: Path, version: str = "1.2.3") -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / "claude"
    path.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then echo "{version} (Claude Code)"; exit 0; fi\n'
        'case "$FAKE_CLAUDE_MODE" in\n'
        '  ok) echo "OK"; exit 0 ;;\n'
        '  apikey) echo "Error: ANTHROPIC_API_KEY required under --bare" >&2; exit 1 ;;\n'
        '  other) echo "network down" >&2; exit 1 ;;\n'
        '  slow) sleep 5; exit 0 ;;\n'
        "esac\n"
        "exit 3\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _run_probe(tmp_path: Path, mode: str, *extra: str, with_claude: bool = True, timeout="20"):
    bin_dir = tmp_path / "bin"
    if with_claude:
        _fake_claude(bin_dir)
    else:
        bin_dir.mkdir(exist_ok=True)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "FAKE_CLAUDE_MODE": mode,
        "PROBE_TIMEOUT_SECONDS": timeout,
    }
    return subprocess.run(
        ["bash", str(PROBE), *extra], capture_output=True, text=True, env=env, check=False
    )


def _cells(stdout: str) -> list[str]:
    return [c.strip() for c in stdout.strip().splitlines()[-1].strip("|").split("|")]


def test_probe_script_is_valid_bash():
    assert PROBE.exists(), "scripts/probe_claude_cli.sh missing"
    assert subprocess.run(["bash", "-n", str(PROBE)], check=False).returncode == 0


def test_probe_ja_row_carries_version_and_a_real_sha(tmp_path):
    proc = _run_probe(tmp_path, "ok")
    assert proc.returncode == 0, proc.stderr
    date, version, outcome, sha = _cells(proc.stdout)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", date)
    assert (version, outcome) == ("1.2.3", "ja")
    assert re.fullmatch(r"[0-9a-f]{64}", sha)


def test_probe_api_key_demand_is_nee_with_a_sha(tmp_path):
    cells = _cells(_run_probe(tmp_path, "apikey").stdout)
    assert cells[2] == "nee"
    assert re.fullmatch(r"[0-9a-f]{64}", cells[3])


def test_probe_other_failure_is_niet_gemeten(tmp_path):
    cells = _cells(_run_probe(tmp_path, "other").stdout)
    assert cells[2].startswith("niet gemeten:")


def test_probe_timeout_is_niet_gemeten(tmp_path):
    cells = _cells(_run_probe(tmp_path, "slow", timeout="1").stdout)
    assert cells[2].startswith("niet gemeten: timeout")


def test_probe_without_claude_is_niet_gemeten_without_a_hash(tmp_path):
    proc = _run_probe(tmp_path, "ok", with_claude=False)
    cells = _cells(proc.stdout)
    assert cells[1] == "unknown" and cells[2].startswith("niet gemeten:") and cells[3] == "-"


def test_probe_rejects_an_unknown_argument(tmp_path):
    proc = _run_probe(tmp_path, "ok", "--bogus")
    assert proc.returncode == 2
    assert "unknown argument" in proc.stderr


def test_probe_append_writes_a_row_the_doctor_accepts(tmp_path):
    engine = _engine_with_rows(tmp_path / "engine")
    doc = engine / "docs" / "operations" / "CLAUDE_CLI_MEASUREMENTS.md"
    proc = _run_probe(tmp_path, "ok", "--append", "--doc", str(doc))
    assert proc.returncode == 0, proc.stderr
    assert doc.read_text(encoding="utf-8").rstrip().splitlines()[-1] == proc.stdout.strip()
    assert _check(engine, "1.2.3").status == "PASS"
    assert _check(engine, "1.2.4").status == "WARN"


def test_probe_append_to_a_missing_doc_fails(tmp_path):
    proc = _run_probe(tmp_path, "ok", "--append", "--doc", str(tmp_path / "nope.md"))
    assert proc.returncode == 2


def test_probe_leaves_no_probe_tree_behind(tmp_path):
    _run_probe(tmp_path, "ok")
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("probe-claude-cli")]
