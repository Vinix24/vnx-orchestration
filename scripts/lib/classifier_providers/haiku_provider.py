"""Claude Haiku provider via the local `claude` CLI (subprocess only).

Every call starts ``claude --print`` with a fresh ``--session-id`` and books its
one cost-ledger line on the native ``claude`` lane (K4b-1, ADR-043): the
transcript of that session decides the tokens and the record's
``api_equivalent_usd`` carries the list price. The subscription lane costs 0 at
the margin, so the record's ``cost_usd`` is 0; there is no flat estimate.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from .base import ClassifierProvider, ClassifierResult, parse_json_block

_DEFAULT_MODEL = "claude-haiku-4-5"
_DEFAULT_TIMEOUT = 60


class HaikuProvider(ClassifierProvider):
    """Run classification via `claude --print --model claude-haiku-4-5`."""

    name = "haiku"
    reachability_key = "claude"
    ledger_provider = "claude"

    def __init__(
        self,
        model: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
    ) -> None:
        self.model = model or os.environ.get("VNX_HAIKU_MODEL", _DEFAULT_MODEL)
        self.timeout_seconds = int(
            timeout_seconds
            if timeout_seconds is not None
            else os.environ.get("VNX_HAIKU_TIMEOUT", _DEFAULT_TIMEOUT)
        )

    def is_present(self) -> bool:
        return shutil.which("claude") is not None

    def classify(self, prompt: str, _max_tokens: int = 1500) -> ClassifierResult:
        started = datetime.now(timezone.utc)
        session_id = str(uuid.uuid4())
        cmd = ["claude", "--print", "--session-id", session_id, "--model", self.model]
        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError as exc:
            return self.finish_run(session_id=session_id, model=self.model, started=started,
                                   monotonic_start=start, spawn_refused=f"claude_cli_missing: {exc}",
                                   error=f"claude CLI not found: {exc}")
        except subprocess.TimeoutExpired as exc:
            return self.finish_run(session_id=session_id, model=self.model, started=started,
                                   monotonic_start=start, error=f"timeout after {self.timeout_seconds}s: {exc}")
        raw = proc.stdout or ""
        extra = {"model": self.model}
        if proc.returncode != 0:
            return self.finish_run(session_id=session_id, model=self.model, started=started,
                                   monotonic_start=start, raw_response=raw, extra=extra,
                                   error=f"exit {proc.returncode}: {(proc.stderr or '').strip()[:500]}")
        return self.finish_run(session_id=session_id, model=self.model, started=started, monotonic_start=start,
                               raw_response=raw, parsed_json=parse_json_block(raw), extra=extra)
