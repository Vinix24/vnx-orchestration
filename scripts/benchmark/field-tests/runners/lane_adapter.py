"""lane_adapter.py — route a benchmark dispatch through the right VNX lane.

Each lane in `models.yaml` maps to an existing VNX dispatcher:

| provider          | dispatcher                              | notes                                            |
|-------------------|-----------------------------------------|--------------------------------------------------|
| claude            | tmux_interactive_dispatch.py            | interactive `claude` on subscription (June-15 escape); isolated worktree per dispatch |
| litellm:deepseek  | provider_dispatch.py                    | provider=litellm:deepseek                        |
| litellm:moonshot  | provider_dispatch.py                    | provider=kimi (CLI OAuth)                        |
| litellm:zai       | provider_dispatch.py                    | provider=litellm:zai                             |
| local-gemma       | provider_dispatch.py                    | provider=local-gemma                             |

Claude lanes MUST route via tmux-spawn — `subprocess_dispatch.py` runs `claude -p`
which bills API credits instead of the subscription (CLAUDE.md "June-15 escape").

Returns a DispatchResult dataclass with the receipt path + timing + raw stdout/stderr.
No mocking; if the dispatcher binary or credentials are missing the call fails loudly.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


REPO_ROOT = Path(__file__).resolve().parents[4]
TMUX_INTERACTIVE_DISPATCH = REPO_ROOT / "scripts" / "lib" / "tmux_interactive_dispatch.py"
SUBPROCESS_DISPATCH = REPO_ROOT / "scripts" / "lib" / "subprocess_dispatch.py"
PROVIDER_DISPATCH = REPO_ROOT / "scripts" / "lib" / "provider_dispatch.py"
SKILLS_ROOT = REPO_ROOT / ".claude" / "skills"

# Skill-prefix injection lives in scripts/lib/skill_prefix.py — provider-agnostic
# plain-text prepend. Used here for non-claude lanes (kimi/codex/deepseek-bare)
# that lack a native skill-loading mechanism. Claude lanes already inject skills
# via their dispatcher's _inject_skill_context — we don't double-inject.
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
from skill_prefix import inject_skill_prefix_into_instruction  # noqa: E402

# Models where the interactive Claude Code lane is known-broken (hidden-thinking
# loops + interactive-session hangs per Anthropic GitHub #63390 and #64153).
# Route these via headless subprocess_dispatch instead, which exercises the same
# subscription pre-15-juni and bypasses the interactive-session bug.
#
# Expanded 2026-06-05 after retry-run observed opus-4-7 + sonnet-4-6 hitting
# the same 0.1s immediate-exit pattern on T3-09 instruction content. Issue
# #63390 explicitly names "Opus 4.8, Sonnet 4.6"; opus-4-7 hit it empirically
# on identical content. Safer to route all three through headless until
# Anthropic ships the fix (currently #63390 + #64153 open as of 2026-06-05).
HEADLESS_FORCED_MODELS = {
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-sonnet-4-6",
}
# Report-dir search order: project-local (tmux-spawn writes here) first, central second.
# tmux_interactive_dispatch uses resolve_state_dir which lands on <REPO_ROOT>/.vnx-data/.
# provider_dispatch can write to central or project-local depending on install mode.
REPORT_DIR_CANDIDATES = (
    REPO_ROOT / ".vnx-data" / "unified_reports",
    Path.home() / ".vnx-data" / "vnx-dev" / "unified_reports",
)
# Minimum plausible wallclock for a real worker run. Anything under this is an
# immediate-exit pattern (subscription rate-limit, tmux session-create fail, etc.).
MIN_REAL_WALLCLOCK_SECONDS = 5.0


@dataclass
class DispatchResult:
    lane_id: str
    task_id: str
    replication: int
    dispatch_id: str
    success: bool
    wallclock_seconds: float
    report_path: Optional[Path]
    stdout: str
    stderr: str
    error: Optional[str] = None


def _claude_subprocess_headless(
    lane: dict, dispatch_id: str, instruction: str,
    dispatch_paths: str, deadline_seconds: int,
) -> tuple[int, str, str]:
    """Route a Claude lane via subprocess_dispatch.py (headless `claude -p`).

    Used for models where the interactive lane is known-broken (see
    HEADLESS_FORCED_MODELS). Pre-15-juni-2026 the headless `claude -p` path
    still runs on the subscription per the June-15 escape window; post-cutover
    it routes to API credits and SHOULD NOT be used for cost-sensitive bench runs.
    """
    env = {
        **os.environ,
        "VNX_STATE_DIR": ".vnx-data/state",
        "VNX_DATA_DIR": ".vnx-data",
        "VNX_DISPATCH_DIR": ".vnx-data/dispatches",
        "VNX_WORKER_SCOPED": "0",
    }
    cmd = [
        sys.executable, str(SUBPROCESS_DISPATCH),
        "--terminal-id", "T1",
        "--dispatch-id", dispatch_id,
        "--model", lane["model_arg"],
        "--role", "backend-developer",
        "--pr-id", f"BENCH-{lane['id']}",
        "--dispatch-paths", dispatch_paths,
        "--instruction", instruction,
        "--allow-unstaged",
        "--reason", f"benchmark headless (opus-4-8 interactive-hang workaround) {dispatch_id}",
    ]
    proc = subprocess.run(
        cmd, env=env, capture_output=True, text=True,
        timeout=deadline_seconds + 60, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _claude_tmux_spawn(
    lane: dict, dispatch_id: str, instruction: str,
    dispatch_paths: str, deadline_seconds: int,
) -> tuple[int, str, str]:
    """Route a Claude lane via tmux_interactive_dispatch.py on the subscription.

    Each dispatch gets a fresh ephemeral tmux session in an isolated git worktree
    (default). Interactive `claude` (never `claude -p`) keeps billing on the
    subscription per the June-15 escape (see CLAUDE.md "Tmux-Spawn Dispatch Lane").
    """
    env = {
        **os.environ,
        "VNX_STATE_DIR": ".vnx-data/state",
        "VNX_DATA_DIR": ".vnx-data",
        "VNX_DISPATCH_DIR": ".vnx-data/dispatches",
        # Bench workers need full tool surface (Skill, etc.) for representativity.
        # Ephemeral isolated worktree = bounded blast radius; safe to drop scoping.
        "VNX_WORKER_SCOPED": "0",
    }
    cmd = [
        sys.executable, str(TMUX_INTERACTIVE_DISPATCH),
        "--dispatch-id", dispatch_id,
        "--model", lane["model_arg"],
        "--role", "backend-developer",
        "--dispatch-paths", dispatch_paths,
        "--instruction", instruction,
        "--deadline-seconds", str(deadline_seconds),
        "--allow-unstaged",
        "--reason", f"benchmark run {dispatch_id}",
    ]
    proc = subprocess.run(
        cmd, env=env, capture_output=True, text=True,
        timeout=deadline_seconds + 120, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _provider_dispatch(
    lane: dict, dispatch_id: str, instruction: str,
    dispatch_paths: str, deadline_seconds: int,
) -> tuple[int, str, str]:
    """Route a non-Claude provider via provider_dispatch.py."""
    env = {
        **os.environ,
        "VNX_STATE_DIR": ".vnx-data/state",
        "VNX_DATA_DIR": ".vnx-data",
        "VNX_DISPATCH_DIR": ".vnx-data/dispatches",
    }
    provider_map = {
        "litellm:deepseek": "litellm:deepseek",
        "litellm:moonshot": "kimi",
        "litellm:zai": "litellm:zai",
        "local-gemma": "local-gemma",
    }
    provider = provider_map.get(lane["provider"], lane["provider"])
    cmd = [
        sys.executable, str(PROVIDER_DISPATCH),
        "--provider", provider,
        "--terminal-id", "headless",
        "--dispatch-id", dispatch_id,
        "--model", lane["model_arg"],
        "--role", "backend-developer",
        "--dispatch-paths", dispatch_paths,
        "--instruction", instruction,
    ]
    proc = subprocess.run(
        cmd, env=env, capture_output=True, text=True,
        timeout=deadline_seconds + 60, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def dispatch(
    lane: dict,
    task_id: str,
    replication: int,
    instruction: str,
    dispatch_paths: str,
    deadline_seconds: int,
    skill_names: Optional[list[str]] = None,
) -> DispatchResult:
    """Run a single (lane, task, replication) dispatch and return result.

    If skill_names is provided, the skill body + auto-generated resource-index
    is plain-text-prepended to the instruction for non-claude lanes
    (kimi/codex/deepseek-bare). Claude lanes already inject skills via their
    dispatcher's _inject_skill_context — skipping here avoids double-injection.
    See scripts/lib/skill_prefix.py for the architecture rationale.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dispatch_id = f"bench-{lane['id']}-{task_id}-r{replication}-{ts}"
    start = time.monotonic()

    # Plain-prepend skill prefix for non-claude lanes. Claude lanes get skills
    # via their native injection — we skip the prepend there to avoid doubling.
    if skill_names and lane["provider"] != "claude":
        instruction = inject_skill_prefix_into_instruction(
            instruction, skill_names, SKILLS_ROOT,
        )

    try:
        if lane["provider"] == "claude":
            # Check BOTH lane id and model_arg — models.yaml uses short aliases
            # for some lanes (e.g. claude-sonnet-4-6's model_arg is "sonnet"),
            # so a model_arg-only check missed sonnet on the 2026-06-05 retry.
            if lane["id"] in HEADLESS_FORCED_MODELS or lane["model_arg"] in HEADLESS_FORCED_MODELS:
                rc, out, err = _claude_subprocess_headless(
                    lane, dispatch_id, instruction, dispatch_paths, deadline_seconds,
                )
            else:
                rc, out, err = _claude_tmux_spawn(
                    lane, dispatch_id, instruction, dispatch_paths, deadline_seconds,
                )
        else:
            rc, out, err = _provider_dispatch(
                lane, dispatch_id, instruction, dispatch_paths, deadline_seconds,
            )
    except subprocess.TimeoutExpired as exc:
        wallclock = time.monotonic() - start
        return DispatchResult(
            lane_id=lane["id"], task_id=task_id, replication=replication,
            dispatch_id=dispatch_id, success=False, wallclock_seconds=wallclock,
            report_path=None, stdout="", stderr=str(exc),
            error=f"timeout after {deadline_seconds}s",
        )

    wallclock = time.monotonic() - start

    # Search for the worker-emitted report across known locations.
    report_path: Optional[Path] = None
    for candidate_dir in REPORT_DIR_CANDIDATES:
        for suffix in (".md", "_report.md"):
            p = candidate_dir / f"{dispatch_id}{suffix}"
            if p.exists():
                report_path = p
                break
        if report_path is not None:
            break

    # Distinguish three failure modes so the scorer can apply the right verdict:
    #   - immediate_exit: wallclock < threshold and no report (rate-limit, tmux fail)
    #   - no_report: ran for real but never produced report (lane bug)
    #   - rc_nonzero: dispatcher itself returned non-zero
    failure_reason: Optional[str] = None
    if rc != 0 and report_path is None:
        if wallclock < MIN_REAL_WALLCLOCK_SECONDS:
            failure_reason = (
                f"immediate_exit (wall={wallclock:.2f}s, rc={rc}); "
                "likely subscription rate-limit or session-create failure"
            )
        else:
            failure_reason = f"rc={rc} report_exists=False"
    elif rc != 0:
        failure_reason = f"rc={rc} (report present)"
    elif report_path is None:
        failure_reason = "no_report (worker rc=0 but never emitted unified_report)"

    success = failure_reason is None

    return DispatchResult(
        lane_id=lane["id"], task_id=task_id, replication=replication,
        dispatch_id=dispatch_id, success=success, wallclock_seconds=wallclock,
        report_path=report_path,
        stdout=out[-2000:], stderr=err[-2000:],
        error=failure_reason,
    )


def load_lanes(models_yaml: Path, lane_ids: list[str]) -> list[dict]:
    """Load lane configs from models.yaml, filtered to requested ids."""
    import yaml
    data = yaml.safe_load(models_yaml.read_text(encoding="utf-8"))
    by_id = {m["id"]: m for m in data["models"]}
    missing = [lid for lid in lane_ids if lid not in by_id]
    if missing:
        raise ValueError(f"Lane(s) not in models.yaml: {missing}")
    return [by_id[lid] for lid in lane_ids]
