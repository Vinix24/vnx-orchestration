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

    DISPATCH_TABLE_RE = re.compile(r'\|\s*\*\*Dispatch-ID\*\*\s*\|\s*([^\|]+?)\s*\|')
    DISPATCH_HEADER_RE = re.compile(r'Dispatch-ID:\s*(\S+)')

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

    def _process_user(self, record: dict, metrics: SessionMetrics):
        metrics.user_message_count += 1
        if not metrics.dispatch_id and metrics.user_message_count <= 3:
            content = record.get("message", {}).get("content", "")
            text = content if isinstance(content, str) else " ".join(
                b.get("text", "") for b in content if isinstance(b, dict)
            ) if isinstance(content, list) else ""
            m = self.DISPATCH_TABLE_RE.search(text)
            if not m:
                m = self.DISPATCH_HEADER_RE.search(text)
            if m:
                metrics.dispatch_id = m.group(1).strip()

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
