"""Phase 2: Heuristic pattern detector."""

from typing import List

from .models import SessionMetrics, SessionFlags


class HeuristicDetector:
    """Detect session patterns via heuristics — no LLM needed."""

    ERROR_KEYWORDS = {"error", "fail", "traceback", "exception", "errno",
                      "denied", "not found", "cannot", "fatal"}
    TEST_KEYWORDS = {"pytest", "test", "unittest", "-m pytest", "python3 -m pytest"}

    def detect_patterns(self, metrics: SessionMetrics,
                        messages: List[dict]) -> SessionFlags:
        flags = SessionFlags()

        flags.has_large_refactor = metrics.tool_edit_count > 10

        tool_sequence = self._extract_tool_sequence(messages)
        flags.has_error_recovery = self._detect_error_recovery(messages)
        flags.context_reset_count = self._count_context_resets(messages)
        flags.has_context_reset = flags.context_reset_count > 0
        flags.has_test_cycle = self._detect_test_cycle(tool_sequence, messages)
        flags.primary_activity = self._classify_activity(metrics)

        return flags

    def _extract_tool_sequence(self, messages: List[dict]) -> List[str]:
        sequence = []
        for record in messages:
            if record.get("type") != "assistant":
                continue
            for block in record.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    sequence.append(block.get("name", ""))
        return sequence

    def _detect_error_recovery(self, messages: List[dict]) -> bool:
        error_count = 0
        for record in messages:
            msg_type = record.get("type", "")
            if msg_type == "user":
                content = record.get("message", {}).get("content", "")
                text = self._extract_text(content)
                if any(kw in text.lower() for kw in self.ERROR_KEYWORDS):
                    error_count += 1
            elif msg_type == "assistant":
                for block in record.get("message", {}).get("content", []):
                    if block.get("type") == "tool_result":
                        result_text = self._extract_text(block.get("content", ""))
                        if any(kw in result_text.lower() for kw in self.ERROR_KEYWORDS):
                            error_count += 1
        return error_count >= 2

    def _count_context_resets(self, messages: List[dict]) -> int:
        count = 0
        for record in messages:
            if record.get("type") == "system":
                subtype = record.get("subtype", "")
                if "compaction" in subtype or "summary" in subtype:
                    count += 1
                    continue
                data = record.get("data", "")
                if isinstance(data, str) and "context" in data.lower():
                    count += 1
        return count

    def _detect_test_cycle(self, tool_sequence: List[str],
                           messages: List[dict]) -> bool:
        is_test_bash = []
        for record in messages:
            if record.get("type") != "assistant":
                continue
            for block in record.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    name = block.get("name", "")
                    if name.lower() == "bash":
                        inp = block.get("input", {})
                        cmd = inp.get("command", "") if isinstance(inp, dict) else ""
                        is_test = any(kw in cmd.lower() for kw in self.TEST_KEYWORDS)
                        is_test_bash.append(is_test)
                    else:
                        is_test_bash.append(False)

        test_positions = [i for i, v in enumerate(is_test_bash) if v]

        cycle_count = 0
        for i in range(len(test_positions) - 1):
            idx_a = test_positions[i]
            idx_b = test_positions[i + 1]
            between = tool_sequence[idx_a + 1:idx_b] if idx_b > idx_a + 1 else []
            if any(t.lower() in ("edit", "multiedit") for t in between):
                cycle_count += 1

        return cycle_count >= 2

    @staticmethod
    def _classify_activity(metrics: SessionMetrics) -> str:
        total = metrics.tool_calls_total or 1
        read_grep_ratio = (metrics.tool_read_count + metrics.tool_grep_count) / total
        edit_write_ratio = (metrics.tool_edit_count + metrics.tool_write_count) / total
        bash_ratio = metrics.tool_bash_count / total

        if read_grep_ratio > 0.50:
            return "research"
        if bash_ratio > 0.30:
            return "debugging"
        if edit_write_ratio > 0.40:
            if metrics.tool_edit_count > 10:
                return "refactoring"
            return "coding"
        return "mixed"

    @staticmethod
    def _extract_text(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(item.get("text", ""))
                elif isinstance(item, str):
                    parts.append(item)
            return " ".join(parts)
        return ""
