#!/usr/bin/env python3
"""
VNX Conversation Analyzer — CLI entrypoint.
Version: 1.1.0

Thin CLI shim. All logic lives in the conversation_analyzer/ package.
Run: python3 scripts/conversation_analyzer.py [--max-sessions N] [--dry-run] ...
"""

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from conversation_analyzer import (  # noqa: E402 (path insert above)
    ConversationAnalyzer, Colors, log,
    DB_PATH, PATHS, VNX_BASE, ANALYZER_VERSION,
)


def main():
    parser = argparse.ArgumentParser(
        description="VNX Conversation Analyzer — Nightly Session Mining Pipeline")
    parser.add_argument("--max-sessions", type=int, default=50,
                        help="Max sessions to analyze per run")
    parser.add_argument("--deep-budget", type=int, default=20,
                        help="Max LLM deep analysis calls per run")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse sessions without storing or LLM calls")
    parser.add_argument("--project-filter",
                        help="Only analyze sessions from project matching this string")
    parser.add_argument("--terminal-filter",
                        help="Only analyze sessions from this terminal (T-MANAGER, T1, T2, T3)")
    args = parser.parse_args()

    print(f"\n{Colors.BLUE}{'=' * 70}")
    print("VNX Conversation Analyzer")
    print(f"Version: {ANALYZER_VERSION}")
    print(f"{'=' * 70}{Colors.RESET}\n")

    if not DB_PATH.exists():
        log("ERROR", f"Quality database not found: {DB_PATH}")
        log("INFO", "Run quality_db_init.py first")
        return 1

    analyzer = ConversationAnalyzer(DB_PATH)
    analyzer.connect()

    rc = 0
    run_status = "ok"
    run_error = None
    try:
        analyzer.run(
            max_sessions=args.max_sessions,
            deep_budget=args.deep_budget,
            dry_run=args.dry_run,
            project_filter=args.project_filter,
            terminal_filter=args.terminal_filter,
        )
    except Exception as e:
        log("ERROR", f"Analysis failed: {e}")
        run_status = "fail"
        run_error = str(e)
        rc = 1
    finally:
        analyzer.close()
        try:
            from health_beacon import HealthBeacon
            details = {"max_sessions": args.max_sessions, "dry_run": args.dry_run}
            if run_error:
                details["error"] = run_error
            HealthBeacon(
                Path(PATHS["VNX_DATA_DIR"]),
                "conversation_analyzer",
                expected_interval_seconds=86400,
            ).heartbeat(status=run_status, details=details)
        except (ImportError, OSError, RuntimeError) as exc:
            log("WARNING", f"health_beacon failed: {exc}")

    return rc


if __name__ == "__main__":
    sys.exit(main())
