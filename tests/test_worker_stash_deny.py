"""test_worker_stash_deny.py — OI-1447: every claude-CLI worker lane refuses `git stash`.

``refs/stash`` belongs to the repository, not to a worktree, so every dispatch
worktree of this repo shares ONE stash. ``git stash drop`` / ``clear`` / a
``pop`` of the wrong entry is irreversible on state outside the worker's
isolation boundary (2026-08-23: a build worker's batched ``git stash drop``
removed an entry of an unrelated branch). A worker has its own worktree and its
own branch and never needs the stash: work in progress is committed.

Per-lane argv assertions (the real builders, not the constant):

  L1  worker_permissions.build_claude_scope_args (role profile)
  L2  deepseek_harness_spawn.build_harness_argv / build_harness_cli_args
  L3  glm_harness_spawn.build_harness_cli_args
  L4  subprocess_adapter._build_worker_scope_args / _fallback_scope_args / deliver

L5 (kimi) and L6 (codex, gemini, litellm, local gemma) run other CLIs and are
listed here with the reason they cannot carry this deny; they are not asserted
to contain it.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_LIB = REPO_ROOT / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import subprocess_adapter  # noqa: E402
from provider_spawns import deepseek_harness_spawn as dh  # noqa: E402
from provider_spawns import glm_harness_spawn as gh  # noqa: E402
from provider_spawns.harness_config_dir import bare_harness_cli_args  # noqa: E402
import worker_permissions  # noqa: E402
from worker_permissions import (  # noqa: E402
    build_claude_scope_args,
    resolve_worker_profile,
)

STASH_DENY = "Bash(git stash:*)"
DENY_FLAG = "--disallowedTools"


# ---------------------------------------------------------------------------
# A small model of the claude variadic rule: a --disallowedTools value list
# ends at the next token that starts with "--".
# ---------------------------------------------------------------------------

def variadic_values(argv: list[str], flag: str) -> list[str]:
    """Every value belonging to ``flag`` across all its occurrences in argv."""
    values: list[str] = []
    for idx, token in enumerate(argv):
        if token != flag:
            continue
        for nxt in argv[idx + 1:]:
            if nxt.startswith("--"):
                break
            values.extend(part for part in nxt.split(",") if part)
    return values


def deny_occurrences(argv: list[str]) -> int:
    return argv.count(DENY_FLAG)


# ---------------------------------------------------------------------------
# The one constant
# ---------------------------------------------------------------------------

class TestOneConstant:
    def test_constant_is_a_tuple_holding_the_stash_deny(self):
        constant = worker_permissions.WORKER_CLI_DENIED_TOOLS
        assert isinstance(constant, tuple)
        assert STASH_DENY in constant

    def test_constant_comment_names_oi_1447_and_the_shared_ref(self):
        source = (SCRIPTS_LIB / "worker_permissions.py").read_text(encoding="utf-8")
        anchor = source.index("WORKER_CLI_DENIED_TOOLS")
        block = source[max(0, anchor - 1200):anchor]
        assert "OI-1447" in block
        assert "refs/stash" in block


# ---------------------------------------------------------------------------
# L1 — headless claude lane: the role profile
# ---------------------------------------------------------------------------

class TestLaneL1RoleProfile:
    def test_real_profile_refuses_git_stash(self):
        profile = resolve_worker_profile("backend-developer")
        argv = build_claude_scope_args(profile)
        assert STASH_DENY in variadic_values(argv, DENY_FLAG)
        assert deny_occurrences(argv) == 1

    def test_requires_mcp_keeps_the_stash_deny(self):
        profile = resolve_worker_profile("backend-developer")
        argv = build_claude_scope_args(profile, requires_mcp=True)
        assert STASH_DENY in variadic_values(argv, DENY_FLAG)
        assert deny_occurrences(argv) == 1

    def test_only_one_deny_flag_and_no_prompt_position(self):
        argv = build_claude_scope_args(resolve_worker_profile("security-engineer"))
        # Scope args are appended after the prompt; the deny list must end the argv
        # rather than swallow a following positional.
        assert argv[-2] == DENY_FLAG
        assert not argv[-1].startswith("--")


# ---------------------------------------------------------------------------
# L2 — DeepSeek harness
# ---------------------------------------------------------------------------

class TestLaneL2DeepseekHarness:
    def test_build_harness_argv_refuses_git_stash(self):
        argv = dh.build_harness_argv("claude", "deepseek-v4-pro", output_format="json")
        assert STASH_DENY in variadic_values(argv, DENY_FLAG)
        assert deny_occurrences(argv) == 1
        # The prompt travels on stdin for this builder; the last flag the CLI
        # reads must stay the boolean terminator of --mcp-config.
        assert argv[-1] == "--strict-mcp-config"

    def test_build_harness_cli_args_refuses_git_stash(self):
        argv = dh.build_harness_cli_args(None)
        assert STASH_DENY in variadic_values(argv, DENY_FLAG)
        assert deny_occurrences(argv) == 1
        assert argv[-1] == "--strict-mcp-config"

    def test_deny_does_not_swallow_the_worktree_instructions(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# worker\n", encoding="utf-8")
        argv = dh.build_harness_cli_args(tmp_path)
        assert STASH_DENY in variadic_values(argv, DENY_FLAG)
        idx = argv.index("--append-system-prompt-file")
        assert argv[idx + 1] == str(tmp_path / "CLAUDE.md")


# ---------------------------------------------------------------------------
# L3 — GLM harness
# ---------------------------------------------------------------------------

class TestLaneL3GlmHarness:
    def test_build_harness_cli_args_refuses_git_stash(self):
        argv = gh.build_harness_cli_args(None)
        assert STASH_DENY in variadic_values(argv, DENY_FLAG)
        assert deny_occurrences(argv) == 1
        assert argv[-1] == "--strict-mcp-config"


# ---------------------------------------------------------------------------
# L4 — terminal-pinned subprocess adapter
# ---------------------------------------------------------------------------

def _alive_process() -> MagicMock:
    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = 12345
    proc.poll.return_value = None
    proc.returncode = None
    return proc


class TestLaneL4SubprocessAdapter:
    def test_profile_scope_args_refuse_git_stash(self):
        argv = subprocess_adapter._build_worker_scope_args("backend-developer")
        assert STASH_DENY in variadic_values(argv, DENY_FLAG)
        assert deny_occurrences(argv) == 1

    def test_import_failure_fallback_refuses_git_stash(self):
        argv = subprocess_adapter._fallback_scope_args()
        assert STASH_DENY in variadic_values(argv, DENY_FLAG)
        assert deny_occurrences(argv) == 1

    def test_deliver_argv_keeps_the_prompt_before_the_deny(self):
        adapter = subprocess_adapter.SubprocessAdapter()
        with patch("subprocess.Popen", return_value=_alive_process()) as mock_popen:
            adapter.deliver("T1", "dispatch-stash-001", instruction="do the work")
        cmd = mock_popen.call_args[0][0]
        assert STASH_DENY in variadic_values(cmd, DENY_FLAG)
        assert cmd.index("do the work") < cmd.index(DENY_FLAG)


# ---------------------------------------------------------------------------
# Every claude -p argv builder in scripts/lib is accounted for
# ---------------------------------------------------------------------------

# Functions in scripts/lib whose body builds a claude argv with -p/--print.
# A NEW lane that forgets the deny must show up here and fail the set equality.
ARGV_BUILDERS = {
    ("scripts/lib/subprocess_adapter.py", "deliver"): "worker lane L1/L4 (covered)",
    ("scripts/lib/classifier_providers/deepseek_provider.py", "classify"): (
        "read-only classifier; uses bare_harness_cli_args so it inherits the deny"
    ),
    ("scripts/lib/classifier_providers/haiku_provider.py", "classify"): (
        "read-only classifier, no worktree, no Bash-capable role"
    ),
    ("scripts/lib/llm_decision_router.py", "_decide_claude_cli"): (
        "read-only decision router, no worktree"
    ),
    ("scripts/lib/report_classifier.py", "_call_haiku"): (
        "read-only report classifier, no worktree"
    ),
    ("scripts/lib/t0_decision_summarizer.py", "summarize_with_haiku"): (
        "read-only summarizer, no worktree"
    ),
}

# Lanes L2/L3 build their claude argv through the shared harness tail and carry
# no local "claude" literal, so the discovery scan below cannot see them; they
# are pinned explicitly and asserted to carry the deny.
HARNESS_BUILDERS = [
    ("provider_spawns/harness_config_dir.py", "bare_harness_cli_args"),
    ("provider_spawns/deepseek_harness_spawn.py", "build_harness_cli_args"),
    ("provider_spawns/deepseek_harness_spawn.py", "build_harness_argv"),
    ("provider_spawns/glm_harness_spawn.py", "build_harness_cli_args"),
]


def _discover_argv_builders() -> dict[tuple[str, str], str]:
    found: dict[tuple[str, str], str] = {}
    for path in sorted(SCRIPTS_LIB.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            consts: set[str] = set()
            names: set[str] = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    consts.add(sub.value)
                elif isinstance(sub, ast.Name):
                    names.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    names.add(sub.attr)
            has_print = bool(consts & {"-p", "--print"})
            starts_claude = any(c == "claude" or c.endswith("/claude") for c in consts)
            if has_print and (starts_claude or "resolve_claude_cli" in names):
                found[(str(path.relative_to(REPO_ROOT)), node.name)] = node.name
    return found


def test_every_claude_argv_builder_is_classified():
    discovered = set(_discover_argv_builders())
    assert discovered == set(ARGV_BUILDERS), (
        "a claude -p argv builder in scripts/lib is not classified: "
        f"{sorted(discovered ^ set(ARGV_BUILDERS))}"
    )


@pytest.mark.parametrize("relpath,func", HARNESS_BUILDERS)
def test_harness_builders_are_pinned(relpath, func):
    module = ast.parse((SCRIPTS_LIB / relpath).read_text(encoding="utf-8"))
    names = {
        node.name for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert func in names, f"{relpath}:{func} is gone — re-pin the guard test"


def test_harness_builders_share_the_one_tail():
    """L2/L3 must inherit the deny from the one shared tail, not a local copy."""
    tail = (SCRIPTS_LIB / "provider_spawns" / "harness_config_dir.py").read_text(encoding="utf-8")
    assert "WORKER_CLI_DENIED_TOOLS" in tail
    for module in ("deepseek_harness_spawn.py", "glm_harness_spawn.py"):
        source = (SCRIPTS_LIB / "provider_spawns" / module).read_text(encoding="utf-8")
        assert "bare_harness_cli_args" in source
        assert STASH_DENY not in source, f"{module} copies the deny instead of the shared tail"


# ---------------------------------------------------------------------------
# L5 / L6 — other CLIs: what they offer (report-only, not wired here)
# ---------------------------------------------------------------------------

def test_kimi_and_codex_spawns_do_not_use_the_claude_disallow_flag():
    kimi = (SCRIPTS_LIB / "provider_spawns" / "kimi_spawn.py").read_text(encoding="utf-8")
    codex = (SCRIPTS_LIB / "provider_spawns" / "codex_spawn.py").read_text(encoding="utf-8")
    assert "--disallowedTools" not in kimi
    assert "--disallowedTools" not in codex
