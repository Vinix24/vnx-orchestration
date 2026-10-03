"""Shared fixtures for launchd install tests (OI-1942).

Every launchd install is judged by ``scripts/lib/launchd_install_guard.py``: the engine
root must be a central install, the packaged engine or a primary checkout registered in
``~/.vnx/projects.json``. A fake engine under ``tmp_path`` is none of these, so a test
that installs from one declares what it is: a registered checkout (``register_engine``)
or a central install (``make_central_engine``). The guard is never weakened for a test.

``recording_launchctl`` puts a stub first on PATH that logs every verb, and asserts that
``shutil.which("launchctl")`` resolves to it before the caller runs anything.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path
from typing import Optional


def register_engine(home: Path, engine: Path, project_id: str) -> Path:
    """Make ``engine`` a registered primary checkout of ``project_id`` under ``home``."""
    engine.mkdir(parents=True, exist_ok=True)
    (engine / ".vnx-project-id").write_text(project_id + "\n", encoding="utf-8")
    registry_path = home / ".vnx" / "projects.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry = {"schema_version": 2, "projects": []}
    if registry_path.is_file():
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["projects"] = [
        p for p in registry["projects"] if p.get("project_id") != project_id
    ] + [{"project_id": project_id, "path": str(engine)}]
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    return engine


def make_central_engine(engine: Path, real_scripts: Optional[Path] = None) -> Path:
    """A fake engine that passes the guard as a central install."""
    engine.mkdir(parents=True, exist_ok=True)
    (engine / ".vnx-install-mode").write_text("central\n", encoding="utf-8")
    if real_scripts is not None and not (engine / "scripts").exists():
        os.symlink(real_scripts, engine / "scripts", target_is_directory=True)
    return engine


def recording_launchctl(bin_dir: Path, monkeypatch, loaded_labels: Optional[list] = None) -> Path:
    """Put a recording launchctl stub first on PATH; return its call log.

    ``launchctl list`` prints ``loaded_labels`` plus every label the stub was asked to
    load. Asserts the stub is what ``shutil.which`` finds, so a test can never fall
    through to the real binary.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    log = bin_dir / "launchctl.log"
    loaded = bin_dir / "launchctl.loaded"
    loaded.write_text("".join(f"{label}\n" for label in (loaded_labels or [])), encoding="utf-8")
    log.write_text("", encoding="utf-8")
    stub = bin_dir / "launchctl"
    stub.write_text(
        "#!/bin/bash\n"
        f'echo "$*" >> "{log}"\n'
        'case "$1" in\n'
        f'  load) basename "${{@: -1}}" .plist >> "{loaded}" ;;\n'
        f'  list) while read -r label; do printf -- "-\\t0\\t%s\\n" "$label"; done < "{loaded}" ;;\n'
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    # the launchctl backstop (vnx_paths.refuse_real_launchctl_under_test_runner) passes
    # exactly the binary named here; the stub only records, it never touches launchd
    monkeypatch.setenv("VNX_TEST_LAUNCHCTL_SHIM", str(stub))
    assert shutil.which("launchctl") == str(stub), "the recording stub must be the launchctl on PATH"
    return log
