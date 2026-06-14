"""F39 Replay Harness package.

Backwards-compatible re-exports so existing callers continue to work:
    from replay_harness import run_replay, run_chain_replay, ReplayResult, ...
"""
from __future__ import annotations

import importlib.util as _ilu
import logging
import sys
from pathlib import Path

_F39_DIR = Path(__file__).resolve().parents[1]
_SCRIPTS_LIB_DIR = Path(__file__).resolve().parents[2] / "lib"

# scripts/lib must be accessible for decision_parser and other shared modules.
if str(_SCRIPTS_LIB_DIR) not in sys.path:
    sys.path.append(str(_SCRIPTS_LIB_DIR))

# Load scripts/f39/context_assembler.py by absolute path and register it as
# "f39_context_assembler" so it never shadows the canonical scripts/lib version
# in sys.modules['context_assembler']. Without this isolation, alphabetical
# collection order causes tests/f39/ to cache the wrong module before the lib
# test files are even imported.
if "f39_context_assembler" not in sys.modules:
    _f39_ca_path = _F39_DIR / "context_assembler.py"
    _spec = _ilu.spec_from_file_location("f39_context_assembler", _f39_ca_path)
    _mod = _ilu.module_from_spec(_spec)
    sys.modules["f39_context_assembler"] = _mod
    _spec.loader.exec_module(_mod)

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
