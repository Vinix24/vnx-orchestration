#!/usr/bin/env python3
"""Tests for C3 (golf C) — `vnx start` / `vnx resume` skip a direct
receipt_processor.sh (re)start when launchd already manages a per-project
instance for this project (OI-1509/OI-1510).

Before this dispatch, `scripts/commands/start.sh` started
`receipt_processor.sh` directly at TWO separate call sites (the re-heal
fallback and the fresh-session fallback), and `scripts/commands/resume.sh`
started `receipt_processor_supervisor.sh` / `receipt_processor.sh` directly
in `_vnx_resume_start_daemons` — none of them checked whether launchd
already had a per-project instance loaded. A manual (re)start there races
the launchd-managed instance for `receipt_processor_supervisor.sh`'s own
flock singleton: whichever process wins keeps running, and if launchd's OWN
attempt loses the race it exits 0 (not a crash), so `KeepAlive.
SuccessfulExit=false` means launchd never retries — if the manual process
that "won" later dies, nothing is left driving the receipt processor.

`scripts/lib/launchd_receipt_processor_guard.sh` is the new shared guard.
Launchd state is always injected via `VNX_LAUNCHCTL_LIST_CMD` (a fake
function name) and `VNX_LAUNCHD_GUARD_PLATFORM` — never read from the real
host, and never a second launchctl reader per call site: both start.sh and
resume.sh call the SAME guard function.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
GUARD_SH = VNX_ROOT / "scripts" / "lib" / "launchd_receipt_processor_guard.sh"
START_SH = VNX_ROOT / "scripts" / "commands" / "start.sh"
RESUME_SH = VNX_ROOT / "scripts" / "commands" / "resume.sh"

_FAKE_LAUNCHCTL_LOADED = """
fake_launchctl_loaded() { printf 'PID\\tStatus\\tLabel\\n-\\t0\\tcom.vnx.receipt-processor.testproj\\n'; }
"""
_FAKE_LAUNCHCTL_EMPTY = """
fake_launchctl_empty() { printf 'PID\\tStatus\\tLabel\\n'; }
"""

_RECEIPT_PROCESSOR_STUB = """#!/usr/bin/env bash
d="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
touch "$d/receipt_processor.ran"
"""

_RECEIPT_PROCESSOR_SUPERVISOR_STUB = """#!/usr/bin/env bash
d="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
touch "$d/receipt_processor_supervisor.ran"
"""

_DISPATCHER_MINIMAL_STUB = """#!/usr/bin/env bash
d="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
touch "$d/dispatcher.ran"
sleep 5
"""


def _run(script: str, timeout: int = 20, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run `script` as a fresh bash subprocess with an explicit environment.

    Starts from a copy of the real environment (so PATH etc. still resolve)
    but always drops the ambient `VNX_PROJECT_ID` — this repo's own CI job
    runs as project "vnx-dev", and that env var takes precedence over the
    `.vnx-project-id` marker these tests write (see
    `_vnx_launchd_guard_project_id`), so the guard silently resolves label
    "com.vnx.receipt-processor.vnx-dev" instead of "...testproj" and reports
    NOT_LOADED regardless of the fake launchctl state (measured: CI run
    34394050973, reproduced locally by exporting VNX_PROJECT_ID=vnx-dev with
    no other change). `VNX_LAUNCHD_GUARD_PLATFORM` / `VNX_LAUNCHCTL_LIST_CMD`
    are also always threaded through this real subprocess environment
    (never a same-shell `VAR=value` line before the call) so every caller
    gets the same, auditable injection point instead of two different ones.
    """
    run_env = dict(os.environ)
    run_env.pop("VNX_PROJECT_ID", None)
    if env:
        run_env.update(env)
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=run_env,
    )


def _write_stub(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


# ---------------------------------------------------------------------------
# scripts/lib/launchd_receipt_processor_guard.sh — direct unit tests
# ---------------------------------------------------------------------------


class TestGuardLibDirectly:
    def test_loaded_when_label_present(self, tmp_path):
        marker = tmp_path / "proj"
        marker.mkdir()
        (marker / ".vnx-project-id").write_text("testproj\n", encoding="utf-8")

        result = _run(f"""
set -uo pipefail
source "{GUARD_SH}"
{_FAKE_LAUNCHCTL_LOADED}
if _vnx_receipt_processor_launchd_loaded "{marker}"; then
  echo "LOADED:$VNX_LAUNCHD_GUARD_LABEL"
else
  echo "NOT_LOADED"
fi
""", env={"VNX_LAUNCHD_GUARD_PLATFORM": "Darwin", "VNX_LAUNCHCTL_LIST_CMD": "fake_launchctl_loaded"})
        assert result.returncode == 0, result.stderr
        assert "LOADED:com.vnx.receipt-processor.testproj" in result.stdout

    def test_not_loaded_when_label_absent(self, tmp_path):
        marker = tmp_path / "proj"
        marker.mkdir()
        (marker / ".vnx-project-id").write_text("testproj\n", encoding="utf-8")

        result = _run(f"""
set -uo pipefail
source "{GUARD_SH}"
{_FAKE_LAUNCHCTL_EMPTY}
if _vnx_receipt_processor_launchd_loaded "{marker}"; then
  echo "LOADED"
else
  echo "NOT_LOADED"
fi
""", env={"VNX_LAUNCHD_GUARD_PLATFORM": "Darwin", "VNX_LAUNCHCTL_LIST_CMD": "fake_launchctl_empty"})
        assert result.returncode == 0, result.stderr
        assert "NOT_LOADED" in result.stdout

    def test_not_loaded_on_non_darwin_even_if_label_would_match(self, tmp_path):
        marker = tmp_path / "proj"
        marker.mkdir()
        (marker / ".vnx-project-id").write_text("testproj\n", encoding="utf-8")

        result = _run(f"""
set -uo pipefail
source "{GUARD_SH}"
{_FAKE_LAUNCHCTL_LOADED}
if _vnx_receipt_processor_launchd_loaded "{marker}"; then
  echo "LOADED"
else
  echo "NOT_LOADED"
fi
""", env={"VNX_LAUNCHD_GUARD_PLATFORM": "Linux", "VNX_LAUNCHCTL_LIST_CMD": "fake_launchctl_loaded"})
        assert result.returncode == 0, result.stderr
        assert "NOT_LOADED" in result.stdout

    def test_not_loaded_when_project_id_unresolvable(self, tmp_path):
        empty_dir = tmp_path / "no-marker"
        empty_dir.mkdir()
        result = _run(f"""
set -uo pipefail
source "{GUARD_SH}"
{_FAKE_LAUNCHCTL_LOADED}
if _vnx_receipt_processor_launchd_loaded "{empty_dir}"; then
  echo "LOADED"
else
  echo "NOT_LOADED"
fi
""", env={"VNX_LAUNCHD_GUARD_PLATFORM": "Darwin", "VNX_LAUNCHCTL_LIST_CMD": "fake_launchctl_loaded"})
        assert result.returncode == 0, result.stderr
        assert "NOT_LOADED" in result.stdout

    def test_a_different_projects_label_never_matches(self, tmp_path):
        """mission-control's own instance must never satisfy testproj's check."""
        marker = tmp_path / "proj"
        marker.mkdir()
        (marker / ".vnx-project-id").write_text("testproj\n", encoding="utf-8")

        result = _run(f"""
set -uo pipefail
source "{GUARD_SH}"
fake_launchctl_other() {{ printf 'PID\\tStatus\\tLabel\\n-\\t0\\tcom.vnx.receipt-processor.mission-control\\n'; }}
if _vnx_receipt_processor_launchd_loaded "{marker}"; then
  echo "LOADED"
else
  echo "NOT_LOADED"
fi
""", env={"VNX_LAUNCHD_GUARD_PLATFORM": "Darwin", "VNX_LAUNCHCTL_LIST_CMD": "fake_launchctl_other"})
        assert result.returncode == 0, result.stderr
        assert "NOT_LOADED" in result.stdout

    def test_not_loaded_when_another_projects_id_is_a_prefix(self, tmp_path):
        """'project' is a prefix of 'project-alpha': a substring match on the
        label would report THIS project's instance as loaded when only the
        LONGER project's instance exists (OI-1721). The guard must return 1."""
        marker = tmp_path / "proj"
        marker.mkdir()
        (marker / ".vnx-project-id").write_text("project\n", encoding="utf-8")

        result = _run(f"""
set -uo pipefail
source "{GUARD_SH}"
fake_launchctl_prefix() {{ printf 'PID\\tStatus\\tLabel\\n-\\t0\\tcom.vnx.receipt-processor.project-alpha\\n'; }}
if _vnx_receipt_processor_launchd_loaded "{marker}"; then
  echo "LOADED"
else
  echo "NOT_LOADED"
fi
""", env={"VNX_LAUNCHD_GUARD_PLATFORM": "Darwin", "VNX_LAUNCHCTL_LIST_CMD": "fake_launchctl_prefix"})
        assert result.returncode == 0, result.stderr
        assert "NOT_LOADED" in result.stdout

    def test_loaded_when_the_longer_prefix_variant_is_loaded(self, tmp_path):
        """The genuine positive must survive: the longer project's own label,
        exactly present, is still reported loaded (no anchor so tight it
        breaks the real match)."""
        marker = tmp_path / "proj"
        marker.mkdir()
        (marker / ".vnx-project-id").write_text("project-alpha\n", encoding="utf-8")

        result = _run(f"""
set -uo pipefail
source "{GUARD_SH}"
fake_launchctl_prefix() {{ printf 'PID\\tStatus\\tLabel\\n-\\t0\\tcom.vnx.receipt-processor.project-alpha\\n'; }}
if _vnx_receipt_processor_launchd_loaded "{marker}"; then
  echo "LOADED:$VNX_LAUNCHD_GUARD_LABEL"
else
  echo "NOT_LOADED"
fi
""", env={"VNX_LAUNCHD_GUARD_PLATFORM": "Darwin", "VNX_LAUNCHCTL_LIST_CMD": "fake_launchctl_prefix"})
        assert result.returncode == 0, result.stderr
        assert "LOADED:com.vnx.receipt-processor.project-alpha" in result.stdout


# ---------------------------------------------------------------------------
# scripts/commands/start.sh — _vnx_maybe_start_receipt_processor at BOTH
# call sites (via the shared helper — grep guard below covers "every site").
# ---------------------------------------------------------------------------


class TestStartShSkipsWhenLaunchdManaged:
    def _harness(self, tmp_path, *, loaded: bool) -> tuple[str, Path, dict[str, str]]:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        proj_dir = tmp_path / "proj"
        proj_dir.mkdir()
        (proj_dir / ".vnx-project-id").write_text("testproj\n", encoding="utf-8")
        _write_stub(scripts_dir / "receipt_processor.sh", _RECEIPT_PROCESSOR_STUB)

        fake_fn = "fake_launchctl_loaded" if loaded else "fake_launchctl_empty"
        fake_body = _FAKE_LAUNCHCTL_LOADED if loaded else _FAKE_LAUNCHCTL_EMPTY

        script = f"""
set -uo pipefail
export VNX_HOME="{VNX_ROOT}"
export PROJECT_ROOT="{proj_dir}"
log() {{ echo "[log] $*"; }}
err() {{ echo "[err] $*" >&2; }}
source "{START_SH}"
{fake_body}
_vnx_maybe_start_receipt_processor "{scripts_dir}" "{log_dir}" "started"
sleep 0.3
"""
        env = {"VNX_LAUNCHD_GUARD_PLATFORM": "Darwin", "VNX_LAUNCHCTL_LIST_CMD": fake_fn}
        return script, scripts_dir, env

    def test_skips_direct_start_when_launchd_manages_it(self, tmp_path):
        script, scripts_dir, env = self._harness(tmp_path, loaded=True)
        result = _run(script, env=env)
        assert result.returncode == 0, result.stderr
        assert "skipping direct started" in result.stdout
        assert not (scripts_dir / "receipt_processor.ran").exists(), (
            "receipt_processor.sh ran even though launchd already manages this "
            "project's instance — the two would race for the supervisor's lock"
        )

    def test_starts_directly_when_launchd_does_not_manage_it(self, tmp_path):
        script, scripts_dir, env = self._harness(tmp_path, loaded=False)
        result = _run(script, env=env)
        assert result.returncode == 0, result.stderr
        assert "Receipt processor V4 started" in result.stdout
        assert (scripts_dir / "receipt_processor.ran").exists(), (
            "receipt_processor.sh must still start directly when launchd has no "
            "instance for this project — today's behavior must be preserved"
        )

    def test_every_receipt_processor_start_site_in_start_sh_is_guarded(self):
        """Regression guard against a partial fix: BOTH original call sites
        (the re-heal fallback and the fresh-session fallback) must route
        through the shared guarded helper, not a re-inlined raw start. The
        ONE legitimate raw `nohup bash ./receipt_processor.sh` left in the
        file is the helper's own implementation, gated by the launchd check
        immediately above it — never a second, unguarded copy."""
        text = START_SH.read_text(encoding="utf-8")
        raw_starts = [
            line for line in text.splitlines()
            if "nohup" in line and "receipt_processor.sh" in line
        ]
        assert len(raw_starts) == 1, (
            f"expected exactly 1 raw receipt_processor.sh start (inside the "
            f"guarded helper), found {len(raw_starts)}: {raw_starts}"
        )
        call_sites = text.count('_vnx_maybe_start_receipt_processor "$scripts_dir"')
        assert call_sites == 2, (
            f"expected exactly 2 call sites (re-heal + fresh-session), found {call_sites}"
        )


# ---------------------------------------------------------------------------
# scripts/commands/resume.sh — _vnx_resume_start_daemons
# ---------------------------------------------------------------------------


class TestResumeShSkipsWhenLaunchdManaged:
    def _harness(
        self, tmp_path, *, loaded: bool, with_supervisor: bool = True
    ) -> tuple[str, Path, dict[str, str]]:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        proj_dir = tmp_path / "proj"
        proj_dir.mkdir()
        (proj_dir / ".vnx-project-id").write_text("testproj\n", encoding="utf-8")

        _write_stub(scripts_dir / "dispatcher_minimal.sh", _DISPATCHER_MINIMAL_STUB)
        if with_supervisor:
            _write_stub(
                scripts_dir / "receipt_processor_supervisor.sh",
                _RECEIPT_PROCESSOR_SUPERVISOR_STUB,
            )
        else:
            _write_stub(scripts_dir / "receipt_processor.sh", _RECEIPT_PROCESSOR_STUB)

        fake_fn = "fake_launchctl_loaded" if loaded else "fake_launchctl_empty"
        fake_body = _FAKE_LAUNCHCTL_LOADED if loaded else _FAKE_LAUNCHCTL_EMPTY

        script = f"""
set -uo pipefail
export VNX_HOME="{VNX_ROOT}"
export PROJECT_ROOT="{proj_dir}"
log() {{ echo "[log] $*"; }}
err() {{ echo "[err] $*" >&2; }}
source "{RESUME_SH}"
{fake_body}
_vnx_resume_start_daemons "{scripts_dir}" "{logs_dir}"
sleep 0.3
echo "RECEIPT_PID=[$_resume_receipt_pid]"
_vnx_resume_verify_readiness
echo "READINESS_RC=$?"
"""
        env = {"VNX_LAUNCHD_GUARD_PLATFORM": "Darwin", "VNX_LAUNCHCTL_LIST_CMD": fake_fn}
        return script, scripts_dir, env

    def test_skips_manual_start_when_launchd_manages_it(self, tmp_path):
        script, scripts_dir, env = self._harness(tmp_path, loaded=True)
        result = _run(script, env=env)
        assert result.returncode == 0, result.stderr
        assert "skipping manual (re)start" in result.stdout
        assert not (scripts_dir / "receipt_processor_supervisor.ran").exists(), (
            "receipt_processor_supervisor.sh ran even though launchd already "
            "manages this project's instance"
        )
        assert "RECEIPT_PID=[]" in result.stdout, (
            "_resume_receipt_pid must stay empty when the manual start was skipped"
        )
        assert "READINESS_RC=0" in result.stdout, (
            "an empty (skipped-by-design) receipt PID must not fail readiness verification"
        )

    def test_starts_supervisor_directly_when_launchd_does_not_manage_it(self, tmp_path):
        script, scripts_dir, env = self._harness(tmp_path, loaded=False)
        result = _run(script, env=env)
        assert result.returncode == 0, result.stderr
        assert (scripts_dir / "receipt_processor_supervisor.ran").exists(), (
            "receipt_processor_supervisor.sh must still start directly when "
            "launchd has no instance for this project"
        )
        assert "RECEIPT_PID=[]" not in result.stdout

    def test_falls_back_to_direct_receipt_processor_when_supervisor_absent(self, tmp_path):
        """Same guard, same shape, on the OTHER receipt-processor branch
        (direct receipt_processor.sh, not the supervisor)."""
        script, scripts_dir, env = self._harness(tmp_path, loaded=True, with_supervisor=False)
        result = _run(script, env=env)
        assert result.returncode == 0, result.stderr
        assert "skipping manual (re)start" in result.stdout
        assert not (scripts_dir / "receipt_processor.ran").exists()
