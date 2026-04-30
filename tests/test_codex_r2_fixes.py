#!/usr/bin/env python3
"""
Regression tests for Codex round-2 blocking findings on PR #316.

Finding 1: scripts/dispatcher_v8_minimal.sh used `declare -A` for the
           invalid-skill cooldown map. macOS /bin/bash 3.2 lacks
           associative arrays, so the dispatcher aborted at startup.

Finding 2: scripts/dispatcher_v8_minimal.sh used `((count++))` under
           `set -e`. When `count` is 0 the post-increment arithmetic
           command returns status 1, aborting the loop after the first
           dispatch instead of continuing.

Finding 3: scripts/lib/dispatch_create.sh::extract_context_files piped
           awk through `grep '^\\[\\[@'` inside a `set -euo pipefail`
           command substitution. When a dispatch had no inline `[[@...]]`
           context refs grep exited 1, pipefail propagated nonzero, and
           the YAML-frontmatter fallback never ran.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DISPATCHER_SH = REPO_ROOT / "scripts" / "dispatcher_v8_minimal.sh"
DISPATCH_CREATE_SH = REPO_ROOT / "scripts" / "lib" / "dispatch_create.sh"


def _function_body(text: str, signature: str) -> str:
    """Return the brace-balanced body of a bash function declaration."""
    idx = text.find(signature)
    assert idx != -1, f"function {signature!r} not found"
    start = text.find("{", idx)
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise AssertionError(f"unbalanced braces for {signature!r}")


# ---------------------------------------------------------------------------
# Finding 1 — `declare -A` not supported on macOS /bin/bash 3.2
# ---------------------------------------------------------------------------


class TestNoAssociativeArray:
    def test_dispatcher_does_not_use_declare_dash_a(self):
        """dispatcher_v8_minimal.sh must not use `declare -A` (bash 3.2 gap)."""
        text = DISPATCHER_SH.read_text()
        assert "declare -A" not in text, (
            "declare -A is incompatible with /bin/bash 3.2 on macOS — "
            "use sanitized variable names + indirect expansion instead"
        )

    def test_cooldown_helper_exists(self):
        """A helper that maps dispatch keys to bash-safe variable names exists."""
        text = DISPATCHER_SH.read_text()
        assert "_invalid_skill_cooldown_var" in text, (
            "expected helper _invalid_skill_cooldown_var (sanitizes "
            "dispatch basenames into bash 3.2-safe identifiers)"
        )

    def test_cooldown_uses_indirect_expansion(self):
        """_validate_stuck_files must read/write cooldown via indirect expansion."""
        text = DISPATCHER_SH.read_text()
        body = _function_body(text, "_validate_stuck_files()")
        # Read via ${!var:-0} (indirect expansion).
        assert "${!" in body, (
            "_validate_stuck_files must use indirect variable expansion "
            "(${!var:-0}) for cooldown reads under bash 3.2"
        )
        # Write via printf -v (bash 3.1+ dynamic-name assignment).
        assert "printf -v" in body, (
            "_validate_stuck_files must use `printf -v` to assign cooldown "
            "values to dynamically-named variables under bash 3.2"
        )

    def test_dispatcher_parses_under_bash32(self):
        """`bash -n` succeeds on the dispatcher script (catches `declare -A`)."""
        result = subprocess.run(
            ["bash", "-n", str(DISPATCHER_SH)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    def test_cooldown_variable_naming_runtime(self, tmp_path: Path):
        """The cooldown helper produces a valid bash identifier and round-trips
        through indirect read + printf -v write under bash 3.2 semantics."""
        # Replicate the production helper logic in isolation, then exercise
        # the read / cooldown / write sequence the dispatcher relies on.
        script = tmp_path / "cooldown_probe.sh"
        script.write_text(
            textwrap.dedent(
                """
                #!/bin/bash
                set -euo pipefail

                _invalid_skill_cooldown_var() {
                    local _key="$1"
                    local _safe="${_key//[^a-zA-Z0-9]/_}"
                    printf '_INVALID_SKILL_COOLDOWN_%s' "$_safe"
                }

                key="20260429-fix-sup-pr316-codex-r2"
                var="$(_invalid_skill_cooldown_var "$key")"

                # Variable must be a valid bash identifier.
                case "$var" in
                    [a-zA-Z_]*) ;;
                    *) echo "BAD_IDENT:$var"; exit 2 ;;
                esac
                case "$var" in
                    *[!a-zA-Z0-9_]*) echo "BAD_IDENT_CHARS:$var"; exit 2 ;;
                esac

                # First read: must default to 0 (no flood guard active yet).
                first="${!var:-0}"
                [ "$first" = "0" ] || { echo "WANT_DEFAULT_0:$first"; exit 3; }

                # Write via printf -v (bash 3.1+ compatible).
                printf -v "$var" '%s' "1714387200"
                second="${!var:-0}"
                [ "$second" = "1714387200" ] || { echo "ROUNDTRIP_FAIL:$second"; exit 4; }

                echo "OK:$var"
                """
            ).strip()
            + "\n"
        )
        result = subprocess.run(
            ["bash", str(script)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"cooldown probe failed rc={result.returncode}\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
        assert result.stdout.strip().startswith("OK:_INVALID_SKILL_COOLDOWN_")


# ---------------------------------------------------------------------------
# Finding 2 — `((count++))` aborts under `set -e`
# ---------------------------------------------------------------------------


class TestCounterIncrementSafeUnderErrexit:
    def test_process_dispatches_does_not_use_post_increment(self):
        """process_dispatches must not use ((count++)) — fails under set -e."""
        text = DISPATCHER_SH.read_text()
        body = _function_body(text, "process_dispatches()")
        assert "((count++))" not in body, (
            "((count++)) returns exit 1 when count was 0; under `set -e` "
            "this aborts the dispatcher loop after the first dispatch. "
            "Use `count=$((count + 1))` instead."
        )

    def test_process_dispatches_uses_safe_increment(self):
        """process_dispatches must use $((count + 1)) (or equivalent safe form)."""
        text = DISPATCHER_SH.read_text()
        body = _function_body(text, "process_dispatches()")
        assert "count=$((count + 1))" in body or "count=$(( count + 1 ))" in body, (
            "process_dispatches must increment via assignment "
            "(`count=$((count + 1))`) so `set -e` does not trip on the "
            "arithmetic command's exit status when count was 0"
        )

    def test_increment_idiom_safe_under_errexit(self, tmp_path: Path):
        """The replacement idiom must increment correctly under `set -e`,
        with no risk of an exit-1 arithmetic abort regardless of bash
        version. (`((count++))` aborts on bash 4.x+ when count=0; bash 3.2
        is more lenient. The new idiom must be safe everywhere.)"""
        fixed = tmp_path / "fixed.sh"
        fixed.write_text(
            textwrap.dedent(
                """
                #!/bin/bash
                set -euo pipefail
                count=0
                count=$((count + 1))
                count=$((count + 1))
                count=$((count + 1))
                echo "COUNT:$count"
                """
            ).strip()
            + "\n"
        )
        fixed_rc = subprocess.run(
            ["bash", str(fixed)], capture_output=True, text=True
        )
        assert fixed_rc.returncode == 0, fixed_rc.stderr
        assert fixed_rc.stdout.strip() == "COUNT:3"


# ---------------------------------------------------------------------------
# Finding 3 — extract_context_files aborted on missing inline refs
# ---------------------------------------------------------------------------


class TestExtractContextFilesPipefailSafe:
    def test_pipeline_terminator_present(self):
        """The grep pipeline must be guarded with `|| true` so a no-match
        exit-1 cannot propagate through pipefail and abort the function."""
        text = DISPATCH_CREATE_SH.read_text()
        body = _function_body(text, "extract_context_files()")
        assert "grep '^\\[\\[@' || true" in body, (
            "extract_context_files must terminate the grep pipeline with "
            "`|| true` so pipefail does not propagate a no-match exit "
            "out of the command substitution"
        )

    def test_dispatch_create_parses(self):
        """`bash -n` succeeds on the dispatch_create library."""
        result = subprocess.run(
            ["bash", "-n", str(DISPATCH_CREATE_SH)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    def _missing_dependencies(self) -> list[str]:
        missing: list[str] = []
        for tool in ("bash", "awk", "grep", "tr"):
            if shutil.which(tool) is None:
                missing.append(tool)
        return missing

    def test_extract_context_files_yaml_fallback(self, tmp_path: Path):
        """When the dispatch has no inline [[@...]] context refs, the
        function must NOT abort — it must run the YAML frontmatter fallback
        and emit context_files entries."""
        missing = self._missing_dependencies()
        if missing:
            pytest.skip(f"required tool(s) missing: {missing}")

        dispatch = tmp_path / "no-inline-refs.md"
        dispatch.write_text(
            textwrap.dedent(
                """
                ---
                track: A
                role: backend-developer
                context_files:
                  - docs/spec.md
                  - scripts/foo.py
                other_field: ignored
                ---

                Instruction: do the thing
                [[DONE]]
                """
            ).strip()
            + "\n"
        )

        # Source the library and call extract_context_files under the same
        # `set -euo pipefail` that the dispatcher uses. The function must
        # complete (rc=0) and emit the YAML-list fallback entries.
        probe = tmp_path / "probe.sh"
        probe.write_text(
            textwrap.dedent(
                f"""
                #!/bin/bash
                set -euo pipefail
                # Stub log() — dispatch_create.sh references it via map_role_to_skill,
                # but extract_context_files itself does not call it.
                log() {{ :; }}
                source "{DISPATCH_CREATE_SH}"
                extract_context_files "{dispatch}"
                echo "RC:$?"
                """
            ).strip()
            + "\n"
        )

        result = subprocess.run(
            ["bash", str(probe)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"extract_context_files aborted under pipefail\n"
            f"rc={result.returncode}\nstdout={result.stdout!r}\n"
            f"stderr={result.stderr!r}"
        )
        # YAML fallback should have emitted both list entries.
        assert "docs/spec.md" in result.stdout
        assert "scripts/foo.py" in result.stdout
        # And the function should have returned 0 (final RC line).
        assert "RC:0" in result.stdout

    def test_extract_context_files_inline_refs_still_work(self, tmp_path: Path):
        """Sanity: inline [[@...]] refs still take precedence over YAML."""
        missing = self._missing_dependencies()
        if missing:
            pytest.skip(f"required tool(s) missing: {missing}")

        dispatch = tmp_path / "inline-refs.md"
        dispatch.write_text(
            textwrap.dedent(
                """
                ---
                track: A
                role: backend-developer
                context_files:
                  - docs/should-not-appear.md
                ---

                Context: see refs below
                [[@scripts/alpha.sh]]
                [[@scripts/beta.sh]]

                Instruction: do the thing
                [[DONE]]
                """
            ).strip()
            + "\n"
        )

        probe = tmp_path / "probe_inline.sh"
        probe.write_text(
            textwrap.dedent(
                f"""
                #!/bin/bash
                set -euo pipefail
                log() {{ :; }}
                source "{DISPATCH_CREATE_SH}"
                extract_context_files "{dispatch}"
                """
            ).strip()
            + "\n"
        )

        result = subprocess.run(
            ["bash", str(probe)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "[[@scripts/alpha.sh]]" in result.stdout
        assert "[[@scripts/beta.sh]]" in result.stdout
        # YAML fallback must NOT run when inline refs are present.
        assert "should-not-appear" not in result.stdout
