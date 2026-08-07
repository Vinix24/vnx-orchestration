#!/usr/bin/env python3
"""tests/test_build_t0_state_hook_install.py — OI-1073: ship the state builder
hook as an install artefact.

The hook (scripts/hooks/build_t0_state_hook.sh) must work from a CONSUMER
install, where there is no repo-relative ``scripts/`` tree. It resolves the
builder through the installed engine (the tree this hook lives in), surfaces
a failed build visibly on the hook's own stderr, and never blocks a session
(exit 0). These tests build a throwaway fake engine root and invoke the real
hook as a subprocess — they do NOT reimplement the hook logic.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "scripts" / "hooks" / "build_t0_state_hook.sh"

# The hook resolves the central state dir via vnx_paths.ensure_env(), which
# honors VNX_DATA_DIR_EXPLICIT + VNX_DATA_DIR. We pin those at a tmp tree so
# the hook never touches the live ~/.vnx-data store.
_VNX_ENV_KEYS = (
    "VNX_HOME",
    "VNX_PROJECT_ROOT",
    "PROJECT_ROOT",
    "VNX_CANONICAL_ROOT",
    "VNX_DATA_DIR",
    "VNX_DATA_DIR_EXPLICIT",
    "VNX_STATE_DIR",
    "VNX_DISPATCH_DIR",
    "VNX_LOGS_DIR",
    "VNX_PROJECT_ID",
)


def _clean_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _VNX_ENV_KEYS}
    if extra:
        env.update(extra)
    return env


def _make_fake_engine(
    tmp_path: Path,
    *,
    builder_source: str,
) -> Path:
    """Build a fake installed-engine tree the hook can run from.

    The engine ships the hook at <engine>/scripts/hooks/ and the builder at
    <engine>/scripts/build_t0_state.py — exactly the layout a wheel installs
    (and a dev checkout uses). ``builder_source`` is the body of the builder
    script, so a test can make it succeed or fail.
    """
    engine = tmp_path / "engine"
    (engine / "scripts" / "hooks").mkdir(parents=True)
    (engine / "scripts" / "lib").mkdir(parents=True)
    shutil.copy(HOOK, engine / "scripts" / "hooks" / "build_t0_state_hook.sh")
    os.chmod(engine / "scripts" / "hooks" / "build_t0_state_hook.sh", 0o755)
    (engine / "scripts" / "build_t0_state.py").write_text(builder_source, encoding="utf-8")
    # Minimal vnx_paths stub: ensure_env() returning the pinned dirs, so the
    # hook resolves the central store without the full engine on sys.path.
    (engine / "scripts" / "lib" / "vnx_paths.py").write_text(
        "import os\n"
        "def ensure_env():\n"
        "    d = os.environ\n"
        "    state = d.get('VNX_STATE_DIR') or os.path.join(d['VNX_DATA_DIR'], 'state')\n"
        "    logs = d.get('VNX_LOGS_DIR') or os.path.join(d['VNX_DATA_DIR'], 'logs')\n"
        "    return {'VNX_STATE_DIR': state, 'VNX_LOGS_DIR': logs}\n",
        encoding="utf-8",
    )
    return engine


def _run_hook(engine: Path, data_dir: Path) -> subprocess.CompletedProcess:
    env = _clean_env(
        {
            "VNX_DATA_DIR_EXPLICIT": "1",
            "VNX_DATA_DIR": str(data_dir),
        }
    )
    return subprocess.run(
        ["bash", str(engine / "scripts" / "hooks" / "build_t0_state_hook.sh")],
        capture_output=True,
        text=True,
        env=env,
    )


# ── the builder is resolved through the installed engine, not scripts/ ────────

def test_hook_resolves_builder_via_engine_not_repo_relative(tmp_path: Path) -> None:
    """The hook invokes <engine>/scripts/build_t0_state.py via BASH_SOURCE,
    never a $PROJECT_ROOT/scripts path. A consumer (no scripts/ next to its
    project root) must still run the builder. Assert on the resolved command
    the hook actually executes, not on a log line."""
    called: list[str] = []

    builder = (
        "#!/usr/bin/env python3\n"
        "import sys, os, json\n"
        "out = sys.argv[sys.argv.index('--output') + 1]\n"
        "os.makedirs(os.path.dirname(out), exist_ok=True)\n"
        "# record the builder path the hook invoked so we can assert on it\n"
        "with open(os.path.join(os.path.dirname(out), 'invoked_builder.txt'), 'w') as f:\n"
        "    f.write(sys.argv[0])\n"
        "with open(out, 'w') as f:\n"
        "    json.dump({'generated_at': '2026-08-07T00:00:00+00:00', 'schema_version': '2.2'}, f)\n"
    )
    engine = _make_fake_engine(tmp_path, builder_source=builder)
    data_dir = tmp_path / "data"

    r = _run_hook(engine, data_dir)
    assert r.returncode == 0, r.stdout + r.stderr

    invoked = (data_dir / "state" / "invoked_builder.txt").read_text(encoding="utf-8")
    # The hook MUST have invoked the builder under the ENGINE tree, not some
    # repo-relative scripts/ path. This is the resolved-command assertion.
    assert invoked == str(engine / "scripts" / "build_t0_state.py"), (
        f"hook invoked builder from {invoked!r}, expected the engine path"
    )
    assert (engine / "scripts" / "build_t0_state.py").exists()


def test_hook_runs_in_consumer_layout_without_scripts_dir(tmp_path: Path) -> None:
    """Consumer-shaped layout: the CWD/project has NO scripts/ directory at
    all (a pip-installed consumer). The hook still resolves and runs the
    builder through the installed engine, independent of CWD."""
    builder = (
        "#!/usr/bin/env python3\n"
        "import sys, os, json\n"
        "out = sys.argv[sys.argv.index('--output') + 1]\n"
        "os.makedirs(os.path.dirname(out), exist_ok=True)\n"
        "with open(out, 'w') as f:\n"
        "    json.dump({'generated_at': '2026-08-07T00:00:00+00:00'}, f)\n"
    )
    engine = _make_fake_engine(tmp_path, builder_source=builder)

    # A consumer project dir with no scripts/ — run the hook from inside it.
    consumer = tmp_path / "consumer-project"
    consumer.mkdir()
    (consumer / "README.md").write_text("a consumer", encoding="utf-8")
    data_dir = tmp_path / "data"
    env = _clean_env({"VNX_DATA_DIR_EXPLICIT": "1", "VNX_DATA_DIR": str(data_dir)})

    r = subprocess.run(
        ["bash", str(engine / "scripts" / "hooks" / "build_t0_state_hook.sh")],
        capture_output=True, text=True, env=env, cwd=str(consumer),
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert (data_dir / "state" / "t0_state.json").is_file(), (
        "consumer with no scripts/ must still get a built t0_state.json"
    )
    # The consumer project must NOT have sprouted a scripts/ tree.
    assert not (consumer / "scripts").exists()


# ── a failing build is visible AND non-blocking ──────────────────────────────

def test_failing_build_is_visible_and_does_not_block(tmp_path: Path) -> None:
    """A deliberately broken build produces a VISIBLE failure signal on the
    hook's own stderr, and the hook still exits 0 so the session is not
    blocked."""
    builder = (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stderr.write('cannot import coordination_db: missing module\\n')\n"
        "raise SystemExit(3)\n"
    )
    engine = _make_fake_engine(tmp_path, builder_source=builder)
    data_dir = tmp_path / "data"

    r = _run_hook(engine, data_dir)
    # Non-blocking: the hook MUST exit 0 even when the builder fails.
    assert r.returncode == 0, "a broken state file must not block the session"
    # Visible: the failure surfaces on the hook's own stderr (not only a log).
    assert "build_t0_state_hook FAILED" in r.stderr, (
        f"failed build must be visible on stderr, got stderr={r.stderr!r}"
    )
    assert "rc=3" in r.stderr
    # The real cause line is carried to the session output too.
    assert "missing module" in r.stderr
    # No state file was written by a failed build.
    assert not (data_dir / "state" / "t0_state.json").exists()
    # The full traceback still lands in the central log for diagnosis.
    err_log = data_dir / "logs" / "build_t0_state.err"
    assert err_log.is_file(), "full builder stderr must still go to the central log"
    assert "missing module" in err_log.read_text(encoding="utf-8")


def test_failing_build_visible_when_builder_missing(tmp_path: Path) -> None:
    """If the engine tree is somehow broken (no builder under it), the hook
    reports it visibly instead of silently doing nothing."""
    builder = "# placeholder\n"
    engine = _make_fake_engine(tmp_path, builder_source=builder)
    # Remove the builder to simulate a broken engine install.
    (engine / "scripts" / "build_t0_state.py").unlink()
    data_dir = tmp_path / "data"

    r = _run_hook(engine, data_dir)
    assert r.returncode == 0, "still non-blocking"
    assert "builder not found under engine" in r.stderr
    assert "not refreshed" in r.stderr


# ── a succeeding build writes the file and stamps the build timestamp ────────

def test_succeeding_build_writes_file_and_stamps_timestamp(tmp_path: Path) -> None:
    """A succeeding build writes t0_state.json carrying its own build
    timestamp (generated_at), so staleness is self-evident (OI-1073 defect 3)."""
    stamp = "2026-08-07T09:42:11+00:00"
    builder = (
        "#!/usr/bin/env python3\n"
        "import sys, os, json\n"
        "out = sys.argv[sys.argv.index('--output') + 1]\n"
        "os.makedirs(os.path.dirname(out), exist_ok=True)\n"
        "with open(out, 'w') as f:\n"
        f"    json.dump({{'generated_at': {stamp!r}, 'schema_version': '2.2'}}, f)\n"
    )
    engine = _make_fake_engine(tmp_path, builder_source=builder)
    data_dir = tmp_path / "data"

    r = _run_hook(engine, data_dir)
    assert r.returncode == 0, r.stdout + r.stderr
    state_path = data_dir / "state" / "t0_state.json"
    assert state_path.is_file(), "succeeding build must write t0_state.json"
    import json as _json
    payload = _json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["generated_at"] == stamp, "payload must carry its build timestamp"
    # No failure signal on a healthy build (the hook does not noise it up).
    assert "FAILED" not in r.stderr
    assert r.stderr == ""


def test_describe_freshness_reports_age_for_written_state(tmp_path: Path) -> None:
    """The reader-facing helper turns the stamped generated_at into a
    self-evident age string, including the never-built case."""
    sys.path.insert(0, str(REPO / "scripts"))
    sys.path.insert(0, str(REPO / "scripts" / "lib"))
    import build_t0_state as bts  # noqa: E402
    from datetime import datetime, timezone, timedelta  # noqa: E402

    now = datetime(2026, 8, 7, 10, 0, 0, tzinfo=timezone.utc)
    # never built
    assert bts.describe_freshness(None, now=now) == "never built"
    assert bts.describe_freshness({}, now=now) == "never built"
    # built 5 minutes ago
    ts = (now - timedelta(minutes=5)).isoformat()
    assert bts.describe_freshness({"generated_at": ts}, now=now) == "built 5 minutes ago"
    # built 3 days ago
    ts2 = (now - timedelta(days=3)).isoformat()
    assert bts.describe_freshness({"generated_at": ts2}, now=now) == "built 3 days ago"
    # index.json shape carries the timestamp under "timestamp"
    assert bts.describe_freshness({"timestamp": ts}, now=now) == "built 5 minutes ago"
    # unparseable stamp -> age unknown, not a crash
    assert bts.describe_freshness({"generated_at": "garbage"}, now=now) == "built (age unknown)"
    # file reader: absent file -> never built
    assert bts.state_freshness_for_file(tmp_path / "absent.json", now=now) == "never built"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
