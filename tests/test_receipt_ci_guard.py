#!/usr/bin/env python3
"""CI guardrails for receipt writer exclusivity and direct append detection."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List, Tuple

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"

APPROVED_WRITERS = {
    "receipt_processor_v4.sh",
    "report_watcher.sh",
    "heartbeat_ack_monitor.py",
    "check_active_drain.py",
}

APPEND_HELPER_MARKERS = (
    "append_receipt.py",
    "append_receipt_payload",
)

SKIP_DIRS = {"lib", "state", "__pycache__", "archive", "archived"}
WRITER_SCAN_EXCLUDES = {
    "generate_lean_receipt.sh",
    "receipt_processor_lean_update.sh",
    "llm_benchmark_coding_v2.py",
}

DIRECT_APPEND_PATTERNS = (
    re.compile(r">>\s*['\"]?.*t0_receipts\.ndjson"),
    re.compile(r"open\([^\n]*t0_receipts\.ndjson[^\n]*,\s*['\"]a[+b]?['\"]"),
    re.compile(r"\.open\([^\n]*t0_receipts\.ndjson[^\n]*,\s*['\"]a[+b]?['\"]"),
)

RECEIPT_VAR_PATTERN = re.compile(r"RECEIPTS?_FILE\s*=.*t0_receipts\.ndjson")
PY_RECEIPT_VAR_PATTERN = re.compile(r"receipts_file\s*=.*t0_receipts\.ndjson")
VAR_APPEND_PATTERNS = (
    re.compile(r">>\s*.*\bRECEIPTS?_FILE\b"),
    re.compile(r"open\([^\n]*\breceipts_file\b[^\n]*,\s*['\"]a[+b]?['\"]"),
    re.compile(r"\.open\([^\n]*\breceipts_file\b[^\n]*,\s*['\"]a[+b]?['\"]"),
)


def _iter_all_scripts() -> Iterable[Tuple[Path, str]]:
    for path in SCRIPTS_DIR.rglob("*"):
        if path.is_dir() or path.suffix not in {".sh", ".py"}:
            continue

        rel = path.relative_to(SCRIPTS_DIR).as_posix()
        first = rel.split("/", 1)[0]
        if first in SKIP_DIRS:
            continue

        yield path, rel


def _iter_runtime_scripts() -> Iterable[Tuple[Path, str]]:
    for path, rel in _iter_all_scripts():
        if path.name.startswith("test_"):
            continue
        if path.name in WRITER_SCAN_EXCLUDES:
            continue
        yield path, rel


def _scan_direct_append(path: Path, rel: str) -> List[str]:
    offenders: List[str] = []
    content = path.read_text(encoding="utf-8", errors="ignore")

    for pattern in DIRECT_APPEND_PATTERNS:
        if pattern.search(content):
            offenders.append(f"{rel}: direct append to t0_receipts.ndjson")
            return offenders

    has_t0_var = bool(RECEIPT_VAR_PATTERN.search(content) or PY_RECEIPT_VAR_PATTERN.search(content))
    if has_t0_var:
        for pattern in VAR_APPEND_PATTERNS:
            if pattern.search(content):
                offenders.append(f"{rel}: direct append via receipts_file variable")
                break

    return offenders


def test_no_direct_append_in_active_runtime_scripts():
    offenders: List[str] = []

    for path, rel in _iter_runtime_scripts():
        offenders.extend(_scan_direct_append(path, rel))

    assert offenders == []


def test_receipt_writer_allowlist_enforced():
    writers = set()

    for path, _rel in _iter_runtime_scripts():
        if path.name == "append_receipt.py":
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        if any(marker in content for marker in APPEND_HELPER_MARKERS):
            writers.add(path.name)

    missing = APPROVED_WRITERS - writers
    extra = writers - APPROVED_WRITERS

    assert missing == set() and extra == set(), (
        f"Receipt writer allowlist mismatch. missing={sorted(missing)} extra={sorted(extra)}"
    )
