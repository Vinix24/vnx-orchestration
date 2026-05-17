"""Tests for the 3 blockers + 4 high findings in the governance cluster fix.

B-1: open_items_manager.py save_items() atomic write + lock in CLI paths
B-2: migrations use coordination_db.get_connection() for WAL mode
B-3: closure_verifier _count_report_blocking_indicators self-reference loop
H-1: governance_emit duplicate datetime.now()
H-2: audit_log_entry flock for concurrent safety
H-3: init_schema single-connection race fix
H-4: build_t0_state silent swallow -> log.warning

Dispatch-ID: 20260517-fix-governance-cluster
"""

from __future__ import annotations

import fcntl
import inspect
import json
import os
import re
import sqlite3
import sys
import tempfile
import textwrap
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest

_SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
_LIB_DIR = _SCRIPT_DIR / "lib"

if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))


# ---------------------------------------------------------------------------
# B-1: open_items_manager atomic save + lock
# ---------------------------------------------------------------------------

class TestOpenItemsManagerAtomicSave:
    """B-1: save_items uses tmp + os.replace, CLI paths hold shared lock."""

    def test_save_items_uses_atomic_write(self):
        """save_items must use os.replace, not direct open(path, 'w')."""
        source = inspect.getsource(__import__("open_items_manager").save_items)
        assert "os.replace" in source, "save_items must use os.replace for atomic write"
        assert ".json.tmp" in source or "with_suffix" in source, (
            "save_items must write to a .tmp file first"
        )

    def test_save_items_no_direct_overwrite(self):
        """save_items must NOT open the canonical file for writing directly."""
        source = inspect.getsource(__import__("open_items_manager").save_items)
        assert "open(OPEN_ITEMS_FILE, 'w')" not in source, (
            "save_items must not open OPEN_ITEMS_FILE directly for writing"
        )

    def test_add_item_cli_holds_lock(self):
        """add_item (CLI path) must acquire _with_items_lock."""
        source = inspect.getsource(__import__("open_items_manager").add_item)
        assert "_with_items_lock" in source, "add_item must use _with_items_lock"

    def test_close_item_cli_holds_lock(self):
        """close_item (CLI path) must acquire _with_items_lock."""
        source = inspect.getsource(__import__("open_items_manager").close_item)
        assert "_with_items_lock" in source, "close_item must use _with_items_lock"

    def test_attach_evidence_cli_holds_lock(self):
        """attach_evidence (CLI path) must acquire _with_items_lock."""
        source = inspect.getsource(__import__("open_items_manager").attach_evidence)
        assert "_with_items_lock" in source, "attach_evidence must use _with_items_lock"

    def test_rescan_items_cli_holds_lock(self):
        """rescan_items (CLI path) must acquire _with_items_lock."""
        source = inspect.getsource(__import__("open_items_manager").rescan_items)
        assert "_with_items_lock" in source, "rescan_items must use _with_items_lock"

    def test_add_item_programmatic_uses_shared_lock(self):
        """add_item_programmatic must use _with_items_lock (not inline lock)."""
        source = inspect.getsource(
            __import__("open_items_manager").add_item_programmatic
        )
        assert "_with_items_lock" in source, (
            "add_item_programmatic must use _with_items_lock context manager"
        )

    def test_save_items_roundtrip(self, tmp_path):
        """save_items writes valid JSON that load_items can read back."""
        mod = __import__("open_items_manager")
        orig_file = mod.OPEN_ITEMS_FILE
        orig_state = mod.STATE_DIR
        try:
            mod.STATE_DIR = tmp_path
            mod.OPEN_ITEMS_FILE = tmp_path / "open_items.json"
            data = {"schema_version": "1.0", "items": [], "next_id": 1}
            mod.save_items(data)
            assert mod.OPEN_ITEMS_FILE.exists()
            loaded = json.loads(mod.OPEN_ITEMS_FILE.read_text())
            assert loaded["schema_version"] == "1.0"
            assert "last_updated" in loaded
        finally:
            mod.OPEN_ITEMS_FILE = orig_file
            mod.STATE_DIR = orig_state

    def test_generate_digest_atomic_writes(self):
        """generate_digest + generate_markdown must use os.replace."""
        mod = __import__("open_items_manager")
        digest_src = inspect.getsource(mod.generate_digest)
        markdown_src = inspect.getsource(mod.generate_markdown)
        assert "os.replace" in digest_src, "generate_digest must use os.replace"
        assert "os.replace" in markdown_src, "generate_markdown must use os.replace"


# ---------------------------------------------------------------------------
# B-2: migrations use get_connection (WAL mode)
# ---------------------------------------------------------------------------

class TestMigrationsWalMode:
    """B-2: migration scripts use coordination_db.get_connection for WAL."""

    @pytest.mark.parametrize("module_name", [
        "migrations.apply_0017",
        "migrations.apply_0019",
        "migrations.apply_0020",
    ])
    def test_migration_imports_get_connection(self, module_name):
        """Migration module must import get_connection from coordination_db."""
        mod = __import__(module_name, fromlist=["apply_migration"])
        source = inspect.getsource(mod)
        assert "from coordination_db import get_connection_for_db" in source, (
            f"{module_name} must import get_connection from coordination_db"
        )

    @pytest.mark.parametrize("module_name", [
        "migrations.apply_0017",
        "migrations.apply_0019",
        "migrations.apply_0020",
    ])
    def test_migration_no_direct_sqlite_connect(self, module_name):
        """apply_migration must not use sqlite3.connect directly."""
        mod = __import__(module_name, fromlist=["apply_migration"])
        source = inspect.getsource(mod.apply_migration)
        assert "sqlite3.connect" not in source, (
            f"{module_name}.apply_migration must use get_connection, not sqlite3.connect"
        )

    def test_apply_0020_down_no_direct_sqlite_connect(self):
        """apply_down_migration must not use sqlite3.connect directly."""
        mod = __import__("migrations.apply_0020", fromlist=["apply_down_migration"])
        source = inspect.getsource(mod.apply_down_migration)
        assert "sqlite3.connect" not in source, (
            "apply_0020.apply_down_migration must use get_connection"
        )


# ---------------------------------------------------------------------------
# B-3: closure_verifier self-reference loop
# ---------------------------------------------------------------------------

class TestClosureVerifierSelfReference:
    """B-3: _count_report_blocking_indicators must not match its own output."""

    def _count(self, content: str) -> int:
        mod = __import__("closure_verifier")
        return mod._count_report_blocking_indicators(content)

    def test_self_reference_lines_excluded(self):
        """Lines matching closure verifier output format must be filtered out."""
        report = textwrap.dedent("""\
            Closure verifier: FAIL
            - [FAIL] feature_plan_status: FEATURE_PLAN status is missing
            - [FAIL] pr_queue_complete: PR queue totals are not fully complete
            - [PASS] branch_pushed: remote branch found: feat/x
        """)
        count = self._count(report)
        assert count == 0, (
            f"Self-referential [FAIL]/[PASS] lines must not count as blocking, got {count}"
        )

    def test_genuine_blocking_still_counted(self):
        """Real blocking indicators must still be detected."""
        report = textwrap.dedent("""\
            ## Review Results
            [BLOCKING] SQL injection in user input handling
            **Severity**: blocking
            severity: blocking
            BLOCKER: missing auth check
        """)
        count = self._count(report)
        assert count >= 4, f"Expected at least 4 blocking indicators, got {count}"

    def test_status_fail_in_gate_context_counted(self):
        """Status: FAIL in gate output (not closure verifier format) must count."""
        report = textwrap.dedent("""\
            ## Gate Results
            Gate: codex_gate
            Status: FAIL
            Finding: unused import detected
        """)
        count = self._count(report)
        assert count >= 1, f"Status: FAIL in gate context must count, got {count}"

    def test_mixed_content(self):
        """Mix of self-referential and genuine blocking lines."""
        report = textwrap.dedent("""\
            - [FAIL] some_check: something failed
            [BLOCKING] real finding
            - [PASS] other_check: passed
            Status: FAIL
        """)
        count = self._count(report)
        assert count == 2, (
            f"Expected 2 (BLOCKING + Status: FAIL), got {count}"
        )


# ---------------------------------------------------------------------------
# H-1: governance_emit duplicate datetime.now()
# ---------------------------------------------------------------------------

class TestGovernanceEmitTimestamp:
    """H-1: emit_dispatch_receipt must use a single datetime.now() call."""

    def test_single_timestamp_call(self):
        """now_ts and recorded_ts must derive from the same datetime.now() call."""
        mod = __import__("governance_emit")
        source = inspect.getsource(mod.emit_dispatch_receipt)
        now_calls = re.findall(r"datetime\.now\(", source)
        assert len(now_calls) == 1, (
            f"emit_dispatch_receipt must call datetime.now() exactly once, found {len(now_calls)}"
        )

    def test_recorded_ts_equals_now_ts(self):
        """recorded_ts must be assigned from now_ts, not a separate call."""
        mod = __import__("governance_emit")
        source = inspect.getsource(mod.emit_dispatch_receipt)
        assert "recorded_ts = now_ts" in source, (
            "recorded_ts must be assigned from now_ts"
        )


# ---------------------------------------------------------------------------
# H-2: audit_log_entry flock
# ---------------------------------------------------------------------------

class TestAuditLogEntryFlock:
    """H-2: audit_log_entry must use fcntl.flock for concurrent safety."""

    def test_audit_log_uses_flock(self):
        """audit_log_entry must call fcntl.flock."""
        mod = __import__("open_items_manager")
        source = inspect.getsource(mod.audit_log_entry)
        assert "fcntl.flock" in source, "audit_log_entry must use fcntl.flock"

    def test_audit_log_uses_lock_ex(self):
        """audit_log_entry must use LOCK_EX for exclusive access."""
        mod = __import__("open_items_manager")
        source = inspect.getsource(mod.audit_log_entry)
        assert "LOCK_EX" in source, "audit_log_entry must use LOCK_EX"

    def test_audit_log_flushes(self):
        """audit_log_entry must flush before releasing lock."""
        mod = __import__("open_items_manager")
        source = inspect.getsource(mod.audit_log_entry)
        assert "f.flush()" in source, "audit_log_entry must flush before unlock"

    def test_audit_log_writes_valid_ndjson(self, tmp_path):
        """audit_log_entry produces valid NDJSON."""
        mod = __import__("open_items_manager")
        orig_log = mod.AUDIT_LOG
        orig_state = mod.STATE_DIR
        try:
            mod.STATE_DIR = tmp_path
            mod.AUDIT_LOG = tmp_path / "test_audit.jsonl"
            mod.audit_log_entry("test_action", item_id="OI-001", severity="warn")
            content = mod.AUDIT_LOG.read_text()
            entry = json.loads(content.strip())
            assert entry["action"] == "test_action"
            assert entry["item_id"] == "OI-001"
            assert "timestamp" in entry
        finally:
            mod.AUDIT_LOG = orig_log
            mod.STATE_DIR = orig_state


# ---------------------------------------------------------------------------
# H-3: init_schema single-connection race fix
# ---------------------------------------------------------------------------

class TestInitSchemaRace:
    """H-3: init_schema must use a single connection for the full sequence."""

    def test_single_get_connection_call(self):
        """init_schema must open only one get_connection context."""
        from coordination_db import init_schema
        source = inspect.getsource(init_schema)
        context_opens = source.count("with get_connection(")
        assert context_opens == 1, (
            f"init_schema must use exactly 1 get_connection context, found {context_opens}"
        )

    def test_version_check_inside_connection(self):
        """Version check + migration loop must be inside the same connection."""
        from coordination_db import init_schema
        source = inspect.getsource(init_schema)
        lines = source.splitlines()
        with_line = None
        version_line = None
        for i, line in enumerate(lines):
            if "with get_connection(" in line:
                with_line = i
            if "SELECT MAX(version)" in line:
                version_line = i
        assert with_line is not None, "get_connection context not found"
        assert version_line is not None, "version check not found"
        assert version_line > with_line, "version check must be inside connection context"


# ---------------------------------------------------------------------------
# H-4: build_t0_state silent swallow -> log.warning
# ---------------------------------------------------------------------------

class TestBuildT0StateSilentSwallow:
    """H-4: main() exception handler must log, not silently pass."""

    def test_no_bare_except_pass_for_build(self):
        """The top-level build_t0_state exception handler must not silently pass."""
        mod = __import__("build_t0_state")
        source = inspect.getsource(mod.main)
        assert "pass  # SessionStart hook must never block session" not in source, (
            "The build_t0_state exception handler must log, not 'pass'"
        )

    def test_exception_logged(self):
        """The main try/except for build_t0_state must log the exception."""
        mod = __import__("build_t0_state")
        source = inspect.getsource(mod.main)
        assert "log.warning" in source or "log.error" in source, (
            "main() must log build_t0_state failures, not silently pass"
        )
