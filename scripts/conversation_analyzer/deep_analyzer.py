"""Phase 3: LLM-powered deep analysis of flagged sessions."""

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from .models import (
    SessionMetrics, SessionFlags,
    LLM_STRATEGY, OLLAMA_MODEL, DEEPSEEK_HARNESS_MODEL, DEEPSEEK_MIN_BALANCE_USD,
    DEEPSEEK_MIN_BALANCE_CNY,
    AUTO_CLAUSE_MAX_SESSIONS,
    DEEP_THRESHOLD_TOKENS, DEEP_THRESHOLD_TOOLS,
    log,
)
from .detector import HeuristicDetector
from claude_cli import resolve_claude_cli
from provider_spawns.deepseek_harness_spawn import (
    build_harness_child_env,
    build_harness_cli_args,
    DEEPSEEK_API_KEY_ENV,
)
from provider_spawns.harness_config_dir import HarnessConfigDirError

DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"


@dataclass(frozen=True)
class LLMOutcome:
    """Result of one LLM-invocation attempt, with the failure mode explicit.

    ``status`` is one of:
      - ``ok``           — call succeeded and produced usable text
      - ``empty``        — call succeeded (rc 0) but produced no usable text
      - ``missing_cli``  — claude binary absent from PATH (FileNotFoundError)
      - ``cli_failed``   — claude exited non-zero (``returncode`` + ``stderr``)
      - ``timeout``      — call exceeded its deadline
      - ``error``        — any other unexpected exception
      - ``config_skip``  — no invocation was ever made: a config gap
                           (unreachable/unconfigured Ollama) refused the
                           attempt before any subprocess started
      - ``key_missing``  — deepseek-harness was chosen but no own
                           ``DEEPSEEK_API_KEY`` reached the job: the operator
                           asked for this lane, so it is a failure, not a skip
      - ``balance_low`` / ``balance_unavailable`` — the DeepSeek ``/user/balance``
                           check said the account cannot pay, or could not be
                           read; same reasoning, a failure the digest shows

    ``text`` carries the assistant output for ``ok`` (and the raw stdout for
    ``empty``, kept for diagnostics). A caller can tell every mode apart
    without parsing logs — the gap the old ``Optional[str]`` return left open,
    where a missing CLI, a crashed CLI, and a successful-but-empty run all
    collapsed to ``None`` (OI-1258).

    ``attempted`` is derived from ``status``: every status counts as an
    attempt except ``config_skip``, which by definition never started one.
    This is what distinguishes a failed attempt from a skipped one
    (fix1585-r2): an Ollama config gap must not count as "tried and failed".
    The deepseek-harness pre-flight statuses (``key_missing``, ``balance_*``)
    start no subprocess either but do count, because that lane was explicitly
    selected and a night that cannot use it must read as failed.
    """

    status: str
    text: Optional[str] = None
    returncode: Optional[int] = None
    stderr: str = ""

    @property
    def attempted(self) -> bool:
        return self.status != "config_skip"


class DeepAnalyzer:
    """LLM-based deep analysis of flagged sessions."""

    ANALYSIS_CATEGORIES = [
        "prompt", "hook", "template", "skill", "workflow", "architecture"
    ]

    # Ollama availability probe: probed once per process, cached for the
    # lifetime of the class. None = not probed yet, True/False = result.
    _ollama_probed: Optional[bool] = None

    # DeepSeek pre-flight verdict (key + balance): computed once per process.
    # None = not run yet.
    _deepseek_preflight_cache: Optional[LLMOutcome] = None

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
        # fix1585-r2: sessions where every candidate strategy refused before
        # invoking anything (missing DEEPSEEK_API_KEY, no usable Ollama model
        # or server) — a config gap, not a failed attempt. Kept separate from
        # ``deep_attempts``/``deep_failures`` so it never fail-closes the run.
        self.deep_config_skips = 0
        # OI-1258: why the failures happened, keyed by LLMOutcome status
        # (``cli_failed``, ``timeout``, ``empty``, ``unparseable`` ...). A bare
        # count says something broke; the reason says what to fix.
        self.deep_failure_reasons: Dict[str, int] = {}

    def _record_failure(self, reason: str) -> None:
        self.deep_failures += 1
        self.deep_failure_reasons[reason] = self.deep_failure_reasons.get(reason, 0) + 1

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
        # ``any_attempted`` records whether an LLM was actually invoked this
        # session — derived from each candidate's own LLMOutcome.attempted,
        # not from which branch was merely tried. A config gap (missing
        # DEEPSEEK_API_KEY, no usable Ollama model/server) refuses before any
        # subprocess starts, so it must never flip this to True (fix1585-r2).
        any_attempted = False
        # Status of the last candidate that actually fired: the reason a
        # session ends up without a usable result (OI-1258).
        last_attempted_status = "error"

        # deepseek-harness: own-key auth via the claude CLI driving DeepSeek's
        # Anthropic-compatible endpoint.  Fails closed when DEEPSEEK_API_KEY is
        # unset — never falls back to the OAuth subscription (constraint
        # deepseek-harness-subscription-blocked).
        if LLM_STRATEGY == "deepseek-harness":
            outcome = self._try_deepseek_harness(prompt)
            any_attempted = any_attempted or outcome.attempted
            if outcome.attempted:
                last_attempted_status = outcome.status
            result_text = outcome.text if outcome.status == "ok" else None
        # Billing guard: in "auto" mode, refuse the claude path when the
        # session backlog exceeds the threshold. "claude-only" bypasses
        # the guard — the operator explicitly opted in to metered spend.
        elif LLM_STRATEGY == "claude-only":
            outcome = self._try_claude_max(prompt)
            any_attempted = any_attempted or outcome.attempted
            if outcome.attempted:
                last_attempted_status = outcome.status
            result_text = outcome.text if outcome.status == "ok" else None
        elif LLM_STRATEGY == "auto":
            if self._session_backlog <= AUTO_CLAUSE_MAX_SESSIONS:
                outcome = self._try_claude_max(prompt)
                any_attempted = any_attempted or outcome.attempted
                if outcome.attempted:
                    last_attempted_status = outcome.status
                result_text = outcome.text if outcome.status == "ok" else None
            else:
                if self._session_backlog > 0:
                    log("WARNING",
                        f"auto: skipping claude path — {self._session_backlog} "
                        f"session backlog exceeds AUTO_CLAUSE_MAX_SESSIONS "
                        f"({AUTO_CLAUSE_MAX_SESSIONS}). "
                        f"Use VNX_ANALYZER_LLM=claude-only to override.")

        if result_text is None and LLM_STRATEGY in ("auto", "ollama-only"):
            outcome = self._try_ollama(prompt)
            any_attempted = any_attempted or outcome.attempted
            if outcome.attempted:
                last_attempted_status = outcome.status
            result_text = outcome.text if outcome.status == "ok" else None

        if any_attempted:
            self.deep_attempts += 1
        else:
            # No candidate strategy ever invoked an LLM this session — every
            # branch that ran refused on a config gap. That is a skipped
            # attempt, not a failed one (fix1585-r2).
            self.deep_config_skips += 1

        if result_text is None:
            if any_attempted:
                self._record_failure(last_attempted_status)
                log("ERROR", f"Deep analysis failed: {last_attempted_status}")
            return None

        parsed = self._parse_response(result_text)
        if parsed is None or "suggestions" not in parsed:
            # The LLM answered but nothing parseable — or no "suggestions"
            # key — came back: an attempt that produced no usable result,
            # distinct from a hard failure but still not success.
            self._record_failure("unparseable")
            return None
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
    def _try_claude_max(prompt: str) -> LLMOutcome:
        claude_bin = resolve_claude_cli() or "claude"
        try:
            result = subprocess.run(
                [claude_bin, "-p", "--output-format", "json", "--max-turns", "1"],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=90,
            )
        except FileNotFoundError:
            # The binary is absent from PATH — a launchd job inherits no shell
            # profile, so this is a distinct failure from a binary that runs
            # and then errors out (OI-1258).
            log("ERROR", "Claude CLI not found on PATH or in the native install locations")
            return LLMOutcome("missing_cli")
        except subprocess.TimeoutExpired:
            log("ERROR", "Claude CLI timed out after 90s")
            return LLMOutcome("timeout")
        except Exception as e:
            log("ERROR", f"Claude CLI error: {e}")
            return LLMOutcome("error", stderr=str(e))

        if result.returncode != 0:
            snippet = (result.stderr or "").strip()[:200]
            log("ERROR",
                f"Claude CLI failed (rc={result.returncode}): {snippet}")
            return LLMOutcome("cli_failed",
                              returncode=result.returncode, stderr=snippet)

        try:
            output = json.loads(result.stdout)
            text = output.get("result", result.stdout)
        except json.JSONDecodeError:
            text = result.stdout

        if not (text or "").strip():
            log("ERROR", "Claude CLI succeeded (rc=0) but produced no usable text")
            return LLMOutcome("empty", text=result.stdout)

        return LLMOutcome("ok", text=text)

    @staticmethod
    def _fetch_deepseek_balance(api_key: str) -> dict:
        """GET DeepSeek's ``/user/balance`` and return the parsed JSON body.

        This is the pre-flight probe. ``/v1/models`` is deliberately not used:
        it answers OK for an account that cannot pay (on 2026-09-14 the door
        reported ``API OK`` and the run failed 11 seconds later on 402).
        """
        import urllib.request
        req = urllib.request.Request(
            DEEPSEEK_BALANCE_URL,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    @classmethod
    def _deepseek_preflight(cls, api_key: str) -> LLMOutcome:
        """Check own key and balance once per process; cache the verdict.

        Returns ``ok`` or a failure outcome (``key_missing``, ``balance_low``,
        ``balance_unavailable``). Every failure is logged at ERROR, once, with
        the reason. The key value is never logged.
        """
        if cls._deepseek_preflight_cache is not None:
            return cls._deepseek_preflight_cache

        outcome = cls._run_deepseek_preflight(api_key)
        cls._deepseek_preflight_cache = outcome
        return outcome

    @classmethod
    def _run_deepseek_preflight(cls, api_key: str) -> LLMOutcome:
        if not (api_key or "").strip():
            msg = (f"{DEEPSEEK_API_KEY_ENV} not set: deepseek-harness needs the own "
                   f"DeepSeek key (the lane must never ride the OAuth subscription); "
                   f"deep analysis is skipped for this run")
            log("ERROR", msg)
            return LLMOutcome("key_missing", stderr=msg)

        try:
            body = cls._fetch_deepseek_balance(api_key)
        except Exception as e:
            msg = (f"DeepSeek balance check failed ({type(e).__name__}: {e}); "
                   f"deep analysis is skipped for this run")
            log("ERROR", msg)
            return LLMOutcome("balance_unavailable", stderr=msg)

        infos = [info for info in (body.get("balance_infos") or []) if info]
        if not body.get("is_available"):
            msg = (f"DeepSeek account reports is_available={body.get('is_available')}; "
                   f"deep analysis is skipped for this run (top up the account)")
            log("ERROR", msg)
            return LLMOutcome("balance_low", stderr=msg)

        minimums = {"USD": DEEPSEEK_MIN_BALANCE_USD, "CNY": DEEPSEEK_MIN_BALANCE_CNY}
        row = next((i for cur in minimums for i in infos if i.get("currency") == cur), None)
        if row is None:
            msg = (f"DeepSeek balance response has no USD or CNY row (currencies="
                   f"{[i.get('currency') for i in infos]}); "
                   f"deep analysis is skipped for this run")
            log("ERROR", msg)
            return LLMOutcome("balance_unavailable", stderr=msg)

        currency = row["currency"]
        try:
            balance = float(row.get("total_balance"))
        except (TypeError, ValueError):
            msg = (f"DeepSeek balance response has no readable {currency} total_balance; "
                   f"deep analysis is skipped for this run")
            log("ERROR", msg)
            return LLMOutcome("balance_unavailable", stderr=msg)

        if balance < minimums[currency]:
            msg = (f"DeepSeek balance {balance:.2f} {currency} is below the minimum "
                   f"{minimums[currency]:.2f} {currency}; deep analysis is skipped "
                   f"for this run (top up the account)")
            log("ERROR", msg)
            return LLMOutcome("balance_low", stderr=msg)

        log("INFO", f"DeepSeek balance OK: {balance:.2f} {currency}")
        return LLMOutcome("ok")

    @classmethod
    def _try_deepseek_harness(cls, prompt: str) -> LLMOutcome:
        """Run deep analysis via the claude CLI driving DeepSeek's Anthropic-compatible endpoint.

        Uses the measured-safe key-auth recipe from
        ``provider_spawns.deepseek_harness_spawn``: own ``DEEPSEEK_API_KEY``,
        ``ANTHROPIC_AUTH_TOKEN`` bearer, telemetry suppressed, MCP off, and the
        Anthropic credentials scrubbed from the child environment. Fails closed
        when no own key is available: never falls back to the OAuth
        subscription (constraint deepseek-harness-subscription-blocked).

        Before the first call the own key and the ``/user/balance`` are checked
        (``_deepseek_preflight``); a failure there is a loud, counted failure
        (``key_missing``, ``balance_low``, ``balance_unavailable``) and no
        subprocess is started.

        Model is ``VNX_ANALYZER_DEEPSEEK_MODEL`` (default ``deepseek-flash``).
        """
        api_key = os.environ.get(DEEPSEEK_API_KEY_ENV, "")
        preflight = cls._deepseek_preflight(api_key)
        if preflight.status != "ok":
            return preflight

        claude_bin = resolve_claude_cli() or "claude"
        cli_args = [claude_bin, "-p", "--output-format", "json", "--max-turns", "1",
                    "--model", DEEPSEEK_HARNESS_MODEL]
        cli_args.extend(build_harness_cli_args())

        try:
            child_env = build_harness_child_env(api_key)
        except HarnessConfigDirError as exc:
            log("ERROR", f"DeepSeek harness config dir unsafe, not spawning: {exc}")
            return LLMOutcome("error", stderr=str(exc)[:200])

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
                snippet = (result.stderr or "").strip()[:200]
                log("ERROR", f"DeepSeek harness failed (rc={result.returncode}): "
                               f"{snippet}")
                return LLMOutcome("cli_failed",
                                  returncode=result.returncode, stderr=snippet)
            try:
                output = json.loads(result.stdout)
                text = output.get("result", result.stdout)
            except json.JSONDecodeError:
                text = result.stdout
            if not (text or "").strip():
                return LLMOutcome("empty", text=result.stdout)
            return LLMOutcome("ok", text=text)
        except FileNotFoundError:
            log("ERROR", "Claude CLI not found for deepseek-harness")
            return LLMOutcome("missing_cli")
        except subprocess.TimeoutExpired:
            log("ERROR", "DeepSeek harness timed out (120s)")
            return LLMOutcome("timeout")
        except Exception as e:
            log("ERROR", f"DeepSeek harness error: {e}")
            return LLMOutcome("error", stderr=str(e))

    @classmethod
    def _try_ollama(cls, prompt: str) -> LLMOutcome:
        if not cls._probe_ollama():
            # The probe already logs the specific reason (unreachable server,
            # zero models, or the configured model missing) — no subprocess-
            # equivalent call was ever issued, so this is a config_skip, not
            # an attempted-and-failed call (fix1585-r2).
            return LLMOutcome("config_skip")
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
                text = body.get("response", "")
        except Exception as e:
            log("INFO", f"Ollama not available: {e}")
            return LLMOutcome("error", stderr=str(e))

        if not (text or "").strip():
            return LLMOutcome("empty", text=text)
        return LLMOutcome("ok", text=text)

    @staticmethod
    def _parse_response(text: str) -> Optional[dict]:
        json_match = re.search(r'\{[\s\S]*\}', text)
        if not json_match:
            return None
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            return None
