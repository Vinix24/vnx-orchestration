#!/usr/bin/env python3
"""Regression: sourcing vnx_paths.sh must not change the caller's strict mode.

Base bug: the resolver snapshotted its shell options with
``__VNX_PATHS_SHELLOPTS="$(set +o)"``. Bash clears errexit inside a command
substitution, so that snapshot always read "errexit off" and the trailing
restore silently turned ``set -e`` off in the caller (nounset and pipefail
survive a subshell, so only ``-e`` was lost).

These tests run the resolver under the system bash (3.2 on macOS) and prove that
errexit/nounset/pipefail come out of the source exactly as they went in, and that
errexit still *functions* afterwards. The option report is emitted from the
current shell (never a command substitution) so it observes the same state the
caller sees.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
RESOLVER = REPO_ROOT / "scripts" / "lib" / "vnx_paths.sh"

# Test the system bash (3.2 on macOS) and any newer bash on PATH, so the
# no-command-substitution capture is proven on both grammar generations.
_BASHES = []
for _candidate in ("/bin/bash", shutil.which("bash")):
    if _candidate and _candidate not in _BASHES and Path(_candidate).exists():
        _BASHES.append(_candidate)


def _isolated_resolver(tmp_path: Path) -> Path:
    """Copy the resolver into a throwaway ``lib/`` tree so sourcing it touches
    nothing else (no repo git state, no install-mode marker, no live store)."""
    lib = tmp_path / "vnx" / "lib"
    lib.mkdir(parents=True, exist_ok=True)
    resolver = lib / "vnx_paths.sh"
    resolver.write_text(RESOLVER.read_text())
    return resolver


def _run(bash_bin: str, tmp_path: Path, body: str) -> subprocess.CompletedProcess:
    resolver = _isolated_resolver(tmp_path)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TERM": os.environ.get("TERM", "xterm-256color"),
    }
    script = f'''
report() {{
  case "$-" in *e*) e=on ;; *) e=off ;; esac
  if [[ -o nounset ]]; then u=on; else u=off; fi
  if [[ -o pipefail ]]; then p=on; else p=off; fi
  printf '%s/%s/%s' "$e" "$u" "$p"
}}
{body}
'''
    return subprocess.run(
        [bash_bin, "-c", script, "bash", str(resolver)],
        cwd=str(tmp_path),
        env=env,
        text=True,
        capture_output=True,
    )


@pytest.mark.parametrize("bash_bin", _BASHES)
def test_errexit_survives_source_when_caller_had_it(bash_bin: str, tmp_path: Path):
    body = '''
set -euo pipefail
printf 'before='
report
source "$1"
printf '\\nafter='
report
printf '\\n'
'''
    result = _run(bash_bin, tmp_path, body)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "before=on/on/on\nafter=on/on/on"


@pytest.mark.parametrize("bash_bin", _BASHES)
def test_strict_mode_is_not_switched_on_when_caller_had_it_off(bash_bin: str, tmp_path: Path):
    body = '''
printf 'before='
report
source "$1"
printf '\\nafter='
report
printf '\\n'
'''
    result = _run(bash_bin, tmp_path, body)
    assert result.returncode == 0, result.stderr
    # bash -c default: errexit/nounset/pipefail all off. Sourcing must not enable them.
    assert result.stdout.strip() == "before=off/off/off\nafter=off/off/off"


@pytest.mark.parametrize("bash_bin", _BASHES)
def test_nounset_and_pipefail_preserved_mixed(bash_bin: str, tmp_path: Path):
    body = '''
set -u
set -o pipefail
printf 'before='
report
source "$1"
printf '\\nafter='
report
printf '\\n'
'''
    result = _run(bash_bin, tmp_path, body)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "before=off/on/on\nafter=off/on/on"


@pytest.mark.parametrize("bash_bin", _BASHES)
def test_errexit_is_functional_after_source(bash_bin: str, tmp_path: Path):
    # A failing command after the source must abort the script, not fall through.
    aborts = _run(
        bash_bin,
        tmp_path,
        'set -euo pipefail\nsource "$1"\nfalse\necho SHOULD_NOT_PRINT\n',
    )
    assert aborts.returncode != 0
    assert "SHOULD_NOT_PRINT" not in aborts.stdout

    # And a caller that never asked for errexit keeps running past a failure.
    continues = _run(
        bash_bin,
        tmp_path,
        'source "$1"\nfalse\necho CALLER_STILL_RUNNING\n',
    )
    assert continues.returncode == 0, continues.stderr
    assert "CALLER_STILL_RUNNING" in continues.stdout
