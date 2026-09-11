#!/usr/bin/env python3
"""OI-1718 — a cutover names its behindness out loud.

A cutover (``_atomic_symlink_flip``) can silently activate a target that is
far behind ``main``: measured 2026-09-11, tag ``v1.6.1`` sat 42 commits behind
``origin/main`` and nothing in ``vnx update --to`` / ``vnx release publish
--set-current`` surfaced that. These tests pin the guard:

1. The behind-count is ALWAYS named — under the threshold, over it, and on
   every successful flip. A silent successful cutover is the bug.
2. Over the (configurable) threshold the flip is REFUSED; the refusal names
   the count, the target ref, and the way out.
3. The escape hatch is an explicit REASON (``--cutover-reason``), never a bare
   flag; an empty/whitespace reason is itself a refusal. A used reason is
   recorded in the central-install audit log.
4. Unmeasurable behindness (unreachable source, missing ref) is a THIRD
   outcome: reported as UNKNOWN, never silently as 0. UNKNOWN proceeds loudly
   rather than blocking — a cutover is non-destructive, and refusing every
   flip whenever the remote is unreachable would make ``update``/``rollback``
   unusable exactly when they are needed as recovery tools. Rollback is never
   refused at all: rolling back goes backwards by design.

Every measurement carries two controls: something that MUST be found (the
count in the output of a succeeding cutover) and something that must NOT be
found (a cutover path with no count, an empty reason waved through).
"""

import io
import json
import stat
import subprocess
import sys
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "scripts" / "lib") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import config_registry as cr
import vnx_cli.commands.update as update_module
from vnx_cli.commands.update import (
    CutoverRefusedError,
    DEFAULT_CUTOVER_MAX_BEHIND_COMMITS,
    _atomic_symlink_flip,
    _cutover_max_behind_commits,
    _do_rollback,
    _measure_behind_main,
    vnx_update,
)
from vnx_cli.commands.release import vnx_release_publish


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _source_repo(path: Path, tag: str = "v1.0.0", ahead: int = 5) -> Path:
    """A local git repo standing in for the canonical source (offline).

    ``tag`` sits ``ahead`` commits behind the tip of ``main`` — the exact
    shape of the OI-1718 incident (a tag cut from an older main).
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=path, check=True)
    (path / "VERSION").write_text(f"{tag.lstrip('vV')}\n", encoding="utf-8")
    subprocess.run(["git", "add", "VERSION"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    subprocess.run(["git", "tag", tag], cwd=path, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=path, check=True)
    for i in range(ahead):
        (path / f"file{i}.md").write_text(f"{i}\n", encoding="utf-8")
        subprocess.run(["git", "add", f"file{i}.md"], cwd=path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", f"ahead {i}"], cwd=path, check=True)
    return path


@pytest.fixture
def central_root(tmp_path, monkeypatch):
    """Isolated central store + audit log location."""
    root = tmp_path / "vnx-system"
    monkeypatch.setenv("VNX_HOME_ROOT", str(root))
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "vnx-data"))
    return root


@pytest.fixture
def audit_log(tmp_path):
    return tmp_path / "events" / "central_install.ndjson"


def _audit_events(audit_log: Path):
    if not audit_log.exists():
        return []
    return [json.loads(line) for line in audit_log.read_text().strip().splitlines()]


def _flip_target(root: Path, name: str) -> Path:
    target = root / "versions" / name
    target.mkdir(parents=True)
    return target


def _flip(root, target_dir, audit_log, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        _atomic_symlink_flip(
            root, target_dir, dry_run=False, audit_log=audit_log, **kwargs
        )
    return out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# _measure_behind_main — the measurement itself
# ---------------------------------------------------------------------------

def test_measure_behind_main_counts_commits(tmp_path):
    src = _source_repo(tmp_path / "src", ahead=5)
    assert _measure_behind_main(str(src), "v1.0.0") == 5


def test_measure_behind_main_zero_for_tip_tag(tmp_path):
    src = _source_repo(tmp_path / "src", ahead=0)
    assert _measure_behind_main(str(src), "v1.0.0") == 0


def test_measure_behind_main_unknown_for_missing_ref(tmp_path):
    src = _source_repo(tmp_path / "src", ahead=2)
    assert _measure_behind_main(str(src), "v9.9.9") is None


def test_measure_behind_main_unknown_for_unreachable_source(tmp_path):
    missing = tmp_path / "no-such-repo"
    assert _measure_behind_main(str(missing), "v1.0.0") is None


def test_measure_behind_main_unknown_when_source_has_no_main(tmp_path):
    """A source without a ``main`` branch cannot define behindness — UNKNOWN,
    never 0."""
    src = tmp_path / "src"
    src.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "trunk"], cwd=src, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=src, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=src, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=src, check=True)
    subprocess.run(["git", "tag", "v1.0.0"], cwd=src, check=True)
    assert _measure_behind_main(str(src), "v1.0.0") is None


# ---------------------------------------------------------------------------
# Threshold configuration
# ---------------------------------------------------------------------------

def test_threshold_default_when_nothing_set(monkeypatch):
    monkeypatch.delenv("VNX_CUTOVER_MAX_BEHIND_COMMITS", raising=False)
    monkeypatch.delenv("VNX_OVERRIDE_CUTOVER_MAX_BEHIND_COMMITS", raising=False)
    assert _cutover_max_behind_commits() == DEFAULT_CUTOVER_MAX_BEHIND_COMMITS


def test_threshold_env_override(monkeypatch):
    monkeypatch.setenv("VNX_CUTOVER_MAX_BEHIND_COMMITS", "2")
    assert _cutover_max_behind_commits() == 2


def test_threshold_invalid_value_falls_back_to_default_loudly(monkeypatch, capsys):
    monkeypatch.setenv("VNX_CUTOVER_MAX_BEHIND_COMMITS", "banana")
    assert _cutover_max_behind_commits() == DEFAULT_CUTOVER_MAX_BEHIND_COMMITS
    assert "VNX_CUTOVER_MAX_BEHIND_COMMITS" in capsys.readouterr().err


def test_registry_entry_registered_with_description():
    """The threshold lives in the config registry (requirement 4), with a
    written-out description — not a bare number in code. The registry default
    mirrors the code fallback, the registry's own contract."""
    entry = cr.CONFIG_REGISTRY["VNX_CUTOVER_MAX_BEHIND_COMMITS"]
    assert entry.default == str(DEFAULT_CUTOVER_MAX_BEHIND_COMMITS)
    assert entry.type == "string"
    assert len(entry.description) > 40
    assert entry.subsystem
    assert entry.status in cr.ALLOWED_STATUSES


# ---------------------------------------------------------------------------
# The guard on the flip: under threshold proceeds and NAMES the count
# ---------------------------------------------------------------------------

def test_under_threshold_proceeds_and_names_count(
    tmp_path, central_root, audit_log, monkeypatch
):
    src = _source_repo(tmp_path / "src", ahead=3)
    monkeypatch.setenv("VNX_CUTOVER_MAX_BEHIND_COMMITS", "20")
    target_dir = _flip_target(central_root, "v1.0.0")

    out, _err = _flip(
        central_root, target_dir, audit_log, behind_source=str(src)
    )

    # Positive control: the count is named even though the cutover succeeds.
    assert "3 commits behind" in out
    assert "v1.0.0" in out
    assert (central_root / "current").resolve() == target_dir.resolve()
    assert "Activated:" in out
    events = _audit_events(audit_log)
    checks = [e for e in events if e["event_type"] == "central_install_cutover_behind_check"]
    assert len(checks) == 1
    assert checks[0]["behind"] == 3
    assert checks[0]["outcome"] == "proceed"
    assert checks[0]["threshold"] == 20


def test_over_threshold_refused_names_count_ref_and_way_out(
    tmp_path, central_root, audit_log, monkeypatch
):
    src = _source_repo(tmp_path / "src", ahead=5)
    monkeypatch.setenv("VNX_CUTOVER_MAX_BEHIND_COMMITS", "2")
    target_dir = _flip_target(central_root, "v1.0.0")

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        with pytest.raises(CutoverRefusedError) as excinfo:
            _atomic_symlink_flip(
                central_root, target_dir, dry_run=False,
                audit_log=audit_log, behind_source=str(src),
            )

    message = str(excinfo.value)
    # The refusal names the count, the target ref, and the way out.
    assert "5" in message
    assert "v1.0.0" in message
    assert "--cutover-reason" in message
    # Negative control: the flip never happened — no symlink, no flip events.
    assert not (central_root / "current").exists()
    events = _audit_events(audit_log)
    assert not [e for e in events if e["event_type"] == "central_install_update"]
    checks = [e for e in events if e["event_type"] == "central_install_cutover_behind_check"]
    assert len(checks) == 1
    assert checks[0]["outcome"] == "refused"
    assert checks[0]["behind"] == 5


def test_over_threshold_with_reason_proceeds_and_records_reason(
    tmp_path, central_root, audit_log, monkeypatch
):
    src = _source_repo(tmp_path / "src", ahead=5)
    monkeypatch.setenv("VNX_CUTOVER_MAX_BEHIND_COMMITS", "2")
    target_dir = _flip_target(central_root, "v1.0.0")
    reason = "mission-control hotfix must ship before the next tag is cut"

    out, _err = _flip(
        central_root, target_dir, audit_log,
        behind_source=str(src), cutover_reason=reason,
    )

    assert "5 commits behind" in out
    assert reason in out
    assert (central_root / "current").resolve() == target_dir.resolve()
    checks = [
        e for e in _audit_events(audit_log)
        if e["event_type"] == "central_install_cutover_behind_check"
    ]
    assert len(checks) == 1
    assert checks[0]["outcome"] == "override"
    assert checks[0]["reason"] == reason


@pytest.mark.parametrize("empty_reason", ["", "   "])
def test_over_threshold_empty_reason_is_a_refusal(
    tmp_path, central_root, audit_log, monkeypatch, empty_reason
):
    """The escape hatch demands an explicit reason — a blank one is refused."""
    src = _source_repo(tmp_path / "src", ahead=5)
    monkeypatch.setenv("VNX_CUTOVER_MAX_BEHIND_COMMITS", "2")
    target_dir = _flip_target(central_root, "v1.0.0")

    with pytest.raises(CutoverRefusedError):
        _atomic_symlink_flip(
            central_root, target_dir, dry_run=False,
            audit_log=audit_log, behind_source=str(src),
            cutover_reason=empty_reason,
        )

    assert not (central_root / "current").exists()


# ---------------------------------------------------------------------------
# Third outcome: unmeasurable behindness is UNKNOWN, never silently 0
# ---------------------------------------------------------------------------

def test_unmeasurable_source_is_unknown_not_zero(
    tmp_path, central_root, audit_log, monkeypatch, capsys
):
    missing = tmp_path / "no-such-repo"
    target_dir = _flip_target(central_root, "v1.0.0")

    out, err = _flip(
        central_root, target_dir, audit_log, behind_source=str(missing)
    )

    combined = out + err
    assert "UNKNOWN" in combined
    # Negative control: unknown is never reported as a number, and never as 0.
    assert "0 commits behind" not in combined
    # Design decision: UNKNOWN proceeds loudly rather than blocking (a cutover
    # is non-destructive; update/rollback are recovery tools that must keep
    # working when the remote is unreachable).
    assert (central_root / "current").resolve() == target_dir.resolve()
    checks = [
        e for e in _audit_events(audit_log)
        if e["event_type"] == "central_install_cutover_behind_check"
    ]
    assert len(checks) == 1
    assert checks[0]["outcome"] == "proceed_unknown"
    assert checks[0]["behind"] is None


def test_missing_ref_is_unknown_not_zero(
    tmp_path, central_root, audit_log, monkeypatch
):
    src = _source_repo(tmp_path / "src", ahead=2)
    target_dir = _flip_target(central_root, "v9.9.9")

    out, err = _flip(
        central_root, target_dir, audit_log, behind_source=str(src)
    )

    assert "UNKNOWN" in (out + err)
    assert (central_root / "current").resolve() == target_dir.resolve()


def test_non_git_flip_target_with_no_source_is_unknown_without_network(
    tmp_path, central_root, audit_log
):
    """A flip target that is not a git checkout (test stubs, hand-made dirs)
    carries no origin to measure against — UNKNOWN, and crucially NO clone of
    the canonical remote is attempted (the existing direct-flip tests must
    stay offline)."""
    target_dir = _flip_target(central_root, "v1.0.0")

    out, err = _flip(central_root, target_dir, audit_log)

    assert "UNKNOWN" in (out + err)
    assert (central_root / "current").resolve() == target_dir.resolve()


# ---------------------------------------------------------------------------
# Rollback: measured and named, never blocked
# ---------------------------------------------------------------------------

def test_rollback_reports_count_but_never_blocks(
    tmp_path, central_root, audit_log, monkeypatch
):
    src = _source_repo(tmp_path / "src", ahead=5)
    monkeypatch.setenv("VNX_CUTOVER_MAX_BEHIND_COMMITS", "2")

    old = central_root / "versions" / "v1.0.0"
    old.mkdir(parents=True)
    # Give the rollback target an origin so the guard can measure it.
    subprocess.run(["git", "init", "-q"], cwd=old, check=True)
    _git(old, "remote", "add", "origin", str(src))
    new = central_root / "versions" / "v1.0.1"
    new.mkdir(parents=True)
    import os
    os.utime(old, (1_700_000_000, 1_700_000_000))
    os.utime(new, (1_700_000_100, 1_700_000_100))
    (central_root / "current").symlink_to(new)

    out = io.StringIO()
    with redirect_stdout(out):
        rc = _do_rollback(central_root, dry_run=False)

    assert rc == 0
    # The count is named on the rollback path too ...
    assert "5 commits behind" in out.getvalue()
    # ... but over-threshold does not refuse: rollback goes backwards by design.
    assert (central_root / "current").resolve() == old.resolve()


# ---------------------------------------------------------------------------
# edge tracks main by definition: 0 behind, no clone attempted
# ---------------------------------------------------------------------------

def test_edge_reports_zero_without_touching_source(
    tmp_path, central_root, audit_log
):
    target_dir = _flip_target(central_root, "edge")

    out, _err = _flip(
        central_root, target_dir, audit_log,
        # A nonexistent source proves no measurement clone was attempted.
        behind_source=str(tmp_path / "no-such-repo"),
    )

    assert "0 commits behind" in out
    assert (central_root / "current").resolve() == target_dir.resolve()


# ---------------------------------------------------------------------------
# No cutover path passes without the measurement being named
# ---------------------------------------------------------------------------

def test_every_successful_flip_output_names_the_measurement(
    tmp_path, central_root, audit_log, monkeypatch
):
    """Negative control for requirement 1: three different flip paths
    (measured, override, unknown) all carry the behind-check line — no silent
    cutover exists."""
    monkeypatch.setenv("VNX_CUTOVER_MAX_BEHIND_COMMITS", "2")

    # measured + over threshold + reason
    src = _source_repo(tmp_path / "src", ahead=5)
    t1 = _flip_target(central_root, "v1.0.0")
    out1, _ = _flip(
        central_root, t1, audit_log,
        behind_source=str(src), cutover_reason="deliberate pin",
    )
    assert "behind" in out1 and "5" in out1

    # measured + under threshold
    src2 = _source_repo(tmp_path / "src2", tag="v1.0.1", ahead=1)
    t2 = _flip_target(central_root, "v1.0.1")
    out2, _ = _flip(central_root, t2, audit_log, behind_source=str(src2))
    assert "1 commits behind" in out2

    # unknown
    t3 = _flip_target(central_root, "v1.0.2")
    out3, err3 = _flip(central_root, t3, audit_log)
    assert "UNKNOWN" in (out3 + err3)


# ---------------------------------------------------------------------------
# Dry-run announces the guard without measuring
# ---------------------------------------------------------------------------

def test_flip_dry_run_announces_behind_check(tmp_path, central_root):
    target_dir = central_root / "versions" / "v1.0.0"
    out = io.StringIO()
    with redirect_stdout(out):
        _atomic_symlink_flip(central_root, target_dir, dry_run=True)
    assert "[dry-run]" in out.getvalue()
    assert "behind" in out.getvalue()
    assert not (central_root / "current").exists()


def test_update_dry_run_announces_behind_check(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_HOME_ROOT", str(tmp_path))
    args = Namespace(
        to_version="v2.0.0", keep_last=3, dry_run=True,
        rollback=False, protect_pins=None, cutover_reason=None,
    )
    out = io.StringIO()
    with redirect_stdout(out):
        rc = vnx_update(args)
    assert rc == 0
    assert "behind" in out.getvalue()


# ---------------------------------------------------------------------------
# Release publish --set-current: the guard rides the publish cutover too
# ---------------------------------------------------------------------------

STUB_INSTALL_CENTRAL = """#!/usr/bin/env bash
set -euo pipefail
VERSION=""
TARGET=""
while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --target)  TARGET="$2";  shift 2 ;;
    --source)  shift 2 ;;
    --materialize-only) shift ;;
    *) echo "stub: unknown arg $1" >&2; exit 1 ;;
  esac
done
[ -n "$VERSION" ] && [ -n "$TARGET" ]
mkdir -p "${TARGET}/versions/${VERSION}"
echo "stub-materialized ${VERSION}"
"""


@pytest.fixture
def stub_install_central(tmp_path, monkeypatch):
    stub = tmp_path / "install-central.sh"
    stub.write_text(STUB_INSTALL_CENTRAL, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("VNX_INSTALL_CENTRAL_SCRIPT", str(stub))
    return stub


def _publish_args(*, tag, repo, dry_run=False, set_current=False, cutover_reason=None):
    return Namespace(
        tag=tag, repo=repo, dry_run=dry_run,
        set_current=set_current, cutover_reason=cutover_reason,
    )


def test_release_set_current_refused_over_threshold(
    tmp_path, central_root, stub_install_central, monkeypatch, capsys
):
    src = _source_repo(tmp_path / "src", tag="v1.3.1", ahead=5)
    monkeypatch.setenv("VNX_CUTOVER_MAX_BEHIND_COMMITS", "2")

    rc = vnx_release_publish(
        _publish_args(tag="v1.3.1", repo=str(src), set_current=True)
    )

    assert rc == 1
    captured = capsys.readouterr()
    assert "5" in captured.err
    assert "v1.3.1" in captured.err
    assert "--cutover-reason" in captured.err
    # The publish itself succeeded; only the cutover was refused.
    assert (central_root / "versions" / "v1.3.1").is_dir()
    assert not (central_root / "current").exists()


def test_release_set_current_with_reason_proceeds(
    tmp_path, central_root, stub_install_central, monkeypatch
):
    src = _source_repo(tmp_path / "src", tag="v1.3.1", ahead=5)
    monkeypatch.setenv("VNX_CUTOVER_MAX_BEHIND_COMMITS", "2")
    reason = "staging cutover rehearsed against the old tag on purpose"

    out = io.StringIO()
    with redirect_stdout(out):
        rc = vnx_release_publish(
            _publish_args(
                tag="v1.3.1", repo=str(src), set_current=True,
                cutover_reason=reason,
            )
        )

    assert rc == 0
    assert "5 commits behind" in out.getvalue()
    current = central_root / "current"
    assert current.is_symlink()
    assert current.resolve() == (central_root / "versions" / "v1.3.1").resolve()
    # The reason is on the audit trail.
    log = central_root.parent / "vnx-data" / "events" / "central_install.ndjson"
    events = _audit_events(log)
    checks = [e for e in events if e["event_type"] == "central_install_cutover_behind_check"]
    assert len(checks) == 1
    assert checks[0]["outcome"] == "override"
    assert checks[0]["reason"] == reason


def test_release_set_current_under_threshold_names_count(
    tmp_path, central_root, stub_install_central, monkeypatch
):
    src = _source_repo(tmp_path / "src", tag="v1.3.1", ahead=1)
    monkeypatch.delenv("VNX_CUTOVER_MAX_BEHIND_COMMITS", raising=False)

    out = io.StringIO()
    with redirect_stdout(out):
        rc = vnx_release_publish(
            _publish_args(tag="v1.3.1", repo=str(src), set_current=True)
        )

    assert rc == 0
    assert "1 commits behind" in out.getvalue()
    assert (central_root / "current").is_symlink()


def test_release_dry_run_set_current_announces_behind_check(
    tmp_path, central_root, monkeypatch
):
    src = _source_repo(tmp_path / "src", tag="v1.3.1", ahead=5)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = vnx_release_publish(
            _publish_args(tag="v1.3.1", repo=str(src), dry_run=True, set_current=True)
        )
    assert rc == 0
    assert "behind" in out.getvalue()
    assert not (central_root / "current").exists()
