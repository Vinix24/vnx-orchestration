"""build_decisions_digest.py — Phase-20 decisions-first digest orchestrator.

1.0 scope: progress-only path. D3-D5 collectors wired in 1.0.1.

Usage:
  python3 scripts/build_decisions_digest.py

Env vars:
  VNX_STATE_DIR  — state directory (default: resolved centrally via vnx_paths →
                   ~/.vnx-data/<project>/state; do not pin repo-local)
  VNX_DATA_DIR   — data root directory (default: resolved centrally via vnx_paths →
                   ~/.vnx-data/<project>)
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Always insert scripts/lib at position 0 so the digest library package wins
# over any test-package shadow (pytest adds tests/ to sys.path for
# tests/digest/__init__.py, caching tests/digest as sys.modules['digest']).
_LIB = Path(__file__).resolve().parent / "lib"
sys.path.insert(0, str(_LIB))

from digest.collectors.progress import collect_progress
from digest.io import write_digest_output
from digest.renderer import render_minimal_digest

logger = logging.getLogger(__name__)


def _canonical_paths() -> dict | None:
    """Resolve VNX_STATE_DIR/VNX_DATA_DIR via canonical vnx_paths (central-mode aware)."""
    try:
        from vnx_paths import resolve_paths
        return resolve_paths()
    except Exception:
        return None


def _default_state_dir() -> Path:
    env = os.environ.get("VNX_STATE_DIR")
    if env:
        return Path(env)
    # A bare ".vnx-data/state" default resolves relative to CWD, ignoring the
    # central ~/.vnx-data/<project>/state location. See #1023.
    canonical = _canonical_paths()
    return Path(canonical["VNX_STATE_DIR"]) if canonical else Path(".vnx-data/state")


def _default_data_dir() -> Path:
    env = os.environ.get("VNX_DATA_DIR")
    if env:
        return Path(env)
    canonical = _canonical_paths()
    return Path(canonical["VNX_DATA_DIR"]) if canonical else Path(".vnx-data")


def render_decisions_digest(
    state_dir: Path | None = None,
    data_dir: Path | None = None,
) -> str:
    """Orchestrate collectors -> renderer -> markdown string.

    Called by main() and tests. Raises ValueError when state_dir resolves
    to a non-directory path (sanity guard only; missing files are tolerated).
    """
    if state_dir is None:
        state_dir = _default_state_dir()
    if data_dir is None:
        data_dir = _default_data_dir()

    progress = collect_progress(state_dir=state_dir, data_dir=data_dir)
    return render_minimal_digest(progress=progress, manual_decisions=None)


def main() -> int:
    """Write digest to state_dir/decisions_digest.md atomically.

    Returns 0 on success. Raises ValueError on invalid env configuration.
    OSError from write propagates (ADR-021: never silently drop).
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    state_dir = _default_state_dir()
    data_dir = _default_data_dir()

    content = render_decisions_digest(state_dir=state_dir, data_dir=data_dir)
    output_path = state_dir / "decisions_digest.md"
    write_digest_output(content, output_path)
    logger.info("digest written to %s", output_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
