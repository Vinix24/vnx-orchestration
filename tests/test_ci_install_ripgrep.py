"""scripts/ci/install_ripgrep.sh: patient release retry, bounded apt fallback."""
from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci" / "install_ripgrep.sh"
OLD_SHA = "7dfbeb45d9768866843f1c6e3926cf523036a5c0"

CURL_STUB = """#!/bin/bash
# Emulates curl --retry N: N+1 tries in total, each one counted; exit 22 while failing.
retries=0; out=""
while [ $# -gt 0 ]; do
  case "$1" in --retry) retries="$2";; -o) out="$2";; esac
  shift
done
for _ in $(seq 0 "$retries"); do
  n=$(cat "$STUB_STATE/curl_n" 2>/dev/null || echo 0)
  n=$((n+1)); echo "$n" > "$STUB_STATE/curl_n"
  if [ "$n" -gt "${CURL_FAILS:-0}" ]; then
    [ -n "$out" ] && echo archive > "$out"
    exit 0
  fi
done
exit 22
"""
SHA_STUB = """#!/bin/bash
cat >/dev/null
[ "${SHA_OK:-1}" = "1" ] && exit 0
echo "sha256sum: WARNING: 1 computed checksum did NOT match" >&2
exit 1
"""
TAR_STUB = """#!/bin/bash
dest=""
while [ $# -gt 0 ]; do [ "$1" = "-C" ] && dest="$2"; shift; done
d="$dest/ripgrep-15.2.0-x86_64-unknown-linux-musl"
mkdir -p "$d"
printf '#!/bin/bash\\necho "ripgrep 15.2.0 (stub release)"\\n' > "$d/rg"
chmod +x "$d/rg"
"""
APT_STUB = """#!/bin/bash
echo "$@" >> "$STUB_STATE/apt_calls"
if [ "$1" = "install" ]; then
  [ "${APT_OK:-1}" = "1" ] || exit 100
  printf '#!/bin/bash\\necho "ripgrep 14 (stub apt)"\\n' > "$STUB_BIN/rg"
  chmod +x "$STUB_BIN/rg"
fi
exit 0
"""
SUDO_STUB = '#!/bin/bash\n[ "$1" = "rm" ] && exit 0\nexec "$@"\n'
TIMEOUT_STUB = '#!/bin/bash\nshift\nexec "$@"\n'


def _write(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture()
def env(tmp_path):
    stub = tmp_path / "stubbin"
    state = tmp_path / "state"
    stub.mkdir()
    state.mkdir()
    for name, body in {
        "curl": CURL_STUB, "sha256sum": SHA_STUB, "tar": TAR_STUB,
        "apt-get": APT_STUB, "sudo": SUDO_STUB, "timeout": TIMEOUT_STUB,
    }.items():
        _write(stub / name, body)
    e = {
        "PATH": f"{stub}:/usr/bin:/bin",
        "STUB_STATE": str(state),
        "STUB_BIN": str(stub),
        "RUNNER_TEMP": str(tmp_path / "runner"),
        "HOME": str(tmp_path),
    }
    return e, stub, state


def _run(e, **extra):
    full = {**e, **{k: str(v) for k, v in extra.items()}}
    return subprocess.run(["bash", str(SCRIPT)], env=full, capture_output=True, text=True, timeout=60)


def _curl_calls(state: Path) -> int:
    p = state / "curl_n"
    return int(p.read_text()) if p.exists() else 0


def test_release_fails_then_succeeds_installs_from_release(env, tmp_path):
    e, stub, state = env
    gh_path = tmp_path / "github_path"
    r = _run(e, CURL_FAILS=0, GITHUB_PATH=gh_path)
    assert r.returncode == 0, r.stderr
    assert "stub release" in r.stdout and "pinned release" in r.stdout
    assert not (state / "apt_calls").exists()
    assert gh_path.read_text().strip().endswith("ripgrep-install/bin")


def test_curl_uses_patient_retry_flags(tmp_path):
    text = SCRIPT.read_text()
    for flag in ("--retry 8", "--retry-delay 10", "--retry-all-errors", "--max-time 60", "--connect-timeout 15", "--fail"):
        assert flag in text


def test_release_always_fails_falls_back_to_apt(env):
    e, stub, state = env
    r = _run(e, CURL_FAILS=999)
    assert r.returncode == 0, r.stderr
    assert "installed from apt" in r.stdout
    assert (state / "apt_calls").read_text().splitlines() == ["update", "install -y ripgrep"]


def test_both_fail_exits_nonzero_naming_both(env):
    e, stub, state = env
    r = _run(e, CURL_FAILS=999, APT_OK=0)
    assert r.returncode != 0
    last = r.stderr.strip().splitlines()[-1]
    assert "release path" in last and "apt path" in last


def test_sha_mismatch_never_installs_the_release_binary(env):
    e, stub, state = env
    runner = Path(e["RUNNER_TEMP"])
    r = _run(e, SHA_OK=0, APT_OK=0)
    assert r.returncode != 0
    assert not (runner / "ripgrep-install" / "bin" / "rg").exists()
    assert "stub release" not in r.stdout
    assert "step code 2" in r.stderr  # the release path names the sha failure


def test_sha_mismatch_without_github_path_never_writes_path(env, tmp_path):
    e, stub, state = env
    gh_path = tmp_path / "github_path"
    r = _run(e, SHA_OK=0, APT_OK=0, GITHUB_PATH=gh_path)
    assert r.returncode != 0
    assert not gh_path.exists()


def test_no_github_path_env_writes_nothing(env, tmp_path):
    e, stub, state = env
    r = _run(e)
    assert r.returncode == 0, r.stderr
    assert "GITHUB_PATH" not in r.stderr


def test_rg_already_present_skips(env):
    e, stub, state = env
    _write(stub / "rg", '#!/bin/bash\necho "ripgrep 9 (preinstalled)"\n')
    r = _run(e)
    assert r.returncode == 0
    assert "skipping" in r.stdout and "preinstalled" in r.stdout
    assert _curl_calls(state) == 0
    assert not (state / "apt_calls").exists()


def test_all_four_steps_call_the_script_and_no_inline_curl():
    files = [
        ".github/workflows/vnx-ci.yml",
        ".github/actions/profile-a-prelude/action.yml",
        ".github/workflows/public-ci.yml",
    ]
    calls = 0
    for f in files:
        text = (ROOT / f).read_text()
        calls += text.count("run: bash scripts/ci/install_ripgrep.sh")
        assert "ripgrep-install" not in text, f
        assert "apt-get install -y ripgrep" not in text, f
    assert calls == 4


def test_old_inline_curl_gives_up_after_four_tries(env, tmp_path):
    """Today's inline step (curl --retry 3) dies when the release fails four times."""
    shown = subprocess.run(
        ["git", "show", f"{OLD_SHA}:.github/workflows/vnx-ci.yml"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if shown.returncode != 0:
        pytest.skip("old base commit not available in this clone")
    line = next(ln.strip() for ln in shown.stdout.splitlines() if re.match(r"\s*curl .*--retry 3", ln))
    e, stub, state = env
    (tmp_path / "rg").mkdir()
    full = {**e, "CURL_FAILS": "4", "RG_DIR": str(tmp_path / "rg"), "RG_ARCHIVE": "a.tgz", "RG_URL": "u"}
    old = subprocess.run(["bash", "-c", f"set -e\n{line}"], env=full, capture_output=True, text=True)
    assert old.returncode != 0
    assert _curl_calls(state) == 4


def test_release_that_fails_four_times_still_installs_from_release(env):
    e, stub, state = env
    r = _run(e, CURL_FAILS=4)
    assert r.returncode == 0, r.stderr
    assert "stub release" in r.stdout
    assert not (state / "apt_calls").exists()
    assert _curl_calls(state) == 5
