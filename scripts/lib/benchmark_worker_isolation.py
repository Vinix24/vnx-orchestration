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
    """Materialize the worker's effective CWD inside an isolated worktree.

    Two task shapes, both fair (the worker always works in BENCH_CELL_DIRNAME and
    its output is reachable at the worktree's SEED_REL path):
      * SEED-BASED — the seed dir exists in the worktree: copy it into the cell.
      * FROM-SCRATCH — the seed path does NOT exist (e.g. t3 07/08 build a state
        machine / SSRF validator from nothing): start the worker in an EMPTY cell.
    In both cases the seed path is replaced by a symlink → the cell, so a task's
    verify.py (which reads `workdir / SEED_REL`) finds the worker's output
    identically whether or not a seed was provided.
    """
    if isinstance(dispatch_paths, str):
        paths = [dispatch_paths] if dispatch_paths.strip() else []
    else:
        paths = list(dispatch_paths or [])
    if len(paths) != 1:
        raise RuntimeError(
            "benchmark seed materialization requires exactly one dispatch path"
        )

    root = worktree.resolve()

    # SAFETY (harness hardening, 2026-06-18): refuse to materialize in the shared MAIN
    # checkout. This function rmtree()s the seed dir and replaces it with a symlink; if
    # `root` were ever the main checkout, that would dirty the repo's committed seed and
    # cascade into seed-invariant DNFs for every later cell (observed in the 2026-06-17
    # run — a worker ran with CWD=main and leaked its output into
    # scripts/benchmark/.../seed/).
    #
    # Discriminator: a git worktree's `.git` is a FILE (a gitdir pointer); only the main
    # checkout has `.git` as a DIRECTORY. So `.git`-is-dir == "this is the shared main
    # checkout" and we reject it fail-loud — a worker can NEVER touch the shared checkout.
    if (root / ".git").is_dir():
        raise RuntimeError(
            f"benchmark seed materialization refused: {root} is the shared main checkout "
            "(.git is a directory, not a worktree pointer) — refusing shared main checkout"
        )

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

    worker_cwd = root / BENCH_CELL_DIRNAME
    if worker_cwd.exists():
        raise RuntimeError(f"benchmark worker CWD already exists: {worker_cwd}")

    if seed_dir.is_dir():
        # SEED-BASED: copy the committed seed into the cell, then replace the seed
        # location with a symlink so verify.py's `workdir / SEED_REL` resolves there.
        shutil.copytree(seed_dir, worker_cwd)
        shutil.rmtree(seed_dir)
    elif seed_dir.exists():
        raise RuntimeError(
            f"benchmark seed path exists but is not a directory: {paths[0]!r}"
        )
    else:
        # FROM-SCRATCH: no seed to copy — the worker builds everything itself.
        # Start it in an EMPTY cell and create the seed path's parent so the
        # SEED_REL symlink can be planted (verify.py still reads workdir/SEED_REL).
        worker_cwd.mkdir(parents=True)
        seed_dir.parent.mkdir(parents=True, exist_ok=True)

    seed_dir.symlink_to(
        os.path.relpath(worker_cwd, seed_dir.parent),
        target_is_directory=True,
    )
    return worker_cwd
