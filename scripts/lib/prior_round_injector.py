#!/usr/bin/env python3
"""Wave 5 P0 — Inject prior-round review findings into next dispatch.

When a dispatch has pr_id or parent_dispatch_id that maps to existing review-gate
results, fetch those findings and format as a bounded section for inclusion in
the dispatch instruction.

Per ADR-008 + Wave 5 design, this is the highest signal-to-effort smart-context
P-level: prior-round findings are precisely the data that would prevent
round-cascades like PR #432's 9-round chain.
"""

from __future__ import annotations

import functools
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))

MAX_INJECTION_CHARS = 2000

_FILE_REF_RE = re.compile(
    r'\b([\w./][\w./]*\.(?:py|md|sql|sh|yaml|yml|ts|js|tsx|jsx)):(\d+)(?:-(\d+))?\b'
)

_KNOWN_GATES = ("codex_gate", "gemini_review")


@dataclass(frozen=True)
class PriorFinding:
    pr_id: str
    gate: str
    severity: str
    message: str
    file_paths: Tuple[str, ...]
    contract_hash: str
    recorded_at: str


def _resolve_state_dir(state_dir: Optional[Path]) -> Path:
    if state_dir is not None:
        return Path(state_dir)
    try:
        from vnx_paths import resolve_paths
        paths = resolve_paths()
        return Path(paths["VNX_STATE_DIR"])
    except Exception:
        return Path(".vnx-data") / "state"


def _extract_file_paths(message: str) -> Tuple[str, ...]:
    seen: list[str] = []
    dedup: set[str] = set()
    for m in _FILE_REF_RE.finditer(message):
        fp = m.group(1)
        if fp not in dedup:
            dedup.add(fp)
            seen.append(fp)
    return tuple(seen)


def _load_gate_results(pr_id: str, state_dir: Path) -> List[Tuple[str, str, dict]]:
    """Return list of (recorded_at, gate_name, data) sorted newest-first."""
    results_dir = state_dir / "review_gates" / "results"
    if not results_dir.is_dir():
        return []

    loaded: List[Tuple[str, str, dict]] = []
    for gate in _KNOWN_GATES:
        candidate = results_dir / f"pr-{pr_id}-{gate}.json"
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        loaded.append((data.get("recorded_at", ""), gate, data))

    loaded.sort(key=lambda x: x[0], reverse=True)
    return [(gate, recorded_at, data) for (recorded_at, gate, data) in loaded]


def fetch_prior_findings(
    pr_id: str,
    *,
    dispatch_paths: Optional[Sequence[str]] = None,
    max_chars: int = MAX_INJECTION_CHARS,
    state_dir: Optional[Path] = None,
) -> List[PriorFinding]:
    """Fetch prior-round findings for a PR, scope-filtered + budget-bounded.

    Priority order:
    1. Blocking before advisory
    2. Most recent round first (when multiple pr-<N>-{gate}.json files exist)
    3. Findings whose file_paths overlap with dispatch_paths (scope match)
    4. Trim to fit max_chars
    """
    paths_tuple: Tuple[str, ...] = tuple(dispatch_paths or [])
    time_bucket = int(time.time() // 60)
    results = _fetch_cached(pr_id, paths_tuple, max_chars, state_dir, time_bucket)
    return list(results)


@functools.lru_cache(maxsize=256)
def _fetch_cached(
    pr_id: str,
    dispatch_paths_tuple: Tuple[str, ...],
    max_chars: int,
    state_dir: Optional[Path],
    _time_bucket: int,
) -> Tuple[PriorFinding, ...]:
    resolved = _resolve_state_dir(state_dir)
    gate_data = _load_gate_results(pr_id, resolved)
    if not gate_data:
        return ()

    dispatch_path_set = set(dispatch_paths_tuple)

    blocking: List[PriorFinding] = []
    advisory: List[PriorFinding] = []

    for gate_name, recorded_at, data in gate_data:
        contract_hash = (data.get("contract_hash") or "")
        for raw in data.get("blocking_findings", []):
            msg = raw.get("message", "") if isinstance(raw, dict) else str(raw)
            if not msg:
                continue
            fps = _extract_file_paths(msg)
            blocking.append(PriorFinding(
                pr_id=pr_id,
                gate=gate_name,
                severity="blocking",
                message=msg,
                file_paths=fps,
                contract_hash=contract_hash,
                recorded_at=recorded_at,
            ))
        for raw in data.get("advisory_findings", []):
            msg = raw.get("message", "") if isinstance(raw, dict) else str(raw)
            if not msg:
                continue
            fps = _extract_file_paths(msg)
            advisory.append(PriorFinding(
                pr_id=pr_id,
                gate=gate_name,
                severity="advisory",
                message=msg,
                file_paths=fps,
                contract_hash=contract_hash,
                recorded_at=recorded_at,
            ))

    def scope_score(f: PriorFinding) -> int:
        if not dispatch_path_set:
            return 0
        return 0 if any(fp in dispatch_path_set for fp in f.file_paths) else 1

    blocking.sort(key=scope_score)
    advisory.sort(key=scope_score)
    all_findings = blocking + advisory

    trimmed: List[PriorFinding] = []
    for finding in all_findings:
        candidate = trimmed + [finding]
        if len(format_findings_section(candidate)) <= max_chars:
            trimmed.append(finding)
        else:
            break

    return tuple(trimmed)


def format_findings_section(findings: List[PriorFinding]) -> str:
    """Format findings as a markdown section for dispatch instruction injection.

    Includes anti-anchoring instruction per Codex Q4 epistemic-failure-mode mitigation.
    """
    if not findings:
        return ""

    lines = [
        "## PRIOR ROUND REVIEW FINDINGS",
        "",
        "> **Anti-anchoring notice:** Re-read current code at touched lines before "
        "relying on these findings — they may have been addressed in subsequent rounds.",
        "",
    ]

    blocking = [f for f in findings if f.severity == "blocking"]
    advisory = [f for f in findings if f.severity == "advisory"]

    if blocking:
        lines.append("### Blocking")
        for f in blocking:
            lines.append(f"- **[{f.gate}]** {f.message}")
        lines.append("")

    if advisory:
        lines.append("### Advisory")
        for f in advisory:
            lines.append(f"- **[{f.gate}]** {f.message}")
        lines.append("")

    return "\n".join(lines)
