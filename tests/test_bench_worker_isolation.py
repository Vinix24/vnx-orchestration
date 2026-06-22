"""Parity proof for benchmark worker isolation and seed materialization."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_LIB = REPO_ROOT / "scripts" / "lib"
RUNNERS = REPO_ROOT / "scripts" / "benchmark" / "field-tests" / "runners"
sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(RUNNERS))

import lane_adapter  # noqa: E402
import provider_dispatch  # noqa: E402
from benchmark_worker_isolation import (  # noqa: E402
    BENCH_CELL_DIRNAME,
    materialize_benchmark_seed,
)
from scorer import score_cell  # noqa: E402
from tmux_interactive_dispatch import TmuxInteractiveDispatch, TmuxResult  # noqa: E402
from tmux_worktree import ReapResult, WorktreeHandle  # noqa: E402


SEED_REL = Path("tasks/trivial/seed")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _make_main_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "main"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.name", "Bench Test")
    _git(repo, "config", "user.email", "bench@example.test")
    seed = repo / SEED_REL
    seed.mkdir(parents=True)
    (seed / "input.txt").write_text("committed seed\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "seed")
    return repo


def _make_isolated_copy(main_repo: Path, destination: Path) -> Path:
    seed_dst = destination / SEED_REL
    seed_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(main_repo / SEED_REL, seed_dst)
    return destination


def _write_verify(task_folder: Path) -> None:
    task_folder.mkdir(parents=True)
    (task_folder / "verify.py").write_text(
        f"SEED_REL = {str(SEED_REL)!r}\n"
        "def verify(workdir, expected):\n"
        "    seed = workdir / SEED_REL\n"
        "    ok = (seed / 'input.txt').exists() and (seed / 'output.txt').exists()\n"
        "    return {'pass': ok, 'evidence': str(workdir), "
        "'details': {'files_written': ['output.txt'] if ok else []}}\n",
        encoding="utf-8",
    )


def test_adapter_parity_scores_both_isolated_outputs_and_keeps_main_seed_clean(
    monkeypatch,
    tmp_path,
):
    main_repo = _make_main_repo(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    task_folder = tmp_path / "task"
    _write_verify(task_folder)
    real_run = subprocess.run
    observed_cwds: dict[str, Path] = {}

    monkeypatch.setattr(lane_adapter, "REPO_ROOT", main_repo)
    monkeypatch.setattr(lane_adapter, "REPORT_DIR_CANDIDATES", (reports,))

    def fake_run(cmd, **kwargs):
        if len(cmd) > 1 and Path(str(cmd[1])).name in {
            "tmux_interactive_dispatch.py",
            "provider_dispatch.py",
        }:
            dispatch_id = cmd[cmd.index("--dispatch-id") + 1]
            is_tmux = Path(str(cmd[1])).name == "tmux_interactive_dispatch.py"
            wt_name = f"dispatch-{dispatch_id}" if is_tmux else f"provider-{dispatch_id}"
            wt = _make_isolated_copy(main_repo, main_repo / ".vnx-data" / "worktrees" / wt_name)
            worker_cwd = materialize_benchmark_seed(wt, [str(SEED_REL)])
            (worker_cwd / "output.txt").write_text("worker output\n", encoding="utf-8")
            observed_cwds["claude" if is_tmux else "provider"] = worker_cwd
            (reports / f"{dispatch_id}.md").write_text("report\n", encoding="utf-8")
            stderr = "" if is_tmux else f"VNX_PROVIDER_WORKDIR={wt}\n"
            return SimpleNamespace(returncode=0, stdout="", stderr=stderr)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(lane_adapter.subprocess, "run", fake_run)

    claude = lane_adapter.dispatch(
        lane={"id": "claude-test", "provider": "claude", "model_arg": "sonnet"},
        task_id="trivial",
        replication=1,
        instruction="write output.txt",
        dispatch_paths=str(SEED_REL),
        deadline_seconds=30,
    )
    provider = lane_adapter.dispatch(
        lane={"id": "codex-test", "provider": "codex", "model_arg": "gpt-test"},
        task_id="trivial",
        replication=1,
        instruction="write output.txt",
        dispatch_paths=str(SEED_REL),
        deadline_seconds=30,
    )

    for result in (claude, provider):
        assert result.success, result.error
        worker_cwd = observed_cwds[
            "claude" if result.lane_id == "claude-test" else "provider"
        ]
        assert result.workdir == worker_cwd.parent
        assert (result.workdir / SEED_REL / "input.txt").read_text(
            encoding="utf-8"
        ) == "committed seed\n"
        score = score_cell(
            result,
            {"tier": "t1_trivial", "deadline_seconds": 30},
            task_folder,
            "write output.txt",
            ["output.txt"],
            run_judge=False,
        )
        assert score.correctness == 5.0
        assert score.completeness == 5.0

    assert _git(main_repo, "status", "--porcelain", "--", str(SEED_REL)) == ""


def test_materialize_from_scratch_creates_empty_cell_and_symlink(tmp_path):
    # FROM-SCRATCH task (t3 07/08): the seed path does NOT exist in the worktree.
    wt = tmp_path / "wt"
    wt.mkdir()
    assert not (wt / SEED_REL).exists()

    cell = materialize_benchmark_seed(wt, [str(SEED_REL)])

    assert cell == wt / BENCH_CELL_DIRNAME
    assert cell.is_dir()
    assert not any(cell.iterdir())  # worker starts in an empty cell
    # SEED_REL is now a symlink → the cell, so verify.py's `workdir / SEED_REL` resolves.
    seed_link = wt / SEED_REL
    assert seed_link.is_symlink()
    (cell / "state_machine.py").write_text("ok", encoding="utf-8")
    assert (seed_link / "state_machine.py").read_text(encoding="utf-8") == "ok"


def test_materialize_from_scratch_refuses_main_checkout(tmp_path):
    # The .git-is-dir guard must still fire even on the from-scratch path.
    main = tmp_path / "main"
    main.mkdir()
    (main / ".git").mkdir()
    with pytest.raises(RuntimeError, match="refusing shared main checkout"):
        materialize_benchmark_seed(main, [str(SEED_REL)])


class _TmuxRunner:
    def __init__(self, receipts_file: Path, dispatch_id: str) -> None:
        self.receipts_file = receipts_file
        self.dispatch_id = dispatch_id
        self.cwd: Path | None = None
        self.pending_paste = False

    def available(self) -> bool:
        return True

    def run(self, args, **kwargs) -> TmuxResult:
        if args[0] == "new-session":
            self.cwd = Path(args[args.index("-c") + 1])
            return TmuxResult(0, "%1\n", "")
        if args[0] == "display-message":
            return TmuxResult(0, "@1\n", "")
        if args[0] == "capture-pane":
            return TmuxResult(0, "Welcome to Claude\n? for shortcuts", "")
        if args[0] == "paste-buffer":
            self.pending_paste = True
        if args[0] == "send-keys" and args[-1] == "Enter" and self.pending_paste:
            self.pending_paste = False
            assert self.cwd is not None
            (self.cwd / "output.txt").write_text("claude output\n", encoding="utf-8")
            self.receipts_file.parent.mkdir(parents=True, exist_ok=True)
            self.receipts_file.write_text(
                json.dumps(
                    {
                        "event_type": "subprocess_completion",
                        "dispatch_id": self.dispatch_id,
                        "status": "done",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        return TmuxResult(0, "", "")


def test_tmux_and_provider_dispatchers_launch_from_materialized_seed(
    monkeypatch,
    tmp_path,
    capsys,
):
    main_repo = _make_main_repo(tmp_path)
    tmux_wt = _make_isolated_copy(main_repo, tmp_path / "tmux-wt")
    provider_wt = _make_isolated_copy(main_repo, tmp_path / "provider-wt")
    receipts = tmp_path / "receipts.ndjson"
    dispatch_id = "bench-isolation-parity"
    runner = _TmuxRunner(receipts, dispatch_id)
    handle = WorktreeHandle(
        path=tmux_wt,
        branch=f"dispatch/{dispatch_id}",
        base_sha="abc123",
        base_ref="main",
        dispatch_id=dispatch_id,
    )
    lane = TmuxInteractiveDispatch(
        tmp_path / "state",
        runner=runner,
        project_root=main_repo,
        receipts_file=receipts,
    )
    monkeypatch.setenv("VNX_BENCH_SEED_MATERIALIZE", "1")
    monkeypatch.setenv("VNX_TMUX_PASTE_SETTLE_SECONDS", "0")
    monkeypatch.setenv("VNX_TMUX_SUBMIT_RETRY_DELAY", "0")

    with (
        patch("tmux_interactive_dispatch.allocate", return_value=handle),
        patch("tmux_interactive_dispatch.classify", return_value="dirty"),
        patch(
            "tmux_interactive_dispatch.reap",
            return_value=ReapResult(removed=False, preserved_path=tmux_wt),
        ),
        patch.object(lane, "_start_pipe_pane", return_value=None),
        patch.object(lane, "_govern_report", return_value=tmp_path / "tmux-report.md"),
    ):
        result = lane.dispatch(
            "write output.txt",
            dispatch_id,
            dispatch_paths=[str(SEED_REL)],
            deadline_seconds=1,
            poll_interval=0.001,
            warmup_timeout=0.1,
            warmup_poll_interval=0.001,
        )

    assert result.success, result.failure_reason
    assert runner.cwd == tmux_wt / BENCH_CELL_DIRNAME
    assert (tmux_wt / SEED_REL).resolve() == runner.cwd.resolve()
    assert (runner.cwd / "input.txt").exists()
    assert (runner.cwd / "output.txt").exists()

    args = provider_dispatch._build_parser().parse_args(
        [
            "--provider", "codex",
            "--terminal-id", "headless",
            "--dispatch-id", "bench-provider-parity",
            "--instruction", "write output.txt",
            "--dispatch-paths", str(SEED_REL),
        ]
    )
    spawn_result = SimpleNamespace(
        returncode=0,
        error=None,
        timed_out=False,
        event_writer_failures=0,
        completion_text="",
        token_usage={},
    )
    provider_cwd: dict[str, Path] = {}

    def fake_spawn(**kwargs):
        provider_cwd["path"] = Path(kwargs["cwd"])
        (provider_cwd["path"] / "output.txt").write_text("provider output\n", encoding="utf-8")
        return spawn_result

    monkeypatch.setenv("VNX_ISOLATED_WORKTREE", "1")
    monkeypatch.setenv("VNX_BENCH_REQUIRE_ISOLATION", "1")
    monkeypatch.setenv("VNX_BENCH_PRESERVE_WORKTREE", "1")
    with (
        patch("provider_dispatch._create_provider_worktree", return_value=provider_wt),
        patch("provider_dispatch._resolve_codex_model", return_value="gpt-test"),
        patch("provider_dispatch._emit_governance", side_effect=lambda *a, **k: None),
        patch("provider_dispatch._event_store_safety_net"),
        patch("event_store.EventStore", return_value=MagicMock()),
        patch("provider_spawns.codex_spawn.spawn_codex", side_effect=fake_spawn),
    ):
        assert provider_dispatch._dispatch_codex(args) == 0

    assert provider_cwd["path"] == provider_wt / BENCH_CELL_DIRNAME
    assert (provider_wt / SEED_REL).resolve() == provider_cwd["path"].resolve()
    assert (provider_cwd["path"] / "input.txt").exists()
    assert f"VNX_PROVIDER_WORKDIR={provider_wt}" in capsys.readouterr().err
    assert _git(main_repo, "status", "--porcelain", "--", str(SEED_REL)) == ""


def test_provider_isolation_failure_dnfs_loud_without_running_in_main(
    monkeypatch,
    tmp_path,
):
    main_repo = _make_main_repo(tmp_path)
    args = provider_dispatch._build_parser().parse_args(
        [
            "--provider", "codex",
            "--terminal-id", "headless",
            "--dispatch-id", "bench-provider-isolation-failure",
            "--instruction", "write output.txt",
            "--dispatch-paths", str(SEED_REL),
        ]
    )
    monkeypatch.setenv("VNX_ISOLATED_WORKTREE", "1")
    monkeypatch.setenv("VNX_BENCH_REQUIRE_ISOLATION", "1")
    spawn = MagicMock()
    with (
        patch("provider_dispatch._create_provider_worktree", return_value=None),
        patch("provider_dispatch._resolve_codex_model", return_value="gpt-test"),
        patch("event_store.EventStore", return_value=MagicMock()),
        patch("provider_spawns.codex_spawn.spawn_codex", spawn),
        pytest.raises(RuntimeError, match="refusing shared main checkout"),
    ):
        provider_dispatch._dispatch_codex(args)
    spawn.assert_not_called()

    monkeypatch.setattr(lane_adapter, "REPO_ROOT", main_repo)
    monkeypatch.setattr(lane_adapter, "REPORT_DIR_CANDIDATES", ())
    ran_dispatcher = False
    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        nonlocal ran_dispatcher
        if len(cmd) > 1 and Path(str(cmd[1])).name == "provider_dispatch.py":
            ran_dispatcher = True
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr=(
                    "RuntimeError: benchmark provider isolation required but "
                    "worktree creation failed; refusing shared main checkout"
                ),
            )
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(lane_adapter.subprocess, "run", fake_run)
    result = lane_adapter.dispatch(
        lane={"id": "codex-test", "provider": "codex", "model_arg": "gpt-test"},
        task_id="trivial",
        replication=1,
        instruction="write output.txt",
        dispatch_paths=str(SEED_REL),
        deadline_seconds=30,
    )

    assert ran_dispatcher
    assert not result.success
    assert "isolation failed" in result.error
    assert "refusing shared main checkout" in result.error
    assert not (main_repo / "output.txt").exists()
    assert _git(main_repo, "status", "--porcelain", "--", str(SEED_REL)) == ""
