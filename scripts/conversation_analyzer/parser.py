"""Phase 1: JSONL session parser."""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from .models import (
    SessionMetrics, TERMINAL_PATTERNS, normalize_model,
)


class SessionParser:
    """Parse a Claude Code JSONL session into structured metrics."""

    # How many initial user messages are scanned for a dispatch ID. A governed
    # worker session always carries its own dispatch ID in the initial prompt
    # (the Dispatch Metadata footer), so a bounded window is enough. The window
    # is generous (10) to cover sessions where the real ID lands in a slightly
    # later user message (measured: msg 6), while deliberately NOT scanning the
    # full transcript — long-lived T0 orchestration sessions reference many
    # other dispatches later on, and attributing the whole session to one of
    # them would be wrong.
    MAX_DISPATCH_SCAN_MESSAGES = 10

    DISPATCH_TABLE_RE = re.compile(r'\|\s*\*\*Dispatch-ID\*\*\s*\|\s*([^\|]+?)\s*\|')
    # Optional bold markers around the label ("**Dispatch-ID:** value") are
    # tolerated so bold-styled mentions are captured instead of yielding the
    # asterisks as the candidate. The closing "**" sits AFTER the colon in the
    # bold form ("**Dispatch-ID:** value"), hence the trailing optional group.
    DISPATCH_HEADER_RE = re.compile(r'(?:\*\*)?Dispatch-ID\s*:\s*(?:\*\*)?\s*(\S+)')
    # Placeholder pattern: <any-text> — template placeholders like <dispatch_id>
    # must never be treated as real IDs.
    PLACEHOLDER_RE = re.compile(r'^<[^>]+>$')
    # Dispatch IDs always start with a date-prefix (YYYYMMDD-). This is the
    # minimum validation that rejects template placeholders, empty strings,
    # and free-form text while accepting all real dispatch ID formats.
    VALID_DISPATCH_ID_RE = re.compile(r'^\d{8}-')
    # Trailing prose punctuation that commonly follows an inline dispatch ID in
    # free text ("Dispatch-ID: 20260613-852rev-deepseek.") — never part of the
    # ID itself, stripped before the date-prefix check.
    TRAILING_PUNCT_RE = re.compile(r'[.,;:!?)\]}\'"]+$')

    @staticmethod
    def session_id_from_path(jsonl_path: Path) -> str:
        return jsonl_path.stem

    @staticmethod
    def project_path_from_dir(dir_name: str) -> str:
        decoded = dir_name.replace("-", "/")
        if decoded.startswith("/"):
            return decoded
        return "/" + decoded

    @staticmethod
    def detect_terminal(dir_name: str) -> str:
        for terminal, pattern in TERMINAL_PATTERNS.items():
            if pattern.search(dir_name):
                return terminal
        return "unknown"

    def parse_file(self, jsonl_path: Path) -> Tuple[SessionMetrics, List[dict]]:
        metrics = SessionMetrics()
        metrics.session_id = self.session_id_from_path(jsonl_path)
        metrics.file_size_bytes = jsonl_path.stat().st_size

        dir_name = jsonl_path.parent.name
        metrics.project_path = self.project_path_from_dir(dir_name)
        metrics.terminal = self.detect_terminal(dir_name)

        messages, first_ts, last_ts = self._parse_records(jsonl_path, metrics)

        if first_ts and last_ts:
            delta = (last_ts - first_ts).total_seconds()
            metrics.duration_minutes = round(delta / 60.0, 1)

        if first_ts:
            metrics.session_date = first_ts.strftime("%Y-%m-%d")
        else:
            metrics.session_date = datetime.now().strftime("%Y-%m-%d")

        return metrics, messages

    def _parse_records(
        self, jsonl_path: Path, metrics: SessionMetrics
    ) -> Tuple[List[dict], Optional[datetime], Optional[datetime]]:
        messages = []
        first_ts = None
        last_ts = None

        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = record.get("type", "")
                timestamp_str = record.get("timestamp", "")
                metrics.message_count += 1

                if timestamp_str:
                    ts = self._parse_timestamp(timestamp_str)
                    if ts:
                        if first_ts is None:
                            first_ts = ts
                        last_ts = ts

                if msg_type == "assistant":
                    self._process_assistant(record, metrics)
                    messages.append(record)
                elif msg_type == "user":
                    self._process_user(record, metrics)
                    messages.append(record)
                elif msg_type == "system":
                    messages.append(record)

        return messages, first_ts, last_ts

    def _process_assistant(self, record: dict, metrics: SessionMetrics):
        metrics.assistant_message_count += 1
        msg = record.get("message", {})
        usage = msg.get("usage", {})
        metrics.total_input_tokens += usage.get("input_tokens", 0)
        metrics.total_output_tokens += usage.get("output_tokens", 0)
        metrics.cache_creation_tokens += usage.get("cache_creation_input_tokens", 0)
        metrics.cache_read_tokens += usage.get("cache_read_input_tokens", 0)

        if not metrics.session_model:
            raw_model = msg.get("model", "")
            if raw_model:
                metrics.session_model = normalize_model(raw_model)

        for block in msg.get("content", []):
            if block.get("type") == "tool_use":
                metrics.tool_calls_total += 1
                self._count_tool(metrics, block.get("name", ""))

    @classmethod
    def _validate_dispatch_id(cls, raw: str) -> Optional[str]:
        """Validate and normalise an extracted dispatch_id candidate.

        Returns the trimmed string when it looks like a real dispatch ID,
        or None when the value is a template placeholder, empty, or
        otherwise not a valid ID.

        Normalisation strips markdown bold markers (``**id**``) and trailing
        prose punctuation (``Dispatch-ID: 20260613-x.``) that an inline mention
        picks up from the surrounding sentence.
        """
        if not raw:
            return None
        candidate = raw.strip()
        if candidate.startswith("**") and candidate.endswith("**"):
            candidate = candidate[2:-2].strip()
        # Reject template placeholders: <dispatch_id>, <any-text>, etc.
        if cls.PLACEHOLDER_RE.match(candidate):
            return None
        # Strip trailing sentence punctuation (never part of a dispatch ID).
        candidate = cls.TRAILING_PUNCT_RE.sub("", candidate)
        if not candidate:
            return None
        # Require the YYYYMMDD- date-prefix that every real dispatch ID carries.
        if not cls.VALID_DISPATCH_ID_RE.match(candidate):
            return None
        return candidate

    @classmethod
    def _extract_dispatch_id(cls, text: str) -> Optional[str]:
        """Return the first VALID dispatch ID found in *text*, or None.

        Scans every Dispatch-ID mention (table cell or header) in document
        order and returns the first candidate that passes validation.  A
        template placeholder (``<dispatch_id>``) earlier in the text no longer
        blocks extraction of a real ID that appears later in the same message:
        the worker-context template carries the placeholder in its Commit
        Convention section before the Dispatch Metadata footer holds the real
        ID, so first-match-only extraction silently lost 96.7% of sessions
        (OI-872).
        """
        candidates = []
        for m in cls.DISPATCH_TABLE_RE.finditer(text):
            candidates.append((m.start(), m.group(1)))
        for m in cls.DISPATCH_HEADER_RE.finditer(text):
            candidates.append((m.start(), m.group(1)))
        candidates.sort(key=lambda pair: pair[0])
        for _, raw in candidates:
            validated = cls._validate_dispatch_id(raw)
            if validated:
                return validated
        return None

    def _process_user(self, record: dict, metrics: SessionMetrics):
        metrics.user_message_count += 1
        if metrics.dispatch_id or metrics.user_message_count > self.MAX_DISPATCH_SCAN_MESSAGES:
            return
        content = record.get("message", {}).get("content", "")
        text = content if isinstance(content, str) else " ".join(
            b.get("text", "") for b in content if isinstance(b, dict)
        ) if isinstance(content, list) else ""
        dispatch_id = self._extract_dispatch_id(text)
        if dispatch_id:
            metrics.dispatch_id = dispatch_id

    @staticmethod
    def _parse_timestamp(ts_str: str) -> Optional[datetime]:
        try:
            clean = ts_str.replace("Z", "+00:00")
            return datetime.fromisoformat(clean)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _count_tool(metrics: SessionMetrics, tool_name: str):
        name_lower = tool_name.lower()
        if name_lower == "read":
            metrics.tool_read_count += 1
        elif name_lower in ("edit", "multiedit"):
            metrics.tool_edit_count += 1
        elif name_lower == "bash":
            metrics.tool_bash_count += 1
        elif name_lower in ("grep", "glob"):
            metrics.tool_grep_count += 1
        elif name_lower == "write":
            metrics.tool_write_count += 1
        elif name_lower == "task":
            metrics.tool_task_count += 1
        else:
            metrics.tool_other_count += 1
