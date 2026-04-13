#!/usr/bin/env python3
"""dispatch_parameter_tracker.py — Karpathy-style self-improvement loop for dispatch parameters.

Tracks dispatch parameters (instruction size, context, role, model, etc.) against
outcomes (CQS, success rate, completion time) and surfaces insights to T0.

CLI:
    python3 scripts/lib/dispatch_parameter_tracker.py analyze    # show top insights
    python3 scripts/lib/dispatch_parameter_tracker.py recommend  # show recommendations
    python3 scripts/lib/dispatch_parameter_tracker.py stats      # experiment count + summary

BILLING SAFETY: No Anthropic SDK. SQLite + stdlib only.
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
import sys
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Optional

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).parent))

try:
    from vnx_paths import ensure_env
    _PATHS = ensure_env()
    _STATE_DIR = Path(_PATHS["VNX_STATE_DIR"])
except Exception:
    _STATE_DIR = Path(__file__).resolve().parents[2] / ".vnx-data" / "state"

DB_PATH = _STATE_DIR / "dispatch_tracker.db"

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class DispatchParameters:
    instruction_char_count: int
    context_item_count: int       # intelligence injection items
    repo_map_symbol_count: int    # from F55 repo map
    role: str
    cognition: str                # "high", "medium", "low"
    model: str
    terminal: str
    file_count: int               # files mentioned in dispatch instruction


@dataclass
class DispatchOutcome:
    cqs: Optional[float]          # Composite Quality Score from receipt
    success: bool
    completion_minutes: float
    test_count: int
    committed: bool
    lines_changed: int


@dataclass
class Insight:
    dimension: str                # e.g. "instruction_chars", "role"
    group_a: str                  # e.g. "> 2000 chars"
    group_b: str                  # e.g. "<= 2000 chars"
    metric: str                   # e.g. "avg_cqs"
    value_a: float
    value_b: float
    sample_a: int
    sample_b: int

    def summary(self) -> str:
        diff = self.value_a - self.value_b
        direction = "higher" if diff > 0 else "lower"
        return (
            f"{self.dimension}: {self.group_a} ({self.value_a:.1f}, n={self.sample_a}) "
            f"vs {self.group_b} ({self.value_b:.1f}, n={self.sample_b}) "
            f"— {abs(diff):.1f} {direction}"
        )


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _get_db_path(state_dir: Optional[Path] = None) -> Path:
    base = state_dir or _STATE_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base / "dispatch_tracker.db"


@contextmanager
def _connect(state_dir: Optional[Path] = None) -> Generator[sqlite3.Connection, None, None]:
    db_path = _get_db_path(state_dir)
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema(state_dir: Optional[Path] = None) -> None:
    """Idempotent schema creation for dispatch_experiments table."""
    with _connect(state_dir) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS dispatch_experiments (
                id INTEGER PRIMARY KEY,
                dispatch_id TEXT UNIQUE,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                -- parameters
                instruction_chars INTEGER,
                context_items INTEGER,
                repo_map_symbols INTEGER,
                role TEXT,
                cognition TEXT,
                model TEXT,
                terminal TEXT,
                file_count INTEGER,
                -- outcomes (nullable until filled in)
                success BOOLEAN,
                cqs REAL,
                completion_minutes REAL,
                test_count INTEGER,
                committed BOOLEAN,
                lines_changed INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_de_dispatch_id
                ON dispatch_experiments (dispatch_id);
            CREATE INDEX IF NOT EXISTS idx_de_role
                ON dispatch_experiments (role);
            CREATE INDEX IF NOT EXISTS idx_de_timestamp
                ON dispatch_experiments (timestamp DESC);
        """)


# ---------------------------------------------------------------------------
# Parameter extraction helpers
# ---------------------------------------------------------------------------


def _count_context_items(instruction: str) -> int:
    """Count intelligence-injected context sections in instruction text."""
    markers = [
        r"^#{1,3}\s*(context|intelligence|patterns|insights|recent dispatches)",
        r"^---\s*context",
        r"\[CONTEXT\]",
        r"dispatch_insights",
        r"success_patterns",
    ]
    count = 0
    for line in instruction.splitlines():
        for pattern in markers:
            if re.search(pattern, line, re.IGNORECASE):
                count += 1
                break
    return count


def _count_repo_map_symbols(repo_map: Optional[str]) -> int:
    """Count symbol entries from repo map string."""
    if not repo_map:
        return 0
    # Repo map lines typically contain function/class names with indentation
    symbol_lines = [
        ln for ln in repo_map.splitlines()
        if ln.strip() and not ln.strip().startswith("#") and len(ln.strip()) > 2
    ]
    return len(symbol_lines)


def _count_file_mentions(instruction: str) -> int:
    """Count distinct file paths mentioned in instruction."""
    file_pattern = re.compile(
        r"\b[\w./\-]+\.(?:py|ts|tsx|js|sh|yaml|yml|json|md|sql|toml)\b"
    )
    matches = set(file_pattern.findall(instruction))
    return len(matches)


def _infer_cognition(instruction: str, role: Optional[str]) -> str:
    """Infer cognition level from instruction complexity signals."""
    length = len(instruction)
    complexity_signals = [
        "architecture", "design", "analyze", "investigate",
        "complex", "refactor", "optimize",
    ]
    signal_count = sum(
        1 for s in complexity_signals
        if s in instruction.lower()
    )
    if length > 3000 or signal_count >= 3:
        return "high"
    if length > 1500 or signal_count >= 1:
        return "medium"
    return "low"


def extract_parameters(
    instruction: str,
    terminal_id: str,
    model: str,
    role: Optional[str] = None,
    repo_map: Optional[str] = None,
) -> DispatchParameters:
    """Extract DispatchParameters from dispatch metadata."""
    return DispatchParameters(
        instruction_char_count=len(instruction),
        context_item_count=_count_context_items(instruction),
        repo_map_symbol_count=_count_repo_map_symbols(repo_map),
        role=role or "unknown",
        cognition=_infer_cognition(instruction, role),
        model=model,
        terminal=terminal_id,
        file_count=_count_file_mentions(instruction),
    )


def _count_lines_changed(since_timestamp: str) -> int:
    """Count lines changed via git diff --stat since timestamp."""
    try:
        import subprocess
        result = subprocess.run(
            ["git", "log", "--oneline", f"--since={since_timestamp}", "--numstat"],
            capture_output=True, text=True, timeout=10,
        )
        total = 0
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                try:
                    total += int(parts[0]) + int(parts[1])
                except ValueError:
                    pass
        return total
    except Exception:
        return 0


def _lookup_cqs(dispatch_id: str, state_dir: Optional[Path] = None) -> Optional[float]:
    """Look up CQS from quality_intelligence.db dispatch_metadata."""
    try:
        base = state_dir or _STATE_DIR
        qi_db = base / "quality_intelligence.db"
        if not qi_db.exists():
            return None
        conn = sqlite3.connect(str(qi_db), timeout=5)
        try:
            row = conn.execute(
                "SELECT cqs FROM dispatch_metadata WHERE dispatch_id = ? LIMIT 1",
                (dispatch_id,),
            ).fetchone()
            return float(row[0]) if row and row[0] is not None else None
        finally:
            conn.close()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Tracker class
# ---------------------------------------------------------------------------


class DispatchParameterTracker:
    """Track and analyze dispatch parameters vs outcomes."""

    def __init__(self, state_dir: Optional[Path] = None) -> None:
        self._state_dir = state_dir or _STATE_DIR
        init_schema(self._state_dir)

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def capture_parameters(self, dispatch_id: str, params: DispatchParameters) -> None:
        """Insert or upsert parameter record before dispatch execution."""
        with _connect(self._state_dir) as conn:
            conn.execute(
                """
                INSERT INTO dispatch_experiments (
                    dispatch_id, timestamp,
                    instruction_chars, context_items, repo_map_symbols,
                    role, cognition, model, terminal, file_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(dispatch_id) DO UPDATE SET
                    instruction_chars = excluded.instruction_chars,
                    context_items     = excluded.context_items,
                    repo_map_symbols  = excluded.repo_map_symbols,
                    role              = excluded.role,
                    cognition         = excluded.cognition,
                    model             = excluded.model,
                    terminal          = excluded.terminal,
                    file_count        = excluded.file_count
                """,
                (
                    dispatch_id,
                    datetime.now(timezone.utc).isoformat(),
                    params.instruction_char_count,
                    params.context_item_count,
                    params.repo_map_symbol_count,
                    params.role,
                    params.cognition,
                    params.model,
                    params.terminal,
                    params.file_count,
                ),
            )

    def capture_outcome(self, dispatch_id: str, outcome: DispatchOutcome) -> None:
        """Update outcome columns for an existing experiment record."""
        with _connect(self._state_dir) as conn:
            conn.execute(
                """
                UPDATE dispatch_experiments
                SET success = ?, cqs = ?, completion_minutes = ?,
                    test_count = ?, committed = ?, lines_changed = ?
                WHERE dispatch_id = ?
                """,
                (
                    1 if outcome.success else 0,
                    outcome.cqs,
                    outcome.completion_minutes,
                    outcome.test_count,
                    1 if outcome.committed else 0,
                    outcome.lines_changed,
                    dispatch_id,
                ),
            )

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------

    def _fetch_completed(self) -> list[dict]:
        """Return rows that have both parameters and outcomes."""
        with _connect(self._state_dir) as conn:
            rows = conn.execute(
                """
                SELECT * FROM dispatch_experiments
                WHERE success IS NOT NULL
                ORDER BY timestamp DESC
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def analyze(self, min_experiments: int = 20) -> list[Insight]:
        """Produce comparative insights from completed experiments.

        Returns empty list when fewer than min_experiments rows have outcomes.
        """
        rows = self._fetch_completed()
        if len(rows) < min_experiments:
            return []

        insights: list[Insight] = []

        # Helper: avg metric for a filtered subset
        def _avg(subset: list[dict], key: str) -> tuple[float, int]:
            values = [r[key] for r in subset if r.get(key) is not None]
            if not values:
                return 0.0, 0
            return round(statistics.mean(values), 2), len(values)

        # 1. instruction_chars split at 2000
        large = [r for r in rows if (r["instruction_chars"] or 0) > 2000]
        small = [r for r in rows if (r["instruction_chars"] or 0) <= 2000]
        if large and small:
            cqs_large, n_large = _avg(large, "cqs")
            cqs_small, n_small = _avg(small, "cqs")
            if n_large >= 3 and n_small >= 3:
                insights.append(Insight(
                    dimension="instruction_chars",
                    group_a="> 2000 chars",
                    group_b="<= 2000 chars",
                    metric="avg_cqs",
                    value_a=cqs_large,
                    value_b=cqs_small,
                    sample_a=n_large,
                    sample_b=n_small,
                ))

        # 2. context_items: 2+ vs 0
        ctx_rich = [r for r in rows if (r["context_items"] or 0) >= 2]
        ctx_none = [r for r in rows if (r["context_items"] or 0) == 0]
        if ctx_rich and ctx_none:
            success_rich = round(
                sum(1 for r in ctx_rich if r["success"]) / len(ctx_rich) * 100, 1
            )
            success_none = round(
                sum(1 for r in ctx_none if r["success"]) / len(ctx_none) * 100, 1
            )
            if len(ctx_rich) >= 3 and len(ctx_none) >= 3:
                insights.append(Insight(
                    dimension="context_items",
                    group_a=">= 2 items",
                    group_b="0 items",
                    metric="success_rate_%",
                    value_a=success_rich,
                    value_b=success_none,
                    sample_a=len(ctx_rich),
                    sample_b=len(ctx_none),
                ))

        # 3. Per-role average completion time
        roles = {}
        for r in rows:
            role = r.get("role") or "unknown"
            roles.setdefault(role, []).append(r)

        role_items = sorted(roles.items(), key=lambda kv: len(kv[1]), reverse=True)
        if len(role_items) >= 2:
            role_a_name, role_a_rows = role_items[0]
            role_b_name, role_b_rows = role_items[1]
            mins_a, n_a = _avg(role_a_rows, "completion_minutes")
            mins_b, n_b = _avg(role_b_rows, "completion_minutes")
            if n_a >= 3 and n_b >= 3:
                insights.append(Insight(
                    dimension="role",
                    group_a=role_a_name,
                    group_b=role_b_name,
                    metric="avg_completion_min",
                    value_a=mins_a,
                    value_b=mins_b,
                    sample_a=n_a,
                    sample_b=n_b,
                ))

        # 4. cognition level: high vs medium/low
        cog_high = [r for r in rows if r.get("cognition") == "high"]
        cog_low = [r for r in rows if r.get("cognition") in ("medium", "low")]
        if cog_high and cog_low:
            cqs_h, n_h = _avg(cog_high, "cqs")
            cqs_l, n_l = _avg(cog_low, "cqs")
            if n_h >= 3 and n_l >= 3:
                insights.append(Insight(
                    dimension="cognition",
                    group_a="high",
                    group_b="medium/low",
                    metric="avg_cqs",
                    value_a=cqs_h,
                    value_b=cqs_l,
                    sample_a=n_h,
                    sample_b=n_l,
                ))

        # 5. repo_map_symbols: with vs without
        with_map = [r for r in rows if (r["repo_map_symbols"] or 0) > 0]
        without_map = [r for r in rows if (r["repo_map_symbols"] or 0) == 0]
        if with_map and without_map:
            cqs_wm, n_wm = _avg(with_map, "cqs")
            cqs_nom, n_nom = _avg(without_map, "cqs")
            if n_wm >= 3 and n_nom >= 3:
                insights.append(Insight(
                    dimension="repo_map",
                    group_a="with symbols",
                    group_b="no symbols",
                    metric="avg_cqs",
                    value_a=cqs_wm,
                    value_b=cqs_nom,
                    sample_a=n_wm,
                    sample_b=n_nom,
                ))

        # Sort by absolute effect size (largest first)
        insights.sort(key=lambda i: abs(i.value_a - i.value_b), reverse=True)
        return insights[:10]

    def get_recommended_parameters(
        self, role: str = "", task_type: str = ""
    ) -> dict:
        """Return optimal parameter ranges based on experiment data.

        Falls back to conservative defaults when < 20 experiments exist.
        """
        rows = self._fetch_completed()

        defaults = {
            "instruction_chars": "1500-2500",
            "context_items": "2-3",
            "cognition": "high",
            "file_count": "3-6",
            "note": "defaults (insufficient data)",
        }

        if len(rows) < 20:
            return defaults

        # Filter by role when specified and enough data exists
        role_rows = [r for r in rows if r.get("role") == role] if role else []
        analysis_rows = role_rows if len(role_rows) >= 10 else rows

        # Find optimal instruction_chars range by CQS quartile
        with_cqs = [r for r in analysis_rows if r.get("cqs") is not None]
        if len(with_cqs) >= 10:
            sorted_by_cqs = sorted(with_cqs, key=lambda r: r["cqs"] or 0, reverse=True)
            top_quartile = sorted_by_cqs[: max(1, len(sorted_by_cqs) // 4)]
            char_counts = [r["instruction_chars"] for r in top_quartile if r["instruction_chars"]]
            context_items = [r["context_items"] for r in top_quartile if r["context_items"] is not None]
            file_counts = [r["file_count"] for r in top_quartile if r["file_count"] is not None]

            rec: dict = {"note": "data-driven"}

            if char_counts:
                lo = int(statistics.quantiles(char_counts, n=4)[0])
                hi = int(statistics.quantiles(char_counts, n=4)[2])
                rec["instruction_chars"] = f"{lo}-{hi}"
            else:
                rec["instruction_chars"] = defaults["instruction_chars"]

            if context_items:
                med = int(statistics.median(context_items))
                rec["context_items"] = f"{max(1, med-1)}-{med+1}"
            else:
                rec["context_items"] = defaults["context_items"]

            if file_counts:
                med = int(statistics.median(file_counts))
                rec["file_count"] = f"{max(1, med-1)}-{med+1}"
            else:
                rec["file_count"] = defaults["file_count"]

            # Cognition: pick whichever level has highest avg CQS in top set
            cog_counts: dict[str, list[float]] = {}
            for r in top_quartile:
                cog = r.get("cognition") or "medium"
                val = r.get("cqs")
                if val is not None:
                    cog_counts.setdefault(cog, []).append(val)
            if cog_counts:
                rec["cognition"] = max(cog_counts, key=lambda k: statistics.mean(cog_counts[k]))
            else:
                rec["cognition"] = defaults["cognition"]

            return rec

        return defaults

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        """Return experiment count and summary metrics."""
        with _connect(self._state_dir) as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM dispatch_experiments"
            ).fetchone()[0]
            completed = conn.execute(
                "SELECT COUNT(*) FROM dispatch_experiments WHERE success IS NOT NULL"
            ).fetchone()[0]
            success_count = conn.execute(
                "SELECT COUNT(*) FROM dispatch_experiments WHERE success = 1"
            ).fetchone()[0]
            avg_cqs_row = conn.execute(
                "SELECT AVG(cqs) FROM dispatch_experiments WHERE cqs IS NOT NULL"
            ).fetchone()
            avg_cqs = round(float(avg_cqs_row[0]), 2) if avg_cqs_row[0] is not None else None

        return {
            "total_experiments": total,
            "completed": completed,
            "success_count": success_count,
            "success_rate": round(success_count / completed * 100, 1) if completed > 0 else None,
            "avg_cqs": avg_cqs,
            "insights_available": completed >= 20,
        }

    # ------------------------------------------------------------------
    # T0 context summary
    # ------------------------------------------------------------------

    def top_insights_for_t0(self, n: int = 5) -> list[str]:
        """Return up to n insight summaries for T0 state injection."""
        insights = self.analyze(min_experiments=20)
        return [i.summary() for i in insights[:n]]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_analyze(tracker: DispatchParameterTracker) -> None:
    insights = tracker.analyze(min_experiments=20)
    if not insights:
        s = tracker.stats()
        print(
            f"Insufficient data: {s['completed']} completed experiments "
            f"(need 20). Run more dispatches to enable analysis."
        )
        return
    print(f"Top {len(insights)} dispatch insights:\n")
    for i, ins in enumerate(insights, 1):
        print(f"  {i}. {ins.summary()}")


def _cli_recommend(tracker: DispatchParameterTracker) -> None:
    role = sys.argv[3] if len(sys.argv) > 3 else ""
    rec = tracker.get_recommended_parameters(role=role)
    print(f"Recommended parameters{' for role=' + role if role else ''}:")
    for k, v in rec.items():
        print(f"  {k}: {v}")


def _cli_stats(tracker: DispatchParameterTracker) -> None:
    s = tracker.stats()
    print("Dispatch experiment stats:")
    for k, v in s.items():
        print(f"  {k}: {v}")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: dispatch_parameter_tracker.py {analyze|recommend|stats}")
        sys.exit(1)

    tracker = DispatchParameterTracker()
    cmd = sys.argv[1]

    if cmd == "analyze":
        _cli_analyze(tracker)
    elif cmd == "recommend":
        _cli_recommend(tracker)
    elif cmd == "stats":
        _cli_stats(tracker)
    else:
        print(f"Unknown command: {cmd}. Use analyze|recommend|stats")
        sys.exit(1)


if __name__ == "__main__":
    main()
