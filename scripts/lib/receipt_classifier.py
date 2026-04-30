"""Adaptive receipt classifier (ARC-3).

Triggered (best-effort, non-blocking) from the append_receipt post-write hook.
Three modes:
  - batch (default): receipts are appended to a queue file, processed hourly
    by `receipt_classifier_batch.py`.
  - per_receipt: every receipt write spawns an async classifier subprocess.
  - failures_direct: failures fire immediately, successes batch.

Operator opt-in only: `VNX_RECEIPT_CLASSIFIER_ENABLED=1`. When unset/0 every
public function is a no-op.

Significance gate (per operator decision): only edits with confidence >= 0.8
AND impact_class in {policy_change, removed_rule, new_skill, threshold_change}
are queued to `pending_edits.json`. Trivial findings are dropped.

Daily cost budget (default $0.20) is tracked in
`<state_dir>/receipt_classifier_cost.json`. When exhausted the classifier
skips work and emits a single warning per call.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

# Local imports — keep classifier_providers package on sys.path.
_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from classifier_providers import ClassifierProvider, ClassifierResult, get_provider  # noqa: E402

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------

ENV_ENABLED = "VNX_RECEIPT_CLASSIFIER_ENABLED"
ENV_MODE = "VNX_RECEIPT_CLASSIFIER_MODE"
ENV_PROVIDER = "VNX_RECEIPT_CLASSIFIER_PROVIDER"
ENV_DAILY_BUDGET = "VNX_RECEIPT_CLASSIFIER_DAILY_COST_USD"
ENV_STATE_DIR = "VNX_STATE_DIR"

DEFAULT_MODE = "batch"
DEFAULT_PROVIDER = "haiku"
DEFAULT_DAILY_BUDGET_USD = 0.20

VALID_MODES = {"batch", "per_receipt", "failures_direct"}

SIGNIFICANT_IMPACT_CLASSES = {
    "policy_change",
    "removed_rule",
    "new_skill",
    "threshold_change",
}

CONFIDENCE_THRESHOLD = 0.8

# State file names (under VNX_STATE_DIR).
COST_FILE_NAME = "receipt_classifier_cost.json"
QUEUE_FILE_NAME = "receipt_classifier_queue.ndjson"
PENDING_EDITS_FILE_NAME = "pending_edits.json"

# Failure statuses we consider when sampling/branching.
_FAILURE_STATUSES = {"failed", "failure", "error", "blocked", "timeout"}


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def is_enabled() -> bool:
    return os.environ.get(ENV_ENABLED, "0") == "1"


def get_mode() -> str:
    mode = (os.environ.get(ENV_MODE) or DEFAULT_MODE).strip().lower()
    return mode if mode in VALID_MODES else DEFAULT_MODE


def get_provider_name() -> str:
    return (os.environ.get(ENV_PROVIDER) or DEFAULT_PROVIDER).strip().lower()


def get_daily_budget_usd() -> float:
    raw = os.environ.get(ENV_DAILY_BUDGET)
    if raw is None or raw == "":
        return DEFAULT_DAILY_BUDGET_USD
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_DAILY_BUDGET_USD


def state_dir() -> Path:
    """Resolve the VNX state directory; falls back to repo .vnx-data/state."""
    raw = os.environ.get(ENV_STATE_DIR)
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parents[2] / ".vnx-data" / "state"


def should_classify(receipt: Dict[str, Any], mode: Optional[str] = None) -> bool:
    """Whether the receipt is a candidate for classification at all.

    Sampling rules per mode:
      - batch / per_receipt: any task_complete / task_failed / task_timeout receipt.
      - failures_direct: only failures fire directly; successes are queued for batch.

    State-mutation, ack, dispatch_sent and other lightweight events are skipped
    because they carry no outcome signal.
    """
    if not isinstance(receipt, dict):
        return False
    event_type = str(receipt.get("event_type") or receipt.get("event") or "").lower()
    OUTCOME_EVENTS = {"task_complete", "task_completed", "task_failed", "task_timeout"}
    if event_type not in OUTCOME_EVENTS:
        return False
    return True


def trigger_receipt_classifier_async(receipt: Dict[str, Any]) -> Optional[str]:
    """Best-effort async fire from the receipt write hook.

    Returns:
        Short string describing the action taken (for logging), or None when
        the classifier is disabled. Never raises.
    """
    try:
        if not is_enabled():
            return None
        if not should_classify(receipt):
            return "skipped_not_outcome"

        mode = get_mode()
        if mode == "batch":
            _append_to_queue(receipt)
            return "queued_batch"

        if mode == "per_receipt":
            _spawn_async_classify(receipt)
            return "fired_per_receipt"

        if mode == "failures_direct":
            status = str(receipt.get("status") or "").lower()
            event_type = str(receipt.get("event_type") or receipt.get("event") or "").lower()
            is_failure = status in _FAILURE_STATUSES or event_type in {"task_failed", "task_timeout"}
            if is_failure:
                _spawn_async_classify(receipt)
                return "fired_failure_direct"
            _append_to_queue(receipt)
            return "queued_success_for_batch"

        return "skipped_unknown_mode"
    except Exception as exc:  # never propagate to the receipt writer
        logger.warning("receipt_classifier_async_failed: %s", exc)
        return "error"


def classify_receipt(
    receipt: Dict[str, Any], provider: Optional[ClassifierProvider] = None
) -> Dict[str, Any]:
    """Classify a single receipt synchronously. Used by per_receipt mode.

    Returns the parsed classification (or an error dict). Significant
    suggested edits are queued automatically when present.
    """
    return _run_classification([receipt], provider=provider, batch=False)


def classify_batch(
    receipts: List[Dict[str, Any]], provider: Optional[ClassifierProvider] = None
) -> Dict[str, Any]:
    """Classify a batch of receipts in a single provider call."""
    return _run_classification(receipts, provider=provider, batch=True)


# ----------------------------------------------------------------------
# Cost tracking
# ----------------------------------------------------------------------


@dataclass
class CostEntry:
    date: str
    spent_usd: float
    calls: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _today_str() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def _cost_path() -> Path:
    return state_dir() / COST_FILE_NAME


def _read_cost(path: Optional[Path] = None) -> CostEntry:
    p = path or _cost_path()
    today = _today_str()
    if not p.is_file():
        return CostEntry(date=today, spent_usd=0.0, calls=0)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return CostEntry(date=today, spent_usd=0.0, calls=0)
        if str(data.get("date")) != today:
            return CostEntry(date=today, spent_usd=0.0, calls=0)
        return CostEntry(
            date=today,
            spent_usd=float(data.get("spent_usd", 0.0)),
            calls=int(data.get("calls", 0)),
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return CostEntry(date=today, spent_usd=0.0, calls=0)


def _write_cost(entry: CostEntry, path: Optional[Path] = None) -> None:
    p = path or _cost_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(entry.to_dict(), separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, p)


def track_cost(provider_name: str, cost_usd: float, path: Optional[Path] = None) -> CostEntry:
    """Increment today's spend; ensures atomic write under concurrent fires."""
    p = path or _cost_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lock_path = p.with_suffix(p.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        entry = _read_cost(p)
        entry.spent_usd = round(entry.spent_usd + max(0.0, float(cost_usd)), 6)
        entry.calls += 1
        _write_cost(entry, p)
    logger.debug(
        "receipt_classifier cost: provider=%s today=%s spent=%.6f calls=%d",
        provider_name, entry.date, entry.spent_usd, entry.calls,
    )
    return entry


def is_budget_exhausted(path: Optional[Path] = None) -> bool:
    budget = get_daily_budget_usd()
    if budget <= 0:
        return True
    entry = _read_cost(path)
    return entry.spent_usd >= budget


# ----------------------------------------------------------------------
# Queue (batch mode)
# ----------------------------------------------------------------------


def _queue_path() -> Path:
    return state_dir() / QUEUE_FILE_NAME


def _append_to_queue(receipt: Dict[str, Any], path: Optional[Path] = None) -> None:
    p = path or _queue_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lock_path = p.with_suffix(p.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"queued_at": time.time(), "receipt": receipt}, separators=(",", ":")))
            fh.write("\n")


def drain_queue(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Read & truncate the queue under lock. Returns raw receipts list."""
    p = path or _queue_path()
    if not p.is_file():
        return []
    lock_path = p.with_suffix(p.suffix + ".lock")
    receipts: List[Dict[str, Any]] = []
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            content = p.read_text(encoding="utf-8")
        except OSError:
            return []
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            receipt = obj.get("receipt") if isinstance(obj, dict) else None
            if isinstance(receipt, dict):
                receipts.append(receipt)
        try:
            p.write_text("", encoding="utf-8")
        except OSError:
            pass
    return receipts


# ----------------------------------------------------------------------
# Classification core
# ----------------------------------------------------------------------


_PROMPT_HEADER = """You are an adaptive receipt classifier for the VNX governance system.

Task: read the dispatch receipt(s) below, classify the outcome, and (only if a
truly significant rule/policy/skill change is warranted) suggest a single
concrete edit. Trivial observations must be returned without any suggested_edit.

Output ONLY a JSON object — no prose, no markdown fences. The schema is:

{
  "domain": "governance|coding|business|test|other",
  "outcome_class": "success|failure|stuck|partial",
  "recurring_pattern_observed": "<text or null>",
  "impact_class": "trivial|moderate|significant|policy_change",
  "suggested_edit": null OR {
      "target_file": "<path>",
      "edit_type": "add_line|remove_line|replace",
      "content": "<text>",
      "rationale": "<text>",
      "confidence": <number between 0 and 1>
  }
}

Significance rule: emit `suggested_edit` ONLY when confidence >= 0.8 AND the
edit clearly changes a policy, removes a rule, adds a new skill, or shifts a
threshold. Otherwise set `suggested_edit` to null and choose impact_class
accordingly.

Receipts:
"""


def _build_prompt(receipts: Iterable[Dict[str, Any]]) -> str:
    body_parts: List[str] = []
    for idx, rcpt in enumerate(receipts, start=1):
        compact = {
            "dispatch_id": rcpt.get("dispatch_id"),
            "terminal": rcpt.get("terminal"),
            "event_type": rcpt.get("event_type") or rcpt.get("event"),
            "status": rcpt.get("status"),
            "timestamp": rcpt.get("timestamp"),
            "duration_seconds": rcpt.get("duration_seconds"),
            "model": rcpt.get("model"),
            "report_path": rcpt.get("report_path"),
            "pr_id": rcpt.get("pr_id") or rcpt.get("pr_number"),
            "summary": rcpt.get("summary") or rcpt.get("notes") or "",
        }
        body_parts.append(f"--- receipt {idx} ---\n{json.dumps(compact, ensure_ascii=False)}")
    return _PROMPT_HEADER + "\n".join(body_parts) + "\n"


def _is_significant(parsed: Dict[str, Any]) -> bool:
    edit = parsed.get("suggested_edit")
    if not isinstance(edit, dict):
        return False
    impact = str(parsed.get("impact_class") or "").strip().lower()
    if impact not in SIGNIFICANT_IMPACT_CLASSES:
        return False
    try:
        confidence = float(edit.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < CONFIDENCE_THRESHOLD:
        return False
    if not edit.get("target_file") or not edit.get("edit_type"):
        return False
    return True


def _pending_edits_path() -> Path:
    return state_dir() / PENDING_EDITS_FILE_NAME


def _queue_pending_edit(parsed: Dict[str, Any], source_dispatch_id: str) -> Optional[str]:
    """Append a significant suggested edit to pending_edits.json. Returns id."""
    edit = parsed.get("suggested_edit")
    if not isinstance(edit, dict):
        return None
    p = _pending_edits_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lock_path = p.with_suffix(p.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        existing: List[Dict[str, Any]] = []
        if p.is_file():
            try:
                raw = p.read_text(encoding="utf-8")
                if raw.strip():
                    data = json.loads(raw)
                    if isinstance(data, list):
                        existing = data
            except (OSError, json.JSONDecodeError):
                existing = []
        edit_id = f"arc3-{int(time.time() * 1000)}-{len(existing)}"
        record = {
            "id": edit_id,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "source": "receipt_classifier",
            "source_dispatch_id": source_dispatch_id,
            "domain": parsed.get("domain"),
            "outcome_class": parsed.get("outcome_class"),
            "impact_class": parsed.get("impact_class"),
            "recurring_pattern_observed": parsed.get("recurring_pattern_observed"),
            "edit": edit,
            "status": "pending",
        }
        existing.append(record)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        os.replace(tmp, p)
    return edit_id


def _run_classification(
    receipts: List[Dict[str, Any]],
    *,
    provider: Optional[ClassifierProvider] = None,
    batch: bool,
) -> Dict[str, Any]:
    """Shared classify path for single + batch."""
    if not receipts:
        return {"status": "skipped", "reason": "no_receipts"}

    if is_budget_exhausted():
        logger.warning(
            "receipt_classifier budget_exhausted: daily_budget_usd=%.4f",
            get_daily_budget_usd(),
        )
        return {"status": "skipped", "reason": "budget_exhausted"}

    prov = provider or get_provider(get_provider_name())
    prompt = _build_prompt(receipts)

    result: ClassifierResult = prov.classify(prompt)
    if result.cost_usd > 0:
        track_cost(prov.name, result.cost_usd)

    if result.error:
        return {
            "status": "provider_error",
            "error": result.error,
            "provider": prov.name,
            "latency_ms": result.latency_ms,
        }

    parsed = result.parsed_json
    if not isinstance(parsed, dict):
        return {
            "status": "parse_error",
            "raw_response": result.raw_response[:1000],
            "provider": prov.name,
        }

    queued: List[str] = []
    if _is_significant(parsed):
        first_dispatch_id = str(receipts[0].get("dispatch_id") or "unknown")
        edit_id = _queue_pending_edit(parsed, first_dispatch_id)
        if edit_id:
            queued.append(edit_id)

    return {
        "status": "ok",
        "batch": batch,
        "provider": prov.name,
        "parsed": parsed,
        "queued_edit_ids": queued,
        "cost_usd": result.cost_usd,
        "latency_ms": result.latency_ms,
    }


# ----------------------------------------------------------------------
# Async fire helper (per_receipt / failures_direct)
# ----------------------------------------------------------------------


def _spawn_async_classify(receipt: Dict[str, Any]) -> None:
    """Spawn a detached subprocess that runs the classifier on one receipt.

    The child re-enters this module via __main__ so we keep the import surface
    minimal and reuse the same code path. Fully detached (start_new_session)
    so it does not block the receipt writer.
    """
    try:
        payload = json.dumps({"receipts": [receipt]}, separators=(",", ":"))
    except (TypeError, ValueError):
        return
    cmd = [sys.executable, str(Path(__file__).resolve()), "--from-stdin", "--mode", "per_receipt"]
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        ).stdin.write(payload.encode("utf-8"))  # type: ignore[union-attr]
    except OSError as exc:
        logger.warning("receipt_classifier spawn failed: %s", exc)


# ----------------------------------------------------------------------
# CLI entry (used by the async spawn and ad-hoc operator runs)
# ----------------------------------------------------------------------


def _cli(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Receipt classifier (ARC-3)")
    parser.add_argument("--from-stdin", action="store_true", help="Read JSON {receipts:[...]} from stdin")
    parser.add_argument("--mode", default="per_receipt", choices=sorted(VALID_MODES))
    parser.add_argument("--dry-run", action="store_true", help="Do not call provider; print prompt only")
    args = parser.parse_args(argv)

    receipts: List[Dict[str, Any]] = []
    if args.from_stdin:
        try:
            payload = json.loads(sys.stdin.read() or "{}")
            receipts = list(payload.get("receipts") or [])
        except json.JSONDecodeError as exc:
            print(json.dumps({"status": "error", "reason": f"bad_stdin_json: {exc}"}))
            return 2

    if args.dry_run:
        print(_build_prompt(receipts))
        return 0

    if not receipts:
        print(json.dumps({"status": "skipped", "reason": "no_receipts"}))
        return 0

    if args.mode == "per_receipt":
        result = classify_receipt(receipts[0])
    else:
        result = classify_batch(receipts)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
