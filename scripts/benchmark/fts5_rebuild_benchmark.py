#!/usr/bin/env python3
"""FTS5 rebuild benchmark for quality_intelligence.db.

Measures rebuild time on a COPY of the source DB. Never mutates production.
Output: JSON + Markdown report in --report-dir for maintenance window planning.

Usage:
    python3 scripts/benchmark/fts5_rebuild_benchmark.py \
        --source-db /path/to/quality_intelligence.db \
        --work-dir ~/Archive/fts5-benchmark-2026-05-20/ \
        --tables code_snippets,snippet_metadata \
        --report-dir claudedocs/
"""

import argparse
import json
import os
import shutil
import sqlite3
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

try:
    import psutil

    _HAVE_PSUTIL = True
except ImportError:
    _HAVE_PSUTIL = False


def _file_size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def _is_fts5_table(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    row = cur.fetchone()
    if row is None:
        return False
    sql = (row[0] or "").upper()
    return "FTS5" in sql


def _row_count(conn: sqlite3.Connection, table: str) -> int:
    """Return row count for *table*. Raises sqlite3.Error on failure — let the caller handle it."""
    return conn.execute(f"SELECT count(*) FROM [{table}]").fetchone()[0]


def _baseline_memory_rss() -> int:
    if _HAVE_PSUTIL:
        return psutil.Process(os.getpid()).memory_info().rss
    return 0


def _current_memory_rss() -> int:
    if _HAVE_PSUTIL:
        return psutil.Process(os.getpid()).memory_info().rss
    return 0


def _disk_io_snapshot() -> dict:
    if _HAVE_PSUTIL:
        try:
            io = psutil.Process(os.getpid()).io_counters()
            return {"read_bytes": io.read_bytes, "write_bytes": io.write_bytes}
        except (AttributeError, psutil.AccessDenied):
            pass
    return {"read_bytes": 0, "write_bytes": 0}


def _copy_db(source: Path, work_dir: Path) -> Path:
    work_dir.mkdir(parents=True, exist_ok=True)
    dest = work_dir / source.name
    # Always copy fresh to keep runs idempotent
    shutil.copy2(source, dest)
    # Copy WAL and SHM if present (to get consistent checkpoint state)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(source) + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, work_dir / (source.name + suffix))
    return dest


def _validate_fts5(conn: sqlite3.Connection, table: str, expected_rows: int) -> dict:
    actual = _row_count(conn, table)
    row_match = actual == expected_rows

    # Run a sample MATCH query to confirm FTS is functional
    match_ok = False
    match_error = None
    try:
        cur = conn.execute(
            f"SELECT count(*) FROM [{table}] WHERE [{table}] MATCH 'python'",
            (),
        )
        match_count = cur.fetchone()[0]
        match_ok = True
    except Exception as exc:
        match_count = -1
        match_error = str(exc)

    return {
        "expected_rows": expected_rows,
        "actual_rows": actual,
        "row_match": row_match,
        "match_query_ok": match_ok,
        "match_query_count": match_count,
        "match_query_error": match_error,
    }


def _benchmark_table(
    db_path: Path,
    table: str,
    pre_rss: int,
    pre_io: dict,
) -> dict:
    result: dict = {
        "table": table,
        "is_fts5": False,
        "skipped": False,
        "skip_reason": None,
        "baseline_rows": -1,
        "wall_seconds": None,
        "cpu_seconds": None,
        "peak_rss_bytes": None,
        "peak_rss_delta_bytes": None,
        "io_read_bytes_delta": None,
        "io_write_bytes_delta": None,
        "wal_size_before": 0,
        "wal_size_after": 0,
        "wal_growth_bytes": 0,
        "validation": None,
        "error": None,
    }

    wal_path = Path(str(db_path) + "-wal")

    conn = sqlite3.connect(str(db_path), timeout=3600)
    # Use WAL mode to match production config
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    try:
        if not _is_fts5_table(conn, table):
            result["is_fts5"] = False
            result["skipped"] = True
            result["skip_reason"] = "not an FTS5 virtual table"
            result["baseline_rows"] = _row_count(conn, table)
            return result

        result["is_fts5"] = True
        baseline_rows = _row_count(conn, table)
        result["baseline_rows"] = baseline_rows
        result["wal_size_before"] = _file_size(wal_path)

        io_before = _disk_io_snapshot()
        t_wall_start = time.monotonic()
        t_cpu_start = time.process_time()

        conn.execute(f"INSERT INTO [{table}]([{table}]) VALUES('rebuild')")
        conn.commit()

        t_wall_end = time.monotonic()
        t_cpu_end = time.process_time()
        io_after = _disk_io_snapshot()
        post_rss = _current_memory_rss()

        result["wall_seconds"] = round(t_wall_end - t_wall_start, 3)
        result["cpu_seconds"] = round(t_cpu_end - t_cpu_start, 3)
        result["peak_rss_bytes"] = post_rss
        result["peak_rss_delta_bytes"] = post_rss - pre_rss
        result["io_read_bytes_delta"] = (
            io_after["read_bytes"] - io_before["read_bytes"]
        )
        result["io_write_bytes_delta"] = (
            io_after["write_bytes"] - io_before["write_bytes"]
        )
        result["wal_size_after"] = _file_size(wal_path)
        result["wal_growth_bytes"] = (
            result["wal_size_after"] - result["wal_size_before"]
        )

        # Checkpoint WAL back to main file so validation reads committed data
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()

        result["validation"] = _validate_fts5(conn, table, baseline_rows)

    except Exception as exc:
        result["error"] = traceback.format_exc()
    finally:
        conn.close()

    return result


def _risk_classification(total_wall_seconds: float) -> str:
    minutes = total_wall_seconds / 60
    if minutes < 5:
        return "green"
    elif minutes < 30:
        return "orange"
    return "red"


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = seconds % 60
    return f"{minutes}m {secs:.1f}s"


def _format_bytes(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(b) < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def _write_json_report(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, default=str))
    os.replace(tmp, path)


def _collect_source_info(source_db: Path, work_dir: Path) -> "tuple[dict, Path]":
    """Collect source DB PRAGMA metadata and create a working copy for benchmarking.

    Returns (source_info dict, path to work copy).
    """
    src_conn = sqlite3.connect(str(source_db), timeout=30)
    journal_mode = src_conn.execute("PRAGMA journal_mode").fetchone()[0]
    page_size = src_conn.execute("PRAGMA page_size").fetchone()[0]
    page_count = src_conn.execute("PRAGMA page_count").fetchone()[0]
    src_conn.close()

    source_info: dict = {
        "path": str(source_db),
        "size_bytes": source_db.stat().st_size,
        "journal_mode": journal_mode,
        "page_size": page_size,
        "page_count": page_count,
        "work_copy": "",
    }

    print("[benchmark] Copying DB to work dir...")
    t_copy_start = time.monotonic()
    work_db = _copy_db(source_db, work_dir)
    t_copy_end = time.monotonic()
    print(f"[benchmark] Copy done in {t_copy_end - t_copy_start:.1f}s -> {work_db}")
    source_info["work_copy"] = str(work_db)
    source_info["copy_wall_seconds"] = round(t_copy_end - t_copy_start, 3)

    return source_info, work_db


def _write_reports(report: dict, report_dir: Path, stem: str) -> None:
    """Write JSON and Markdown reports and print their file locations."""
    json_path = report_dir / f"{stem}.json"
    md_path = report_dir / f"{stem}.md"
    _write_json_report(report, json_path)
    _write_md_report(report, md_path)
    print(f"[benchmark] JSON report: {json_path}")
    print(f"[benchmark] MD report:   {md_path}")


def _write_md_report(report: dict, path: Path) -> None:
    lines = []
    meta = report["meta"]
    source = report["source_db"]
    tables = report["tables"]
    summary = report["summary"]

    lines += [
        f"# FTS5 Rebuild Benchmark — {meta['run_date']}",
        "",
        "## Source Database",
        "",
        f"- Path: `{source['path']}`",
        f"- Size: {_format_bytes(source['size_bytes'])} ({source['size_bytes']:,} bytes)",
        f"- Journal mode: {source['journal_mode']}",
        f"- Page size: {source['page_size']} bytes",
        f"- Page count: {source['page_count']:,}",
        f"- Work copy: `{source['work_copy']}`",
        "",
        "## Table Results",
        "",
    ]

    for t in tables:
        lines.append(f"### `{t['table']}`")
        lines.append("")
        if t["skipped"]:
            lines.append(
                f"**Skipped**: {t['skip_reason']} — {t['baseline_rows']:,} rows"
            )
            lines.append("")
            continue

        if t["error"]:
            lines.append(f"**Error during benchmark**: `{t['error']}`")
            lines.append("")
            continue

        lines += [
            f"- Rows before rebuild: {t['baseline_rows']:,}",
            f"- Wall time: **{_format_duration(t['wall_seconds'])}** ({t['wall_seconds']:.3f}s)",
            f"- CPU time: {_format_duration(t['cpu_seconds'])} ({t['cpu_seconds']:.3f}s)",
        ]
        if t["peak_rss_delta_bytes"] is not None:
            lines.append(
                f"- Peak RSS delta: {_format_bytes(t['peak_rss_delta_bytes'])}"
            )
        lines += [
            f"- IO read delta: {_format_bytes(t['io_read_bytes_delta'])}",
            f"- IO write delta: {_format_bytes(t['io_write_bytes_delta'])}",
            f"- WAL growth: {_format_bytes(t['wal_growth_bytes'])}",
        ]

        v = t.get("validation") or {}
        row_match = v.get("row_match", False)
        match_ok = v.get("match_query_ok", False)
        lines += [
            f"- Row count preserved: {'yes' if row_match else 'NO — MISMATCH'}",
            f"- MATCH query functional: {'yes' if match_ok else 'NO'}",
            f"  - Sample MATCH 'python' returned {v.get('match_query_count', 'N/A')} results",
        ]
        lines.append("")

    risk = summary["risk_classification"]
    risk_label = {"green": "GREEN (<5 min)", "orange": "ORANGE (5-30 min)", "red": "RED (>30 min)"}.get(
        risk, risk
    )

    total_w = summary["total_wall_seconds"]
    window = summary["recommended_maintenance_window_seconds"]

    lines += [
        "## Summary",
        "",
        f"- Total FTS5 rebuild wall time: **{_format_duration(total_w)}**",
        f"- Risk classification: **{risk_label}**",
        f"- Recommended Dag 6 maintenance window: **{_format_duration(window)}** (total + 50% buffer)",
        "",
        "## Maintenance Window Recommendation",
        "",
        f"Schedule at least **{_format_duration(window)}** of downtime for FTS5 rebuild during Dag 6.",
        "",
        f"Based on a measured rebuild of {', '.join(t['table'] for t in tables if not t['skipped'] and not t.get('error'))} "
        f"on a {_format_bytes(source['size_bytes'])} database.",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| DB size | {_format_bytes(source['size_bytes'])} |",
    ]
    for t in tables:
        if not t["skipped"] and not t.get("error") and t.get("wall_seconds") is not None:
            lines.append(
                f"| `{t['table']}` rebuild | {_format_duration(t['wall_seconds'])} |"
            )
    lines += [
        f"| Total rebuild | {_format_duration(total_w)} |",
        f"| Recommended window (+50%) | {_format_duration(window)} |",
        f"| Risk | {risk_label} |",
        "",
        "---",
        f"*Generated {meta['run_date']} by fts5_rebuild_benchmark.py*",
    ]

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.replace(tmp, path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark FTS5 rebuild time on a copy of a SQLite database"
    )
    parser.add_argument(
        "--source-db",
        required=True,
        help="Path to the source SQLite database (will NOT be modified)",
    )
    parser.add_argument(
        "--work-dir",
        required=True,
        help="Working directory for the database copy (created if absent)",
    )
    parser.add_argument(
        "--tables",
        default="code_snippets,snippet_metadata",
        help="Comma-separated list of tables to benchmark",
    )
    parser.add_argument(
        "--report-dir",
        default="claudedocs",
        help="Directory for JSON + Markdown output reports",
    )
    parser.add_argument(
        "--report-stem",
        default=None,
        help="File stem for reports (default: fts5-rebuild-benchmark-<date>)",
    )
    args = parser.parse_args()

    source_db = Path(args.source_db).expanduser().resolve()
    work_dir = Path(args.work_dir).expanduser().resolve()
    report_dir = Path(args.report_dir).expanduser()
    tables = [t.strip() for t in args.tables.split(",") if t.strip()]
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stem = args.report_stem or f"fts5-rebuild-benchmark-{run_date}"

    if not source_db.exists():
        raise FileNotFoundError(f"Source DB not found: {source_db}")

    print(f"[benchmark] Source DB: {source_db} ({_format_bytes(source_db.stat().st_size)})")
    print(f"[benchmark] Work dir:  {work_dir}")
    print(f"[benchmark] Tables:    {tables}")

    source_info, work_db = _collect_source_info(source_db, work_dir)

    # --- Benchmark each table ---
    pre_rss = _baseline_memory_rss()
    pre_io = _disk_io_snapshot()
    table_results = []

    for table in tables:
        print(f"[benchmark] Processing table: {table}")
        result = _benchmark_table(work_db, table, pre_rss, pre_io)
        table_results.append(result)
        if result["skipped"]:
            print(f"  -> skipped ({result['skip_reason']}), rows={result['baseline_rows']:,}")
        elif result["error"]:
            print(f"  -> ERROR: {result['error'][:200]}")
        else:
            print(
                f"  -> wall={result['wall_seconds']:.2f}s cpu={result['cpu_seconds']:.2f}s"
                f" rows={result['baseline_rows']:,} valid={result['validation']['row_match']}"
            )

    # --- Summary ---
    fts5_results = [t for t in table_results if t.get("is_fts5") and not t.get("error")]
    total_wall = sum(t["wall_seconds"] for t in fts5_results if t.get("wall_seconds") is not None)
    recommended_window = total_wall * 1.5  # 50% buffer

    report = {
        "meta": {
            "run_date": datetime.now(timezone.utc).isoformat(),
            "script": "scripts/benchmark/fts5_rebuild_benchmark.py",
            "psutil_available": _HAVE_PSUTIL,
        },
        "source_db": source_info,
        "tables": table_results,
        "summary": {
            "fts5_tables_benchmarked": [
                t["table"] for t in fts5_results
            ],
            "total_wall_seconds": round(total_wall, 3),
            "recommended_maintenance_window_seconds": round(recommended_window, 3),
            "risk_classification": _risk_classification(total_wall),
        },
    }

    _write_reports(report, report_dir, stem)

    risk = report["summary"]["risk_classification"]
    print(
        f"[benchmark] Total rebuild: {_format_duration(total_wall)}"
        f" | Risk: {risk.upper()}"
        f" | Recommended window: {_format_duration(recommended_window)}"
    )


if __name__ == "__main__":
    main()
