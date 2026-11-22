"""The T0-state SessionStart hook skips the build in a worker session
(t0-state-een-bouw-per-store S1, OI-2951). Runs the real hook with bash in a
subprocess against a temporary engine and state dir; the builder is a stub."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "scripts" / "hooks" / "build_t0_state_hook.sh"
ACTOR_CLASS = REPO / "scripts" / "lib" / "actor_class.py"

_STRIP = (
    "VNX_HOME", "VNX_PROJECT_ROOT", "PROJECT_ROOT", "VNX_CANONICAL_ROOT",
    "VNX_DATA_DIR", "VNX_DATA_DIR_EXPLICIT", "VNX_STATE_DIR", "VNX_DISPATCH_DIR",
    "VNX_LOGS_DIR", "VNX_PROJECT_ID", "CLAUDE_ROLE", "VNX_TERMINAL",
    "VNX_TERMINAL_ID", "VNX_DISPATCH_ID", "VNX_CURRENT_DISPATCH_ID",
    "VNX_PROCESS_OWNER", "PYTHONPATH",
)

_STUB = (
    "import os, sys\n"
    "out = sys.argv[sys.argv.index('--output') + 1]\n"
    "os.makedirs(os.path.dirname(out), exist_ok=True)\n"
    "open(os.path.join(os.path.dirname(out), 'stub_ran.txt'), 'w').write('ran')\n"
)


def _engine(tmp_path: Path, actor_class_source: str | None = None) -> Path:
    engine = tmp_path / "engine"
    (engine / "scripts" / "hooks").mkdir(parents=True)
    (engine / "scripts" / "lib").mkdir(parents=True)
    shutil.copy(HOOK, engine / "scripts" / "hooks" / "build_t0_state_hook.sh")
    shutil.copy(HOOK.parent / "lib_hook_python.sh", engine / "scripts" / "hooks" / "lib_hook_python.sh")
    (engine / "scripts" / "build_t0_state.py").write_text(_STUB, encoding="utf-8")
    (engine / "scripts" / "lib" / "vnx_paths.py").write_text(
        "import os\n"
        "def ensure_env():\n"
        "    d = os.environ['VNX_DATA_DIR']\n"
        "    return {'VNX_STATE_DIR': d + '/state', 'VNX_LOGS_DIR': d + '/logs'}\n",
        encoding="utf-8",
    )
    target = engine / "scripts" / "lib" / "actor_class.py"
    if actor_class_source is None:
        shutil.copy(ACTOR_CLASS, target)
    else:
        target.write_text(actor_class_source, encoding="utf-8")
    return engine


def _run(engine: Path, data_dir: Path, extra: dict | None = None):
    env = {k: v for k, v in os.environ.items() if k not in _STRIP}
    env.update({"VNX_DATA_DIR_EXPLICIT": "1", "VNX_DATA_DIR": str(data_dir)})
    env.update(extra or {})
    return subprocess.run(
        ["bash", str(engine / "scripts" / "hooks" / "build_t0_state_hook.sh")],
        capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
    )


def _ran(data_dir: Path) -> bool:
    return (data_dir / "state" / "stub_ran.txt").exists()


def test_worker_session_skips_build(tmp_path: Path) -> None:
    data = tmp_path / "data"
    r = _run(_engine(tmp_path), data, {"VNX_CURRENT_DISPATCH_ID": "x"})
    assert r.returncode == 0, r.stderr
    assert not _ran(data)
    assert "skipped: worker session x" in r.stderr


def test_worker_session_names_fallback_dispatch_id(tmp_path: Path) -> None:
    data = tmp_path / "data"
    r = _run(_engine(tmp_path), data, {"VNX_DISPATCH_ID": "y"})
    assert r.returncode == 0, r.stderr
    assert not _ran(data)
    assert "skipped: worker session y" in r.stderr


def test_orchestrator_with_dispatch_marker_builds(tmp_path: Path) -> None:
    data = tmp_path / "data"
    r = _run(
        _engine(tmp_path), data,
        {"CLAUDE_ROLE": "orchestrator", "VNX_CURRENT_DISPATCH_ID": "x"},
    )
    assert r.returncode == 0, r.stderr
    assert _ran(data)
    assert "skipped" not in r.stderr


def test_terminal_t0_with_dispatch_marker_builds(tmp_path: Path) -> None:
    data = tmp_path / "data"
    r = _run(_engine(tmp_path), data, {"VNX_TERMINAL": "T0", "VNX_DISPATCH_ID": "x"})
    assert r.returncode == 0, r.stderr
    assert _ran(data)


def test_no_markers_builds(tmp_path: Path) -> None:
    data = tmp_path / "data"
    r = _run(_engine(tmp_path), data)
    assert r.returncode == 0, r.stderr
    assert _ran(data)
    assert "skipped" not in r.stderr


def test_classification_raising_builds(tmp_path: Path) -> None:
    data = tmp_path / "data"
    engine = _engine(tmp_path, actor_class_source="raise RuntimeError('broken')\n")
    r = _run(engine, data, {"VNX_CURRENT_DISPATCH_ID": "x"})
    assert r.returncode == 0, r.stderr
    assert _ran(data)
    assert "skipped" not in r.stderr


def test_classification_module_missing_builds(tmp_path: Path) -> None:
    data = tmp_path / "data"
    engine = _engine(tmp_path)
    (engine / "scripts" / "lib" / "actor_class.py").unlink()
    r = _run(engine, data, {"VNX_CURRENT_DISPATCH_ID": "x"})
    assert r.returncode == 0, r.stderr
    assert _ran(data)


def test_classification_unknown_output_builds(tmp_path: Path) -> None:
    data = tmp_path / "data"
    engine = _engine(
        tmp_path, actor_class_source="def derive():\n    return 'martian'\n"
    )
    r = _run(engine, data, {"VNX_CURRENT_DISPATCH_ID": "x"})
    assert r.returncode == 0, r.stderr
    assert _ran(data)
