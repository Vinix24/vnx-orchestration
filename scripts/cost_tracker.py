#!/usr/bin/env python3
"""Aggregate model usage and estimated costs from VNX receipts."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from lib.vnx_paths import ensure_env
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths: {exc}")


@dataclass(frozen=True)
class Pricing:
    input_per_million: float
    output_per_million: float
    cache_read_per_million: float = 0.0
    cache_write_per_million: float = 0.0


# USD per 1M tokens (source: anthropic.com/pricing, 2026-03-23)
MODEL_PRICING: Dict[str, Pricing] = {
    # Keep prior model keys for historical receipts.
    "claude-opus-4.5": Pricing(15.00, 75.00, 0.30, 3.75),
    "claude-sonnet-4.5": Pricing(3.00, 15.00, 0.30, 3.75),
    "claude-opus-4.6": Pricing(15.00, 75.00, 0.30, 3.75),
    "claude-sonnet-4.6": Pricing(3.00, 15.00, 0.30, 3.75),
    "gpt-5.3-codex": Pricing(2.00, 16.00),
    "gpt-5.2-codex": Pricing(1.75, 14.00),
    "gpt-5.1-codex": Pricing(1.25, 10.00),
    "gpt-5.1-codex-mini": Pricing(0.25, 2.00),
    "gemini-pro": Pricing(0.50, 1.50),
    "gemini-flash": Pricing(0.10, 0.30),
}

MODEL_ALIASES: Dict[str, str] = {
    "opus": "claude-opus-4.6",
    "claude-opus": "claude-opus-4.6",
    "sonnet": "claude-sonnet-4.6",
    "claude-sonnet": "claude-sonnet-4.6",
    "gpt-5.3": "gpt-5.3-codex",
    "gpt-5.2": "gpt-5.2-codex",
    "gpt-5.1": "gpt-5.1-codex",
    "gpt-5.1-mini": "gpt-5.1-codex-mini",
    "gpt-5-codex-mini": "gpt-5.1-codex-mini",
    "codex": "gpt-5.2-codex",
    "gemini": "gemini-pro",
}


def _normalize_model(model: Optional[str]) -> str:
    if not model:
        return "unknown"
    normalized = str(model).strip().lower()
    if not normalized:
        return "unknown"
    if normalized in MODEL_PRICING:
        return normalized
    if normalized in MODEL_ALIASES:
        return MODEL_ALIASES[normalized]
    return normalized


def _safe_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        if cleaned == "":
            return None
        try:
            return int(float(cleaned))
        except ValueError:
            return None
    return None


def _get_nested(data: Dict[str, Any], keys: Iterable[str]) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _first_value(data: Dict[str, Any], candidate_keys: Iterable[Tuple[str, ...]]) -> Any:
    for key_path in candidate_keys:
        value = _get_nested(data, key_path)
        if value is not None:
            return value
    return None


def _extract_tokens(receipt: Dict[str, Any]) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    input_candidates = (
        ("input_tokens",),
        ("prompt_tokens",),
        ("tokens_in",),
        ("usage", "input_tokens"),
        ("usage", "prompt_tokens"),
        ("token_usage", "input_tokens"),
        ("token_usage", "prompt_tokens"),
    )
    output_candidates = (
        ("output_tokens",),
        ("completion_tokens",),
        ("tokens_out",),
        ("usage", "output_tokens"),
        ("usage", "completion_tokens"),
        ("token_usage", "output_tokens"),
        ("token_usage", "completion_tokens"),
    )
    total_candidates = (
        ("total_tokens",),
        ("usage", "total_tokens"),
        ("token_usage", "total_tokens"),
    )

    input_tokens = _safe_int(_first_value(receipt, input_candidates))
    output_tokens = _safe_int(_first_value(receipt, output_candidates))
    total_tokens = _safe_int(_first_value(receipt, total_candidates))

    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens

    return input_tokens, output_tokens, total_tokens


def _load_receipts(path: Path) -> Tuple[List[Dict[str, Any]], int]:
    receipts: List[Dict[str, Any]] = []
    invalid_lines = 0
    if not path.exists():
        return receipts, invalid_lines

    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                receipts.append(parsed)
            else:
                invalid_lines += 1
        except json.JSONDecodeError:
            invalid_lines += 1
    return receipts, invalid_lines


def _resolve_terminal_map(state_dir: Path) -> Dict[str, Dict[str, str]]:
    result: Dict[str, Dict[str, str]] = {}
    panes_json = state_dir / "panes.json"
    if not panes_json.exists():
        return result

    try:
        payload = json.loads(panes_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return result

    if not isinstance(payload, dict):
        return result

    for terminal_key in ("T0", "T1", "T2", "T3", "t0", "t1", "t2", "t3"):
        value = payload.get(terminal_key)
        if isinstance(value, dict):
            terminal = terminal_key.upper()
            result[terminal] = {
                "provider": str(value.get("provider", "")).strip().lower() or "unknown",
                "model": _normalize_model(str(value.get("model", "")).strip() or None),
            }
    return result


def _infer_provider(terminal: str, terminal_map: Dict[str, Dict[str, str]]) -> str:
    if terminal in terminal_map:
        mapped = terminal_map[terminal].get("provider", "unknown")
        if mapped != "unknown":
            return mapped
    if terminal.startswith("T"):
        return "claude_code"
    if "GEMINI" in terminal:
        return "gemini_cli"
    if "CODEX" in terminal:
        return "codex_cli"
    return "unknown"


def _infer_model(receipt: Dict[str, Any], terminal: str, terminal_map: Dict[str, Dict[str, str]]) -> str:
    model_candidates = (
        ("model",),
        ("model_name",),
        ("requires_model",),
        ("metadata", "model"),
        ("usage", "model"),
    )
    explicit = _first_value(receipt, model_candidates)
    normalized = _normalize_model(str(explicit).strip() if explicit is not None else None)
    if normalized != "unknown":
        return normalized

    if terminal in terminal_map:
        mapped = terminal_map[terminal].get("model", "unknown")
        if mapped != "unknown":
            return mapped
    return "unknown"


def _estimate_cost(
    model: str,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    cache_read_tokens: Optional[int] = None,
    cache_creation_tokens: Optional[int] = None,
) -> Optional[float]:
    pricing = MODEL_PRICING.get(model)
    if pricing is None:
        return None
    if input_tokens is None or output_tokens is None:
        return None
    input_cost = (input_tokens / 1_000_000) * pricing.input_per_million
    output_cost = (output_tokens / 1_000_000) * pricing.output_per_million
    cache_read_cost = ((cache_read_tokens or 0) / 1_000_000) * pricing.cache_read_per_million
    cache_write_cost = ((cache_creation_tokens or 0) / 1_000_000) * pricing.cache_write_per_million
    return round(input_cost + output_cost + cache_read_cost + cache_write_cost, 8)


def _new_usage_bucket() -> Dict[str, Any]:
    return {
        "events": 0,
        "receipts_with_known_tokens": 0,
        "receipts_with_unknown_tokens": 0,
        "receipts_with_estimated_cost": 0,
        "receipts_with_unknown_cost": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "estimated_cost_usd": 0.0,
        "unknown_cost_usd": "unknown",
    }


def _finalize_bucket(bucket: Dict[str, Any]) -> Dict[str, Any]:
    finalized = dict(bucket)
    finalized["estimated_cost_usd"] = round(float(finalized["estimated_cost_usd"]), 8)
    return finalized


# Phase 2: Usage Data Resolution from Session Transcripts

def find_transcript(session_id: str) -> Optional[Path]:
    """Locate transcript file for session_id across all CLI providers.

    Searches in Claude Code, Gemini CLI, and Codex CLI session directories.
    Returns Path to transcript file if found, None otherwise.
    """
    if not session_id or session_id == "unknown":
        return None

    # Provider-specific transcript locations
    search_paths = [
        Path.home() / ".claude" / "sessions" / f"{session_id}.jsonl",
        Path.home() / ".gemini" / "sessions" / f"{session_id}.jsonl",
        Path.home() / ".codex" / "sessions" / f"{session_id}.jsonl",
    ]

    for path in search_paths:
        if path.exists():
            return path

    return None


def extract_usage_from_transcript(transcript_path: Path) -> Dict[str, Any]:
    """Extract cumulative usage from transcript JSONL file.

    Supports multiple formats:
    - Claude Code: usage.input_tokens, usage.output_tokens
    - Gemini CLI: usageMetadata.promptTokenCount, usageMetadata.candidatesTokenCount
    - Codex CLI: usage.prompt_tokens, usage.completion_tokens

    Returns dict with input_tokens, output_tokens, total_tokens.
    """
    total_input = 0
    total_output = 0

    try:
        content = transcript_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    for line in content.splitlines():
        if not line.strip():
            continue

        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        # Claude Code / Codex CLI format
        if "usage" in entry and isinstance(entry["usage"], dict):
            usage = entry["usage"]
            # Claude Code uses input_tokens/output_tokens
            total_input += usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0) or 0
            total_output += usage.get("output_tokens", 0) or usage.get("completion_tokens", 0) or 0

        # Gemini CLI format
        if "usageMetadata" in entry and isinstance(entry["usageMetadata"], dict):
            metadata = entry["usageMetadata"]
            total_input += metadata.get("promptTokenCount", 0) or 0
            total_output += metadata.get("candidatesTokenCount", 0) or 0

    return {
        "input_tokens": total_input,
        "output_tokens": total_output,
        "total_tokens": total_input + total_output
    }


def resolve_usage_for_receipt(receipt: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve usage data from transcript for a receipt.

    Priority order:
    1. Find session_id in receipt.session object (Phase 1B)
    2. Locate transcript file via find_transcript()
    3. Parse usage from transcript via extract_usage_from_transcript()
    4. Calculate cost using model from receipt.session

    Returns dict with input_tokens, output_tokens, total_tokens, cost_usd, resolution_status.
    """
    # Extract session info from Phase 1B session object
    session = receipt.get("session", {})
    session_id = session.get("session_id")
    model = session.get("model", "unknown")

    # If no session_id, cannot resolve
    if not session_id or session_id == "unknown":
        return {
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cost_usd": None,
            "resolution_status": "no_session_id"
        }

    # Find transcript file
    transcript_path = find_transcript(session_id)
    if not transcript_path:
        return {
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cost_usd": None,
            "resolution_status": "transcript_not_found"
        }

    # Extract usage from transcript
    try:
        usage = extract_usage_from_transcript(transcript_path)

        # Calculate cost
        cost = _estimate_cost(
            model,
            usage["input_tokens"] if usage["input_tokens"] > 0 else None,
            usage["output_tokens"] if usage["output_tokens"] > 0 else None
        )

        return {
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "total_tokens": usage["total_tokens"],
            "cost_usd": cost,
            "resolution_status": "resolved" if usage["total_tokens"] > 0 else "no_usage_data"
        }
    except Exception as e:
        return {
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cost_usd": None,
            "resolution_status": f"error: {str(e)[:50]}"
        }


def build_metrics(receipts: List[Dict[str, Any]], invalid_lines: int, state_dir: Path, source_path: Path) -> Dict[str, Any]:
    terminal_map = _resolve_terminal_map(state_dir)
    by_model: Dict[str, Dict[str, Any]] = defaultdict(_new_usage_bucket)
    by_terminal: Dict[str, Dict[str, Any]] = defaultdict(_new_usage_bucket)
    by_worker: Dict[str, Dict[str, Any]] = defaultdict(_new_usage_bucket)
    event_counts: Dict[str, int] = defaultdict(int)

    totals = {
        "receipts_total": len(receipts),
        "invalid_receipt_lines": invalid_lines,
        "events_analyzed": 0,
        "receipts_with_known_tokens": 0,
        "receipts_with_unknown_tokens": 0,
        "receipts_with_estimated_cost": 0,
        "receipts_with_unknown_cost": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "estimated_cost_usd": 0.0,
        "unknown_cost_usd": "unknown",
        # Phase 2: Resolution statistics
        "resolution_stats": {
            "resolved": 0,
            "no_session_id": 0,
            "transcript_not_found": 0,
            "no_usage_data": 0,
            "error": 0,
            "fallback_to_receipt": 0
        }
    }

    for receipt in receipts:
        event_type = str(receipt.get("event_type") or receipt.get("event") or "unknown")
        event_counts[event_type] += 1
        if event_type != "task_complete":
            continue

        totals["events_analyzed"] += 1
        terminal = str(receipt.get("terminal") or "unknown").strip().upper() or "UNKNOWN"
        provider = str(receipt.get("provider") or _infer_provider(terminal, terminal_map)).strip().lower() or "unknown"
        model = _infer_model(receipt, terminal, terminal_map)
        worker_key = f"{terminal}|{provider}"

        # Phase 2: Try to resolve usage from transcript first, fallback to receipt
        resolved_usage = resolve_usage_for_receipt(receipt)
        resolution_status = resolved_usage["resolution_status"]

        if resolution_status == "resolved":
            # Successfully resolved from transcript
            input_tokens = resolved_usage["input_tokens"]
            output_tokens = resolved_usage["output_tokens"]
            total_tokens = resolved_usage["total_tokens"]
            estimated_cost = resolved_usage["cost_usd"]
            totals["resolution_stats"]["resolved"] += 1
        else:
            # Fallback to extracting from receipt (legacy behavior)
            input_tokens, output_tokens, total_tokens = _extract_tokens(receipt)
            estimated_cost = _estimate_cost(model, input_tokens, output_tokens)

            # Track resolution failure reason
            if resolution_status.startswith("error"):
                totals["resolution_stats"]["error"] += 1
            elif resolution_status == "no_session_id":
                totals["resolution_stats"]["no_session_id"] += 1
            elif resolution_status == "transcript_not_found":
                totals["resolution_stats"]["transcript_not_found"] += 1
            elif resolution_status == "no_usage_data":
                totals["resolution_stats"]["no_usage_data"] += 1

            # If we got tokens from receipt, count as fallback
            if input_tokens is not None and output_tokens is not None:
                totals["resolution_stats"]["fallback_to_receipt"] += 1

        has_known_tokens = input_tokens is not None and output_tokens is not None and total_tokens is not None

        buckets = [by_model[model], by_terminal[terminal], by_worker[worker_key]]
        for bucket in buckets:
            bucket["events"] += 1

        if has_known_tokens:
            totals["receipts_with_known_tokens"] += 1
            totals["input_tokens"] += int(input_tokens)
            totals["output_tokens"] += int(output_tokens)
            totals["total_tokens"] += int(total_tokens)
            for bucket in buckets:
                bucket["receipts_with_known_tokens"] += 1
                bucket["input_tokens"] += int(input_tokens)
                bucket["output_tokens"] += int(output_tokens)
                bucket["total_tokens"] += int(total_tokens)
        else:
            totals["receipts_with_unknown_tokens"] += 1
            for bucket in buckets:
                bucket["receipts_with_unknown_tokens"] += 1

        if estimated_cost is None:
            totals["receipts_with_unknown_cost"] += 1
            for bucket in buckets:
                bucket["receipts_with_unknown_cost"] += 1
        else:
            totals["receipts_with_estimated_cost"] += 1
            totals["estimated_cost_usd"] += estimated_cost
            for bucket in buckets:
                bucket["receipts_with_estimated_cost"] += 1
                bucket["estimated_cost_usd"] += estimated_cost

    metrics = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "version": "1.0",
        "source": {
            "receipts_path": str(source_path),
            "state_dir": str(state_dir),
            "pricing_basis": "AGENT_TEAMS/VNX_MULTIMODEL_ARCHITECTURE.MD",
        },
        "totals": {
            **totals,
            "estimated_cost_usd": round(float(totals["estimated_cost_usd"]), 8),
        },
        "event_counts": {key: event_counts[key] for key in sorted(event_counts)},
        "by_model": {key: _finalize_bucket(by_model[key]) for key in sorted(by_model)},
        "by_terminal": {key: _finalize_bucket(by_terminal[key]) for key in sorted(by_terminal)},
        "by_worker": {key: _finalize_bucket(by_worker[key]) for key in sorted(by_worker)},
        "notes": [
            "Costs are deterministic estimates based on static per-model USD/token rates.",
            "Receipts without explicit model or token fields are included with model/tokens marked as 'unknown'.",
            "No external billing APIs are queried.",
        ],
    }
    return metrics


def format_human(metrics: Dict[str, Any]) -> str:
    totals = metrics["totals"]
    lines = [
        "VNX Cost Report",
        f"Generated: {metrics['generated_at_utc']}",
        f"Receipts source: {metrics['source']['receipts_path']}",
        "",
        "Totals",
        f"- Task complete events analyzed: {totals['events_analyzed']}",
        f"- Estimated cost (USD): ${totals['estimated_cost_usd']:.6f}",
        f"- Receipts with estimated cost: {totals['receipts_with_estimated_cost']}",
        f"- Receipts with unknown cost: {totals['receipts_with_unknown_cost']}",
        f"- Input tokens (known): {totals['input_tokens']}",
        f"- Output tokens (known): {totals['output_tokens']}",
        f"- Total tokens (known): {totals['total_tokens']}",
        f"- Receipts with unknown tokens: {totals['receipts_with_unknown_tokens']}",
    ]

    # Phase 2: Add resolution statistics
    res_stats = totals.get("resolution_stats", {})
    total_analyzed = totals["events_analyzed"]
    resolved_count = res_stats.get("resolved", 0)
    resolved_pct = (resolved_count / total_analyzed * 100) if total_analyzed > 0 else 0

    lines.extend([
        "",
        "Resolution Statistics (Phase 2 - Transcript Usage)",
        f"- Successfully resolved from transcripts: {resolved_count} ({resolved_pct:.1f}%)",
        f"- No session_id in receipt: {res_stats.get('no_session_id', 0)}",
        f"- Transcript not found: {res_stats.get('transcript_not_found', 0)}",
        f"- No usage data in transcript: {res_stats.get('no_usage_data', 0)}",
        f"- Resolution errors: {res_stats.get('error', 0)}",
        f"- Fallback to receipt extraction: {res_stats.get('fallback_to_receipt', 0)}",
        "",
        "By Model",
    ])

    for model, bucket in metrics["by_model"].items():
        lines.append(
            f"- {model}: events={bucket['events']}, cost=${bucket['estimated_cost_usd']:.6f}, "
            f"known_tokens={bucket['receipts_with_known_tokens']}, unknown_tokens={bucket['receipts_with_unknown_tokens']}"
        )

    lines.extend(["", "By Worker (terminal|provider)"])
    for worker, bucket in metrics["by_worker"].items():
        lines.append(
            f"- {worker}: events={bucket['events']}, cost=${bucket['estimated_cost_usd']:.6f}, "
            f"unknown_cost={bucket['receipts_with_unknown_cost']}"
        )

    lines.extend(["", "Limitations"])
    for note in metrics.get("notes", []):
        lines.append(f"- {note}")

    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Aggregate VNX receipt cost metrics")
    parser.add_argument(
        "--receipts",
        help="Path to receipts NDJSON (default: $VNX_STATE_DIR/t0_receipts.ndjson)",
    )
    parser.add_argument(
        "--output",
        help="Path to output JSON metrics file (default: $VNX_STATE_DIR/cost_metrics.json)",
    )
    parser.add_argument(
        "--human",
        action="store_true",
        help="Print human-readable report instead of JSON",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    paths = ensure_env()
    state_dir = Path(paths["VNX_STATE_DIR"])

    receipts_path = Path(args.receipts) if args.receipts else state_dir / "t0_receipts.ndjson"
    output_path = Path(args.output) if args.output else state_dir / "cost_metrics.json"

    receipts, invalid_lines = _load_receipts(receipts_path)
    metrics = build_metrics(receipts, invalid_lines, state_dir, receipts_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if args.human:
        print(format_human(metrics))
    else:
        print(json.dumps(metrics, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
