"""F39 Replay Harness package.

Backwards-compatible re-exports so existing callers continue to work:
    from replay_harness import run_replay, run_chain_replay, ReplayResult, ...
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# Set up sys.path so scripts/f39/context_assembler takes precedence over the
# scripts/lib/context_assembler that exists under a different name.
# scripts/f39 must be at position 0; scripts/lib must be accessible for decision_parser.
_F39_DIR = Path(__file__).resolve().parents[1]
_SCRIPTS_LIB_DIR = Path(__file__).resolve().parents[2] / "lib"
if str(_SCRIPTS_LIB_DIR) not in sys.path:
    sys.path.append(str(_SCRIPTS_LIB_DIR))
if str(_F39_DIR) in sys.path:
    sys.path.remove(str(_F39_DIR))
sys.path.insert(0, str(_F39_DIR))

from .models import (  # noqa: E402
    ReplayResult,
    ChainStep,
    ChainScenario,
    ChainStepResult,
    ChainReplayResult,
)
from .prefilter import _code_prefilter, _reason_aligns  # noqa: E402
from .single_replay import (  # noqa: E402
    run_replay,
    run_all_replays,
    assemble_t0_context,
)
from .chain_replay import run_chain_replay, run_all_chain_replays  # noqa: E402
from .cli import main  # noqa: E402

log = logging.getLogger(__name__)

__all__ = [
    "run_replay",
    "run_chain_replay",
    "run_all_replays",
    "run_all_chain_replays",
    "ReplayResult",
    "ChainStep",
    "ChainScenario",
    "ChainStepResult",
    "ChainReplayResult",
    "main",
    "log",
    "assemble_t0_context",
    "_code_prefilter",
    "_reason_aligns",
]
