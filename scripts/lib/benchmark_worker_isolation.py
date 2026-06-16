"""Benchmark-only seed materialization inside an isolated worker worktree."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


BENCH_CELL_DIRNAME = ".vnx-benchmark-cell"


def materialize_benchmark_seed(
    worktree: Path,
    dispatch_paths: "list[str] | str | None",
) -> Path:
    """Copy the single committed benchmark seed into the worker's effective CWD."""
    if isinstance(dispatch_paths, str):
        paths = [dispatch_paths] if dispatch_paths.strip() else []
    else:
        paths = list(dispatch_paths or [])
    if len(paths) != 1:
        raise RuntimeError(
            "benchmark seed materialization requires exactly one dispatch path"
        )

    root = worktree.resolve()
    raw_path = Path(paths[0])
    if raw_path.is_absolute() or ".." in raw_path.parts:
        raise RuntimeError(f"benchmark seed path must be repo-relative: {paths[0]!r}")

    seed_dir = (root / raw_path).resolve()
    try:
        seed_dir.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(
            f"benchmark seed path escapes isolated worktree: {paths[0]!r}"
        ) from exc
    if not seed_dir.is_dir():
        raise RuntimeError(
            f"benchmark seed missing from isolated worktree: {paths[0]!r}"
        )

    worker_cwd = root / BENCH_CELL_DIRNAME
    if worker_cwd.exists():
        raise RuntimeError(f"benchmark worker CWD already exists: {worker_cwd}")
    shutil.copytree(seed_dir, worker_cwd)
    shutil.rmtree(seed_dir)
    seed_dir.symlink_to(
        os.path.relpath(worker_cwd, seed_dir.parent),
        target_is_directory=True,
    )
    return worker_cwd
