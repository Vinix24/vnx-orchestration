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

import os
import shutil
import subprocess
import sys
import tempfile
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

# Skill injection lives in scripts/lib/skill_prefix.py — provider-agnostic.
# Used for ALL lanes (claude/kimi/codex/deepseek) so every worker gets the
# same structured prompt: role → assignment → resources → closing.
# Dispatcher-side enrichment is disabled for benchmark dispatches via
# VNX_BENCH_EQUAL_CONTEXT so this structured prompt is the single context source.
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
from skill_prefix import build_structured_prompt  # noqa: E402
from benchmark_worker_isolation import BENCH_CELL_DIRNAME  # noqa: E402

# Models where the interactive Claude Code lane is known-broken (hidden-thinking
# loops + interactive-session hangs per Anthropic GitHub #63390 and #64153).
# Route these via headless subprocess_dispatch instead, which exercises the same
# subscription pre-15-juni and bypasses the interactive-session bug.
#
# Expanded 2026-06-05 after retry-run observed opus-4-7 + sonnet-4-6 hitting
# the same 0.1s immediate-exit pattern on T3-09 instruction content (#63390/#64153).
#
# 2026-06-15 EMPIRICAL RE-TEST (cutover day — headless = API billing is now
# disallowed per the June-15 subscription escape): opus-4-8 / opus-4-7 /
# sonnet-4-6 each completed a trivial task via the tmux interactive (subscription)
# lane in ~39s, success=true, clean teardown — the #63390 hidden-thinking hang did
# NOT reproduce on the current `claude` CLI for trivial/medium work. Emptied so ALL
# Claude lanes route via tmux (subscription). Mechanism retained: re-add a model
# here ONLY if a t3-complex (>1h) run empirically hangs again (the original hang
# was on a 3h T3 task, so t3 is the residual risk to watch).
HEADLESS_FORCED_MODELS: set[str] = set()
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
    # Where the worker's isolated, materialized seed output lives for verify.py.
    workdir: Optional[Path] = None
    # Temp checkout created purely for scoring (branch survived, worktree
    # reaped). The runner must remove it after score_cell.
    scoring_worktree: Optional[Path] = None


def _claude_subprocess_headless(
    lane: dict, dispatch_id: str, instruction: str,
    dispatch_paths: str, deadline_seconds: int,
    role: str = "backend-developer",
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
        "VNX_BENCH_EQUAL_CONTEXT": "1",
        "VNX_BENCH_SEED_MATERIALIZE": "1",
        "VNX_ISOLATED_WORKTREE": "1",
        "VNX_BENCH_REQUIRE_ISOLATION": "1",   # fail-loud on isolation failure; never run a worker in the shared checkout
    }
    cmd = [
        sys.executable, str(SUBPROCESS_DISPATCH),
        "--terminal-id", "T1",
        "--dispatch-id", dispatch_id,
        "--model", lane["model_arg"],
        "--role", role,
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
    role: str = "backend-developer",
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
        "VNX_BENCH_EQUAL_CONTEXT": "1",
        "VNX_BENCH_SEED_MATERIALIZE": "1",
        "VNX_ISOLATED_WORKTREE": "1",
        "VNX_BENCH_REQUIRE_ISOLATION": "1",   # fail-loud on isolation failure; never run a worker in the shared checkout
    }
    cmd = [
        sys.executable, str(TMUX_INTERACTIVE_DISPATCH),
        "--dispatch-id", dispatch_id,
        "--model", lane["model_arg"],
        "--role", role,
        "--dispatch-paths", dispatch_paths,
        "--instruction", instruction,
        "--deadline-seconds", str(deadline_seconds),
        "--allow-unstaged",
        "--reason", f"benchmark run {dispatch_id}",
        "--isolated-worktree",
    ]
    proc = subprocess.run(
        cmd, env=env, capture_output=True, text=True,
        timeout=deadline_seconds + 120, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _resolve_tmux_workdir(
    dispatch_id: str,
) -> tuple[Optional[Path], Optional[Path], Optional[str]]:
    """Locate a tmux-spawn worker's output for scoring.

    tmux workers run in an ephemeral isolated worktree. After teardown
    (tmux_worktree.reap) the output lives in one of two places:

      dirty     → worktree preserved on disk           → score there
      committed → worktree gone, branch dispatch/<id>  → temp checkout
      clean     → nothing survived                     → unscorable

    Returns (workdir, temp_scoring_worktree, error). temp_scoring_worktree
    is set when this function created a checkout the caller must remove
    after scoring. On error the cell must be marked unscorable — falling
    back to the repo root would score the wrong checkout (PR #831).
    """
    wt = REPO_ROOT / ".vnx-data" / "worktrees" / f"dispatch-{dispatch_id}"
    if wt.is_dir():
        worker_cwd = wt / BENCH_CELL_DIRNAME
        if worker_cwd.is_dir():
            return wt, None, None
        return None, None, (
            f"tmux benchmark output missing materialized worker CWD: {worker_cwd}"
        )

    branch = f"dispatch/{dispatch_id}"
    probe = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--verify", "--quiet", branch],
        capture_output=True, text=True, check=False,
    )
    if probe.returncode == 0:
        tmp = Path(tempfile.mkdtemp(prefix=f"score-{dispatch_id[:48]}-"))
        added = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "worktree", "add", "--detach", str(tmp), branch],
            capture_output=True, text=True, check=False,
        )
        if added.returncode == 0:
            worker_cwd = tmp / BENCH_CELL_DIRNAME
            if worker_cwd.is_dir():
                return tmp, tmp, None
            subprocess.run(
                ["git", "-C", str(REPO_ROOT), "worktree", "remove", "--force", str(tmp)],
                capture_output=True, text=True, check=False,
            )
            return None, None, (
                f"tmux committed output missing materialized worker CWD: {worker_cwd}"
            )
        shutil.rmtree(tmp, ignore_errors=True)
        return None, None, (
            f"scoring-worktree add failed for {branch}: "
            f"{(added.stderr or '').strip()[:200]}"
        )

    return None, None, (
        "tmux worker output unlocatable (no preserved worktree, no dispatch branch)"
    )


def _resolve_codex_bin_dir() -> Optional[str]:
    """Find the dir containing the `codex` binary.

    codex is npm-global-installed under a specific nvm node version; an nvm
    default-switch (v20 -> v22) drops it from the active PATH. We probe known
    nvm node-version bins so the bench works regardless of which node is
    currently default. Returns the dir to prepend to PATH, or None if codex
    is already resolvable.
    """
    if shutil.which("codex"):
        return None
    nvm_root = Path.home() / ".nvm" / "versions" / "node"
    if nvm_root.is_dir():
        # Newest version first, but any with codex wins.
        for version_dir in sorted(nvm_root.iterdir(), reverse=True):
            candidate = version_dir / "bin" / "codex"
            if candidate.exists():
                return str(candidate.parent)
    return None


def _provider_dispatch(
    lane: dict, dispatch_id: str, instruction: str,
    dispatch_paths: str, deadline_seconds: int,
    role: str = "backend-developer",
) -> tuple[int, str, str]:
    """Route a non-Claude provider via provider_dispatch.py."""
    env = {
        **os.environ,
        "VNX_STATE_DIR": ".vnx-data/state",
        "VNX_DATA_DIR": ".vnx-data",
        "VNX_DISPATCH_DIR": ".vnx-data/dispatches",
        "VNX_BENCH_EQUAL_CONTEXT": "1",
        "VNX_ISOLATED_WORKTREE": "1",
        "VNX_BENCH_REQUIRE_ISOLATION": "1",
        "VNX_BENCH_SEED_MATERIALIZE": "1",
        "VNX_BENCH_PRESERVE_WORKTREE": "1",
        "VNX_UNIFIED_ENVELOPE": "0",
        # Agentic tool-loop for litellm lanes (GLM via OpenRouter): the model must
        # write files / run tests itself, else deliverable tasks score correctness 0
        # by construction. Ignored by non-litellm providers (kimi/codex/deepseek-harness).
        "VNX_LITELLM_AGENTIC": "1",
        # claude -p benchmark deadline (only the claude-headless path reads this).
        "VNX_BENCH_CLAUDE_DEADLINE": str(deadline_seconds),
        # Worktrees OUTSIDE the main repo: an unsandboxed worker (claude -p / deepseek-harness)
        # can't reach the main checkout via repo-relative navigation, so from-scratch /
        # introspection tasks (t3 07/08/09, t4) can't leak into the committed seed.
        "VNX_BENCH_WORKTREE_ROOT": str(Path.home() / ".vnx-bench-worktrees"),
        # Base worktrees on the bench checkout's HEAD so they carry the bench branch's
        # committed task seeds (e.g. the seed-based t4_02 SWE-bench task), without merging
        # WIP benchmark tasks to origin/main.
        "VNX_BENCH_WORKTREE_BASE_REF": "HEAD",
    }
    provider_map = {
        "litellm:deepseek": "litellm:deepseek",
        "litellm:moonshot": "kimi",
        "litellm:zai": "litellm:zai",
        "local-gemma": "local-gemma",
    }
    provider = provider_map.get(lane["provider"], lane["provider"])

    # Codex specifics: _dispatch_codex reads VNX_CODEX_MODEL (it ignores
    # --model), and codex_wrapper invokes bare "codex" (PATH-dependent).
    # Pin the model from the lane and ensure the binary resolves.
    if provider == "codex":
        env["VNX_CODEX_MODEL"] = lane["model_arg"]
        codex_bin_dir = _resolve_codex_bin_dir()
        if codex_bin_dir:
            env["PATH"] = codex_bin_dir + os.pathsep + env.get("PATH", "")
    # kimi: dispatched via the CLI OAuth (managed:kimi-code), which serves exactly
    # ONE model — K2.7-Code (kimi-code/kimi-for-coding, the CLI default), reached by
    # passing NO -m. An explicit -m for any other id is rejected by both the
    # registry constraint and the CLI OAuth endpoint. So VNX_KIMI_MODEL is left
    # unset and the lane runs the CLI default (K2.7-Code). kimi-via-cli-only compliant.
    cmd = [
        sys.executable, str(PROVIDER_DISPATCH),
        "--provider", provider,
        "--terminal-id", "headless",
        "--dispatch-id", dispatch_id,
        "--model", lane["model_arg"],
        "--role", role,
        "--dispatch-paths", dispatch_paths,
        "--instruction", instruction,
        "--no-auto-commit",
    ]
    proc = subprocess.run(
        cmd, env=env, capture_output=True, text=True,
        timeout=deadline_seconds + 60, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _resolve_provider_workdir(
    stdout: str,
    stderr: str,
) -> tuple[Optional[Path], Optional[str]]:
    """Read the benchmark provider isolation worktree emitted by provider_dispatch."""
    prefix = "VNX_PROVIDER_WORKDIR="
    for line in reversed((stdout + "\n" + stderr).splitlines()):
        if not line.startswith(prefix):
            continue
        workdir = Path(line[len(prefix):].strip()).resolve()
        if workdir == REPO_ROOT.resolve():
            return None, "provider workdir resolved to shared main checkout"
        if workdir.is_dir() and (workdir / BENCH_CELL_DIRNAME).is_dir():
            return workdir, None
        return None, f"provider benchmark output missing at {workdir}"
    return None, "provider isolation workdir marker missing"


def _main_seed_status(dispatch_paths: str) -> tuple[str, Optional[str]]:
    """Return porcelain status for benchmark seed paths in the main checkout."""
    paths = [p.strip() for p in dispatch_paths.split(",") if p.strip()]
    if not paths:
        return "", None
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--", *paths],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        return "", (
            "main-repo seed invariant check failed: "
            f"{(proc.stderr or proc.stdout).strip()[:300]}"
        )
    return proc.stdout.strip(), None


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

    If skill_names is provided, the instruction is wrapped in a structured
    prompt (role → assignment → resources → closing) for EVERY lane —
    claude, kimi, codex, deepseek alike. Provider-agnostic single source of
    truth. See scripts/lib/skill_prefix.py for the architecture rationale.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dispatch_id = f"bench-{lane['id']}-{task_id}-r{replication}-{ts}"
    start = time.monotonic()

    seed_status, seed_status_err = _main_seed_status(dispatch_paths)
    if seed_status_err or seed_status:
        error = seed_status_err or (
            "main-repo seed paths dirty before dispatch; refusing benchmark cell: "
            f"{seed_status[:300]}"
        )
        return DispatchResult(
            lane_id=lane["id"], task_id=task_id, replication=replication,
            dispatch_id=dispatch_id, success=False, wallclock_seconds=0.0,
            report_path=None, stdout="", stderr="", error=error,
        )

    # Uniform skill injection for ALL lanes (per Vincent 2026-06-05).
    # Place T0's instruction in the middle of the role/SOP/resources frame.
    if skill_names:
        instruction = build_structured_prompt(
            skill_names, instruction, SKILLS_ROOT,
        )
    role = next((name for name in skill_names or [] if name), "backend-developer")

    # Equal-context strips the skill's completion protocol. Providers exit on
    # completion (provider_dispatch reads the exit code), but the INTERACTIVE
    # Claude tmux lane never exits — it needs an explicit completion signal or it
    # DNFs at the deadline. Append a UNIFORM completion instruction (identical for
    # every lane → fairness preserved) pointing at the exact absolute report path
    # that BOTH the tmux lane's PR-8 report-backstop and this adapter's report
    # search poll. The worker writes the 4-heading report there on completion.
    _report_sink = (REPORT_DIR_CANDIDATES[0] / f"{dispatch_id}.md") if REPORT_DIR_CANDIDATES else None
    if _report_sink is not None:
        _report_sink.parent.mkdir(parents=True, exist_ok=True)
        instruction = (
            instruction
            + "\n\n# COMPLETION PROTOCOL (required)\n"
            + "When the assignment is fully done and verified, write a brief completion "
            + "report to this EXACT absolute path:\n"
            + f"  {_report_sink}\n"
            + "The report MUST contain these markdown headings: `## Summary`, `## Changes`, "
            + "`## Verification`, `## Open Items`. The harness detects completion from this "
            + "file; without it the run is recorded as a DNF even if your work is correct.\n"
        )

    via_tmux = False
    # VNX_BENCH_CLAUDE_HEADLESS=1 (operator-authorized `claude -p` for the benchmark):
    # route claude through provider_dispatch --provider claude, which materializes the
    # cell + runs `claude -p` (spawn_claude) + emits a governed report — same path as the
    # other provider lanes, scored via VNX_PROVIDER_WORKDIR. Avoids the tmux warmup-miss.
    _claude_headless_p = (
        lane["provider"] == "claude" and os.environ.get("VNX_BENCH_CLAUDE_HEADLESS") == "1"
    )
    via_provider = lane["provider"] != "claude" or _claude_headless_p
    try:
        if _claude_headless_p:
            rc, out, err = _provider_dispatch(
                lane, dispatch_id, instruction, dispatch_paths, deadline_seconds, role,
            )
        elif lane["provider"] == "claude":
            # Check BOTH lane id and model_arg — models.yaml uses short aliases
            # for some lanes (e.g. claude-sonnet-4-6's model_arg is "sonnet"),
            # so a model_arg-only check missed sonnet on the 2026-06-05 retry.
            if lane["id"] in HEADLESS_FORCED_MODELS or lane["model_arg"] in HEADLESS_FORCED_MODELS:
                rc, out, err = _claude_subprocess_headless(
                    lane, dispatch_id, instruction, dispatch_paths, deadline_seconds, role,
                )
            else:
                via_tmux = True
                rc, out, err = _claude_tmux_spawn(
                    lane, dispatch_id, instruction, dispatch_paths, deadline_seconds, role,
                )
        else:
            rc, out, err = _provider_dispatch(
                lane, dispatch_id, instruction, dispatch_paths, deadline_seconds, role,
            )
    except subprocess.TimeoutExpired as exc:
        wallclock = time.monotonic() - start
        seed_status, seed_status_err = _main_seed_status(dispatch_paths)
        invariant_error = seed_status_err or (
            f"main-repo seed contamination after timeout: {seed_status[:300]}"
            if seed_status else None
        )
        return DispatchResult(
            lane_id=lane["id"], task_id=task_id, replication=replication,
            dispatch_id=dispatch_id, success=False, wallclock_seconds=wallclock,
            report_path=None, stdout="", stderr=str(exc),
            error=invariant_error or f"timeout after {deadline_seconds}s",
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
        if (
            "benchmark provider isolation required" in err
            or "benchmark seed materialization" in err
        ):
            failure_reason = (
                "provider isolation failed; refusing shared main checkout: "
                f"{err.strip()[-300:]}"
            )
        elif wallclock < MIN_REAL_WALLCLOCK_SECONDS:
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

    # Locate the worker's actual materialized seed output for verify.py.
    workdir: Optional[Path] = None
    scoring_worktree: Optional[Path] = None
    if via_tmux and failure_reason is None:
        workdir, scoring_worktree, workdir_err = _resolve_tmux_workdir(dispatch_id)
        if workdir_err:
            failure_reason = f"unscorable: {workdir_err}"
    elif via_provider and failure_reason is None:
        workdir, workdir_err = _resolve_provider_workdir(out, err)
        if workdir_err:
            failure_reason = f"unscorable: {workdir_err}"

    seed_status, seed_status_err = _main_seed_status(dispatch_paths)
    if seed_status_err:
        failure_reason = seed_status_err
    elif seed_status:
        failure_reason = (
            "main-repo seed contamination after dispatch: "
            f"{seed_status[:300]}"
        )

    success = failure_reason is None

    return DispatchResult(
        lane_id=lane["id"], task_id=task_id, replication=replication,
        dispatch_id=dispatch_id, success=success, wallclock_seconds=wallclock,
        report_path=report_path,
        stdout=out[-2000:], stderr=err[-2000:],
        error=failure_reason,
        workdir=workdir,
        scoring_worktree=scoring_worktree,
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
