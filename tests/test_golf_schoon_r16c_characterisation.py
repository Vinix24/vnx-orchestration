"""Characterisation tests for golf-schoon R16c (extract-method refactor).

Pins the observable behaviour of ``validate_installation``
(scripts/vnx_install.py) so the refactor can be shown behaviour-preserving.
"""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

import vnx_install as vi


def _full_install(root: Path, executable=True):
    home = root / ".vnx"
    for d in ["bin", "scripts/lib", "templates/terminals"]:
        (home / d).mkdir(parents=True, exist_ok=True)
    for f in ["bin/vnx", "scripts/vnx_init.py", "scripts/vnx_doctor.py",
              "scripts/lib/vnx_paths.py", "scripts/lib/vnx_paths.sh",
              "scripts/lib/vnx_mode.py"]:
        (home / f).write_text("x")
    for t in ["T0", "T1", "T2", "T3"]:
        (home / "templates" / "terminals" / f"{t}.md").write_text("t")
    mode = home / "bin" / "vnx"
    mode.chmod(0o755 if executable else 0o644)
    return home


def _summ(results):
    return [(r.name, r.category, r.status, r.message, r.remediation)
            for r in results]


def test_validate_no_installation(tmp_path):
    assert _summ(vi.validate_installation(tmp_path)) == [
        ("install", "install", vi.FAIL, "No VNX installation to validate",
         "Run install.sh first")]


def test_validate_full_install_without_data_dir(tmp_path):
    _full_install(tmp_path)
    res = vi.validate_installation(tmp_path)
    names = [r.name for r in res]
    assert names == [
        "dir-bin", "dir-scripts", "dir-scripts/lib", "dir-templates",
        "dir-templates/terminals",
        "file-bin/vnx", "file-scripts/vnx_init.py",
        "file-scripts/vnx_doctor.py", "file-scripts/lib/vnx_paths.py",
        "file-scripts/lib/vnx_paths.sh", "file-scripts/lib/vnx_mode.py",
        "executable", "template-T0", "template-T1", "template-T2",
        "template-T3", "runtime-data",
    ]
    assert [r.status for r in res[:-1]] == [vi.PASS] * 16
    assert res[0].message == "CLI entrypoint: bin"
    assert res[5].message == "CLI entrypoint script"
    assert res[11].message == "bin/vnx is executable"
    assert res[12].message == "Terminal template: T0"
    last = res[-1]
    assert (last.status, last.message, last.remediation) == (
        vi.WARN, "No .vnx-data/ (not yet initialized)",
        "Run: vnx init (or vnx setup)")


def test_validate_missing_pieces_fail(tmp_path):
    home = _full_install(tmp_path, executable=False)
    (home / "templates" / "terminals" / "T2.md").unlink()
    (home / "scripts" / "vnx_doctor.py").unlink()
    (home / "scripts" / "lib" / "vnx_paths.sh").unlink()
    (home / "scripts" / "lib" / "vnx_paths.py").unlink()
    res = {r.name: r for r in vi.validate_installation(tmp_path)}
    assert res["executable"].status == vi.FAIL
    assert res["executable"].message == "bin/vnx exists but is not executable"
    assert res["executable"].remediation == f"Run: chmod +x {home / 'bin' / 'vnx'}"
    assert res["template-T2"].message == "Missing template: templates/terminals/T2.md"
    assert res["template-T2"].remediation == "Re-run install.sh to repair"
    assert res["file-scripts/vnx_doctor.py"].message == (
        "Missing: scripts/vnx_doctor.py (Python doctor)")
    assert res["file-scripts/lib/vnx_paths.sh"].status == vi.FAIL
    assert res["template-T1"].status == vi.PASS


def test_validate_missing_dir_fails(tmp_path):
    home = _full_install(tmp_path)
    for p in (home / "templates" / "terminals").iterdir():
        p.unlink()
    (home / "templates" / "terminals").rmdir()
    res = {r.name: r for r in vi.validate_installation(tmp_path)}
    assert res["dir-templates/terminals"].status == vi.FAIL
    assert res["dir-templates/terminals"].message == (
        "Missing: templates/terminals (Terminal CLAUDE.md templates)")
    assert res["template-T0"].status == vi.FAIL


def test_validate_mode_states(tmp_path):
    _full_install(tmp_path)
    data = tmp_path / ".vnx-data"
    data.mkdir()
    res = vi.validate_installation(tmp_path)
    assert [r.name for r in res][-2:] == ["runtime-data", "mode"]
    assert res[-2].status == vi.PASS
    assert res[-2].message == f"Runtime data: {data}"
    assert (res[-1].status, res[-1].message) == (
        vi.WARN, "No mode.json (run 'vnx init --starter' or 'vnx init --operator')")
    (data / "mode.json").write_text('{"mode": "operator"}')
    res = vi.validate_installation(tmp_path)
    assert (res[-1].status, res[-1].message) == (vi.PASS, "Mode: operator")
    (data / "mode.json").write_text("{}")
    assert vi.validate_installation(tmp_path)[-1].message == "Mode: unknown"
    (data / "mode.json").write_text("{broken")
    res = vi.validate_installation(tmp_path)
    assert (res[-1].status, res[-1].message) == (
        vi.WARN, "mode.json exists but is unreadable")


def test_validate_legacy_layout(tmp_path):
    home = tmp_path / ".claude" / "vnx-system"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "vnx").write_text("x")
    res = vi.validate_installation(tmp_path)
    assert res[0].name == "dir-bin" and res[0].status == vi.PASS
    assert {r.name: r.status for r in res}["dir-scripts"] == vi.FAIL
