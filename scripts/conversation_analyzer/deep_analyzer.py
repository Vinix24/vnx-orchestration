"""Phase 3: LLM-powered deep analysis of flagged sessions."""

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .models import (
    SessionMetrics, SessionFlags,
    LLM_STRATEGY, OLLAMA_MODEL, DEEPSEEK_HARNESS_MODEL,
    AUTO_CLAUSE_MAX_SESSIONS,
    DEEP_THRESHOLD_TOKENS, DEEP_THRESHOLD_TOOLS,
    log,
)
from .detector import HeuristicDetector
from provider_spawns.deepseek_harness_spawn import (
    build_harness_env,
    build_harness_cli_args,
    DEEPSEEK_API_KEY_ENV,
)


@dataclass(frozen=True)
class ClaudeOutcome:
    """Result of one ``claude`` CLI invocation, with the failure mode explicit.

    ``status`` is one of:
      - ``ok``          — CLI succeeded (rc 0) and produced usable text
      - ``empty``       — CLI succeeded (rc 0) but produced no usable text
      - ``missing_cli`` — claude binary absent from PATH (FileNotFoundError)
      - ``cli_failed``  — claude exited non-zero (``returncode`` + ``stderr``)
      - ``timeout``     — claude exceeded the 90s deadline
      - ``error``       — any other unexpected exception

    ``text`` carries the assistant output for ``ok`` (and the raw stdout for
    ``empty``, kept for diagnostics). A caller can tell all six apart without
    parsing logs — the gap the old ``Optional[str]`` return left open, where a
    missing CLI, a crashed CLI, and a successful-but-empty run all collapsed to
    ``None`` (OI-1258).
    """

    status: str
    text: Optional[str] = None
    returncode: Optional[int] = None
    stderr: str = ""


class DeepAnalyzer:
    """LLM-based deep analysis of flagged sessions."""

    ANALYSIS_CATEGORIES = [
        "prompt", "hook", "template", "skill", "workflow", "architecture"
    ]

    # Ollama availability probe: probed once per process, cached for the
    # lifetime of the class. None = not probed yet, True/False = result.
    _ollama_probed: Optional[bool] = None

    # Claude-auto guard: set by the runner before processing sessions to
    # inform the billing guard of the backlog size.
    _session_backlog: int = 0

    def __init__(self) -> None:
        # OI-1258: per-run attempt accounting, distinct from the success-only
        # ``sessions_deep`` counter. ``deep_attempts`` counts sessions where an
        # LLM was actually invoked; ``deep_failures`` counts those that produced
        # no usable result. Together they make a ``deep_analyzed=0`` digest
        # interpretable: 0 attempts ("nothing was tried") vs N attempts with N
        # failures ("everything failed"). Strategy-agnostic on purpose — the
        # digest counter spans claude, deepseek-harness, and ollama paths.
        self.deep_attempts = 0
        self.deep_failures = 0

    SYSTEM_PROMPT = """You are a VNX orchestration system analyst. Analyze this Claude Code session summary and extract actionable improvement suggestions.

For each suggestion, specify:
- category: one of "prompt", "hook", "template", "skill", "workflow", "architecture"
- component: the specific VNX component (e.g. "dispatcher_v8", "receipt_processor", "gather_intelligence")
- current_behavior: what happens now
- suggested_improvement: what should change
- evidence: concrete evidence from the session
- priority: "critical", "high", "medium", or "low"

Respond with valid JSON:
{
  "patterns": ["list of successful patterns observed"],
  "bottlenecks": ["list of bottlenecks or inefficiencies"],
  "suggestions": [
    {
      "category": "prompt",
      "component": "component_name",
      "current_behavior": "...",
      "suggested_improvement": "...",
      "evidence": "...",
      "priority": "medium"
    }
  ]
}"""

    def should_deep_analyze(self, metrics: SessionMetrics,
                            flags: SessionFlags) -> bool:
        if flags.has_error_recovery:
            return True
        if metrics.total_output_tokens > DEEP_THRESHOLD_TOKENS:
            return True
        if flags.has_context_reset:
            return True
        if metrics.tool_calls_total > DEEP_THRESHOLD_TOOLS:
            return True
        return False

    def analyze_session(self, jsonl_path: Path,
                        metrics: SessionMetrics,
                        flags: SessionFlags) -> Optional[dict]:
        summary = self._build_session_summary(jsonl_path, metrics, flags)
        prompt = f"{self.SYSTEM_PROMPT}\n\n## Session Summary\n\n{summary}"

        result_text = None
        # ``attempted`` records whether an LLM was actually invoked this
        # session. It stays False for the billing-guard skip (no LLM called),
        # so a deliberate skip never counts as a failed attempt.
        attempted = False

        # deepseek-harness: own-key auth via the claude CLI driving DeepSeek's
        # Anthropic-compatible endpoint.  Fails closed when DEEPSEEK_API_KEY is
        # unset — never falls back to the OAuth subscription (constraint
        # deepseek-harness-subscription-blocked).
        if LLM_STRATEGY == "deepseek-harness":
            attempted = True
            result_text = self._try_deepseek_harness(prompt)
        # Billing guard: in "auto" mode, refuse the claude path when the
        # session backlog exceeds the threshold. "claude-only" bypasses
        # the guard — the operator explicitly opted in to metered spend.
        elif LLM_STRATEGY == "claude-only":
            attempted = True
            outcome = self._try_claude_max(prompt)
            result_text = outcome.text if outcome.status == "ok" else None
        elif LLM_STRATEGY == "auto":
            if self._session_backlog <= AUTO_CLAUSE_MAX_SESSIONS:
                attempted = True
                outcome = self._try_claude_max(prompt)
                result_text = outcome.text if outcome.status == "ok" else None
            else:
                if self._session_backlog > 0:
                    log("WARNING",
                        f"auto: skipping claude path — {self._session_backlog} "
                        f"session backlog exceeds AUTO_CLAUSE_MAX_SESSIONS "
                        f"({AUTO_CLAUSE_MAX_SESSIONS}). "
                        f"Use VNX_ANALYZER_LLM=claude-only to override.")

        if result_text is None and LLM_STRATEGY in ("auto", "ollama-only"):
            attempted = True
            result_text = self._try_ollama(prompt)

        if attempted:
            self.deep_attempts += 1

        if result_text is None:
            if attempted:
                self.deep_failures += 1
            log("WARNING", "No LLM available for deep analysis")
            return None

        parsed = self._parse_response(result_text)
        if parsed is None:
            # The LLM answered but nothing parseable came back — an attempt
            # that produced no usable result, distinct from a hard failure.
            self.deep_failures += 1
        return parsed

    @classmethod
    def set_session_backlog(cls, count: int):
        """Inform the billing guard of the unanalyzed session count.

        Called by the runner before processing begins. When LLM_STRATEGY is
        "auto" and the count exceeds AUTO_CLAUSE_MAX_SESSIONS, the claude
        path is refused to prevent metered-spend landmines.
        """
        cls._session_backlog = count

    @classmethod
    def _probe_ollama(cls) -> bool:
        """Probe whether Ollama is available with a usable model.

        Issues one GET /api/tags with a 3s timeout. Result is cached for the
        process lifetime — this runs at most once, regardless of session count.
        Returns False when the server is unreachable, has no models, or the
        configured OLLAMA_MODEL is absent.
        """
        if cls._ollama_probed is not None:
            return cls._ollama_probed

        try:
            import urllib.request
            req = urllib.request.Request(
                "http://localhost:11434/api/tags",
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                models = body.get("models", [])
                if not models:
                    log("WARNING", "Ollama server reachable but has zero models installed; "
                                   "deep analysis will be skipped for this run")
                    cls._ollama_probed = False
                    return False
                model_names = {m.get("name", "") for m in models}
                if OLLAMA_MODEL not in model_names:
                    log("WARNING",
                        f"Ollama model '{OLLAMA_MODEL}' not found in installed models "
                        f"({', '.join(sorted(model_names)[:5])}"
                        f"{'...' if len(model_names) > 5 else ''}); "
                        f"deep analysis will be skipped for this run")
                    cls._ollama_probed = False
                    return False
                log("INFO", f"Ollama probe OK: model '{OLLAMA_MODEL}' available "
                            f"({len(models)} model(s) installed)")
                cls._ollama_probed = True
                return True
        except Exception as e:
            log("WARNING", f"Ollama probe failed ({e}); "
                           f"deep analysis will be skipped for this run")
            cls._ollama_probed = False
            return False

    def _build_session_summary(self, jsonl_path: Path,
                               metrics: SessionMetrics,
                               flags: SessionFlags) -> str:
        lines = [
            f"Session: {metrics.session_id}",
            f"Model: {metrics.session_model or 'unknown'}",
            f"Terminal: {metrics.terminal}",
            f"Project: {metrics.project_path}",
            f"Date: {metrics.session_date}",
            f"Duration: {metrics.duration_minutes} min",
            f"Tokens: {metrics.total_input_tokens:,} in / {metrics.total_output_tokens:,} out",
            f"Cache: {metrics.cache_read_tokens:,} read / {metrics.cache_creation_tokens:,} create",
            f"Tools: {metrics.tool_calls_total} total (Read={metrics.tool_read_count}, "
            f"Edit={metrics.tool_edit_count}, Bash={metrics.tool_bash_count}, "
            f"Grep={metrics.tool_grep_count}, Write={metrics.tool_write_count}, "
            f"Task={metrics.tool_task_count})",
            f"Activity: {flags.primary_activity}",
            f"Flags: error_recovery={flags.has_error_recovery}, "
            f"context_reset={flags.has_context_reset}, "
            f"large_refactor={flags.has_large_refactor}, "
            f"test_cycle={flags.has_test_cycle}",
            "",
            "## Key Messages (first/last 20 user messages):",
        ]

        user_messages = []
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    record = json.loads(line.strip())
                    if record.get("type") == "user":
                        content = record.get("message", {}).get("content", "")
                        text = HeuristicDetector._extract_text(content)
                        if text.strip():
                            user_messages.append(text[:200])
                except (json.JSONDecodeError, KeyError):
                    continue

        selected = user_messages[:20] + user_messages[-20:]
        for i, msg in enumerate(selected, 1):
            lines.append(f"  {i}. {msg}")

        return "\n".join(lines)

    @staticmethod
    def _try_claude_max(prompt: str) -> ClaudeOutcome:
        try:
            result = subprocess.run(
                ["claude", "-p", "--output-format", "json", "--max-turns", "1"],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=90,
            )
        except FileNotFoundError:
            # The binary is absent from PATH — a launchd job inherits no shell
            # profile, so this is a distinct failure from a binary that runs
            # and then errors out (OI-1258).
            log("ERROR", "Claude CLI not found on PATH")
            return ClaudeOutcome("missing_cli")
        except subprocess.TimeoutExpired:
            log("ERROR", "Claude CLI timed out after 90s")
            return ClaudeOutcome("timeout")
        except Exception as e:
            log("ERROR", f"Claude CLI error: {e}")
            return ClaudeOutcome("error", stderr=str(e))

        if result.returncode != 0:
            snippet = (result.stderr or "").strip()[:200]
            log("ERROR",
                f"Claude CLI failed (rc={result.returncode}): {snippet}")
            return ClaudeOutcome("cli_failed",
                                 returncode=result.returncode, stderr=snippet)

        try:
            output = json.loads(result.stdout)
            text = output.get("result", result.stdout)
        except json.JSONDecodeError:
            text = result.stdout

        if not (text or "").strip():
            log("ERROR", "Claude CLI succeeded (rc=0) but produced no usable text")
            return ClaudeOutcome("empty", text=result.stdout)

        return ClaudeOutcome("ok", text=text)

    @staticmethod
    def _try_deepseek_harness(prompt: str) -> Optional[str]:
        """Run deep analysis via the claude CLI driving DeepSeek's Anthropic-compatible endpoint.

        Uses the measured-safe key-auth recipe from
        ``provider_spawns.deepseek_harness_spawn``: own ``DEEPSEEK_API_KEY``,
        ``ANTHROPIC_AUTH_TOKEN`` bearer, telemetry suppressed.  Fails closed
        when no own key is available — never falls back to the OAuth
        subscription (constraint deepseek-harness-subscription-blocked).

        Model is ``VNX_ANALYZER_DEEPSEEK_MODEL`` (default ``deepseek-v4-flash``).
        """
        api_key = os.environ.get(DEEPSEEK_API_KEY_ENV, "")
        if not (api_key or "").strip():
            log("WARNING",
                f"{DEEPSEEK_API_KEY_ENV} not set; "
                f"deepseek-harness requires own API key (account safety — "
                f"the lane must never ride the OAuth subscription)")
            return None

        harness_env = build_harness_env(api_key)
        # Override model to the analyzer-specific default (flash, not pro).
        harness_env["CLAUDE_CODE_MODEL"] = DEEPSEEK_HARNESS_MODEL

        cli_args = ["claude", "-p", "--output-format", "json", "--max-turns", "1"]
        cli_args.extend(build_harness_cli_args())

        child_env = dict(os.environ)
        child_env.update(harness_env)

        try:
            result = subprocess.run(
                cli_args,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=120,
                env=child_env,
            )
            if result.returncode != 0:
                log("WARNING", f"DeepSeek harness failed (rc={result.returncode}): "
                               f"{result.stderr[:200]}")
                return None
            try:
                output = json.loads(result.stdout)
                return output.get("result", result.stdout)
            except json.JSONDecodeError:
                return result.stdout
        except FileNotFoundError:
            log("INFO", "Claude CLI not found for deepseek-harness")
            return None
        except subprocess.TimeoutExpired:
            log("WARNING", "DeepSeek harness timed out (120s)")
            return None
        except Exception as e:
            log("WARNING", f"DeepSeek harness error: {e}")
            return None

    @classmethod
    def _try_ollama(cls, prompt: str) -> Optional[str]:
        if not cls._probe_ollama():
            return None
        try:
            import urllib.request
            payload = json.dumps({
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.3, "num_predict": 2048},
            }).encode("utf-8")
            req = urllib.request.Request(
                "http://localhost:11434/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return body.get("response", "")
        except Exception as e:
            log("INFO", f"Ollama not available: {e}")
            return None

    @staticmethod
    def _parse_response(text: str) -> Optional[dict]:
        json_match = re.search(r'\{[\s\S]*\}', text)
        if not json_match:
            return None
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            return None
