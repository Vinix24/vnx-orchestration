"""Tests for scripts/ci_lint_patterns.py lint gate."""

import io
import sys
from pathlib import Path

# Allow importing the script directly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ci_lint_patterns as lint


def _write(tmp_path: Path, name: str, content: str) -> str:
    f = tmp_path / name
    f.write_text(content)
    return str(f)


# --- Pattern A: silent exception ---

def test_silent_except_detected(tmp_path):
    path = _write(tmp_path, "bad.py", "try:\n    pass\nexcept Exception:\n    pass\n")
    findings = lint.scan_file(path)
    assert any(f.pattern == "A" for f in findings), f"Expected Pattern A finding, got: {findings}"


def test_silent_except_with_noqa_ignored(tmp_path):
    path = _write(
        tmp_path,
        "ok.py",
        "try:\n    pass\nexcept Exception:  # noqa: vnx-silent-except\n    pass\n",
    )
    findings = lint.scan_file(path)
    assert not any(f.pattern == "A" for f in findings), f"Expected no Pattern A finding, got: {findings}"


def test_bare_except_detected(tmp_path):
    path = _write(tmp_path, "bare.py", "try:\n    x()\nexcept:\n    pass\n")
    findings = lint.scan_file(path)
    assert any(f.pattern == "A" for f in findings), f"Expected Pattern A for bare except, got: {findings}"


# --- Pattern B: non-atomic state write ---

def test_atomic_write_violation_detected(tmp_path):
    path = _write(
        tmp_path,
        "write.py",
        'with open("foo/state/x.json", "w") as f:\n    f.write("data")\n',
    )
    findings = lint.scan_file(path)
    assert any(f.pattern == "B" for f in findings), f"Expected Pattern B finding, got: {findings}"


def test_atomic_write_with_noqa_ignored(tmp_path):
    path = _write(
        tmp_path,
        "ok_write.py",
        'with open("foo/state/x.json", "w") as f:  # noqa: vnx-atomic-write\n    f.write("data")\n',
    )
    findings = lint.scan_file(path)
    assert not any(f.pattern == "B" for f in findings), f"Expected no Pattern B finding, got: {findings}"


# --- Clean code ---

def test_clean_code_no_findings(tmp_path):
    path = _write(tmp_path, "clean.py", 'print("ok")\n')
    findings = lint.scan_file(path)
    assert findings == [], f"Expected no findings, got: {findings}"


# --- main() exit codes for default (full-tree) mode ---

def test_main_exit_1_on_findings(tmp_path, monkeypatch):
    """main() returns 1 when findings exist."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "bad.py").write_text("try:\n    pass\nexcept Exception:\n    pass\n")

    def patched_collect(root):
        return [str(scripts_dir / "bad.py")]

    monkeypatch.setattr(lint, "collect_files_from_dirs", patched_collect)
    result = lint.main([])
    assert result == 1


def test_main_exit_0_for_no_findings(monkeypatch):
    """main() returns 0 when no findings."""
    monkeypatch.setattr(lint, "collect_files_from_dirs", lambda root: [])
    result = lint.main([])
    assert result == 0


# --- --scan-stdin mode (concatenated added-line blob) ---

def test_scan_stdin_silent_except_detected(monkeypatch):
    """--scan-stdin: catches silent-except added in a diff blob."""
    blob = "try:\n    pass\nexcept Exception:\n    pass\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(blob))
    result = lint.main(["--scan-stdin"])
    assert result == 1


def test_scan_stdin_atomic_write_detected(monkeypatch):
    """--scan-stdin: catches non-atomic state write in a diff blob."""
    blob = 'with open("foo/state/x.json", "w") as f:\n    f.write("data")\n'
    monkeypatch.setattr(sys, "stdin", io.StringIO(blob))
    result = lint.main(["--scan-stdin"])
    assert result == 1


def test_scan_stdin_clean_no_findings(monkeypatch):
    """--scan-stdin: returns 0 on clean added-line blob."""
    blob = 'def hello():\n    return 42\n'
    monkeypatch.setattr(sys, "stdin", io.StringIO(blob))
    result = lint.main(["--scan-stdin"])
    assert result == 0


def test_scan_stdin_empty_input(monkeypatch):
    """--scan-stdin: returns 0 on empty stdin (no added lines)."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    result = lint.main(["--scan-stdin"])
    assert result == 0


def test_scan_stdin_noqa_in_blob_ignored(monkeypatch):
    """--scan-stdin: noqa comment on same line as except suppresses Pattern A."""
    blob = "try:\n    pass\nexcept Exception:  # noqa: vnx-silent-except\n    pass\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(blob))
    result = lint.main(["--scan-stdin"])
    assert result == 0


# --- --files-from-stdin mode (full file scan via stdin paths) ---

def test_files_from_stdin_silent_except_detected(tmp_path, monkeypatch):
    """--files-from-stdin: catches silent-except in a file fed via stdin."""
    bad = _write(tmp_path, "bad.py", "try:\n    pass\nexcept Exception:\n    pass\n")
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{bad}\n"))
    result = lint.main(["--files-from-stdin"])
    assert result == 1


def test_files_from_stdin_atomic_write_detected(tmp_path, monkeypatch):
    """--files-from-stdin: catches non-atomic state write in a file fed via stdin."""
    bad = _write(
        tmp_path,
        "write.py",
        'with open("foo/state/x.json", "w") as f:\n    f.write("data")\n',
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{bad}\n"))
    result = lint.main(["--files-from-stdin"])
    assert result == 1


def test_files_from_stdin_clean_no_findings(tmp_path, monkeypatch):
    """--files-from-stdin: returns 0 on clean files."""
    clean = _write(tmp_path, "clean.py", 'print("ok")\n')
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{clean}\n"))
    result = lint.main(["--files-from-stdin"])
    assert result == 0


def test_files_from_stdin_skips_blanks_and_missing(tmp_path, monkeypatch):
    """--files-from-stdin: blank lines ignored; missing files silently skipped."""
    clean = _write(tmp_path, "clean.py", 'print("ok")\n')
    stdin_text = f"\n{clean}\n\n/nonexistent/path/file.py\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    result = lint.main(["--files-from-stdin"])
    assert result == 0


# --- pre10-slop-sweep anti-revert (2026-06-25) ---------------------------------
# These guard the slop-sweep: the full repo scan stays clean, the field-tests
# seed-exclude stays narrow, the plain-marker convention works, and the 4
# rollback sites keep re-raising (the marker must never hide a real swallow).

import ast

_REPO = Path(__file__).resolve().parent.parent


def test_full_scan_repo_clean():
    # The real default full-scan (scripts/ + dashboard/) must be finding-free.
    # Fails the moment anyone reintroduces a silent-except or drops a marker.
    assert lint.main([]) == 0


def test_field_tests_seeds_excluded_runners_kept():
    files = lint.collect_files_from_dirs(_REPO)
    seed_under_ft = [
        f for f in files
        if "seed" in Path(f).parts and "field-tests" in Path(f).parts
    ]
    assert not seed_under_ft, f"field-tests seeds must be excluded, got: {seed_under_ft[:3]}"
    # ...but the real harness code under field-tests stays linted.
    runners = [f for f in files if "field-tests" in Path(f).parts and "runners" in Path(f).parts]
    assert runners, "field-tests/runners/ harness scripts must remain in scope"


def test_seed_outside_field_tests_still_scanned(tmp_path, monkeypatch):
    # A production `seed/` dir NOT under field-tests must NOT be excluded.
    seed = tmp_path / "scripts" / "seed"
    seed.mkdir(parents=True)
    (seed / "mod.py").write_text("x = 1\n")
    monkeypatch.setattr(lint, "_SCAN_DIRS", ("scripts",))
    files = lint.collect_files_from_dirs(tmp_path)
    assert str(seed / "mod.py") in files, "non-field-tests seed/ must stay scanned"


def test_plain_marker_suppresses(tmp_path):
    # The Ruff-safe plain marker (no `# noqa:` prefix) suppresses Pattern A.
    path = _write(
        tmp_path,
        "ok_plain.py",
        "try:\n    pass\nexcept Exception:  # vnx-silent-except: justified\n    pass\n",
    )
    findings = lint.scan_file(path)
    assert not any(f.pattern == "A" for f in findings), f"plain marker must suppress, got: {findings}"


def _outer_handlers_of_passonly_swallows(filepath: Path):
    """AST/block-scoped: every pass-only except nested inside another handler
    (the rollback-best-effort pattern) -> return its ENCLOSING handler."""
    tree = ast.parse(filepath.read_text())
    parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    outers = []
    for h in ast.walk(tree):
        if isinstance(h, ast.ExceptHandler) and len(h.body) == 1 and isinstance(h.body[0], ast.Pass):
            p = parents.get(h)
            while p is not None and not isinstance(p, ast.ExceptHandler):
                p = parents.get(p)
            if p is not None:
                outers.append(p)
    return outers


def test_rollback_sites_still_reraise():
    # AST, not line-proximity: each marked rollback-swallow lives inside an outer
    # handler that re-raises the original error. Drop the `raise` -> this fails.
    expected = {"scripts/lib/tenant_stamping.py": 3, "scripts/migrate_future_system.py": 1}
    for rel, count in expected.items():
        outers = _outer_handlers_of_passonly_swallows(_REPO / rel)
        assert len(outers) >= count, f"{rel}: expected >= {count} rollback patterns, got {len(outers)}"
        for oh in outers:
            assert any(isinstance(n, ast.Raise) for n in oh.body), \
                f"{rel}: an outer rollback handler does not re-raise (silent swallow!)"
