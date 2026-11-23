#!/usr/bin/env python3
"""
Quality Gate Intelligence Query Tool

Queries t0_intelligence.ndjson for pattern analysis and insights.

Usage:
    # Terminal quality metrics (last 30 days)
    python query_quality_intelligence.py --terminal T1 --days 30

    # Common failure patterns (last 7 days)
    python query_quality_intelligence.py --failures --days 7

    # Terminal learning trajectory (last 90 days)
    python query_quality_intelligence.py --learning --terminal T1 --days 90

    # Lessons learned summary
    python query_quality_intelligence.py --lessons

    # Full intelligence report
    python query_quality_intelligence.py --report --terminal T1 --days 30
"""

import argparse
import os
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, List, Tuple
from collections import defaultdict


def load_intelligence_events(
    intelligence_file: Path,
    event_types: List[str] = None,
    terminal: str = None,
    days_back: int = 30
) -> List[Dict[str, Any]]:
    """Load intelligence events from t0_intelligence.ndjson"""

    if not intelligence_file.exists():
        return []

    events = []
    cutoff_date = datetime.now() - timedelta(days=days_back)

    with open(intelligence_file, 'r') as f:
        for line in f:
            try:
                event = json.loads(line.strip())

                # Filter by event type
                if event_types and event.get("event_type") not in event_types:
                    continue

                # Filter by terminal
                if terminal and event.get("terminal") != terminal:
                    continue

                # Filter by date
                event_date = datetime.fromisoformat(event.get("timestamp", "").replace("Z", ""))
                if event_date < cutoff_date:
                    continue

                events.append(event)
            except (json.JSONDecodeError, ValueError):
                continue

    return events


def calculate_terminal_success_rate(terminal: str, days: int, intelligence_file: Path) -> Dict[str, Any]:
    """Calculate quality gate success rate per terminal"""

    events = load_intelligence_events(
        intelligence_file,
        event_types=["quality_gate_verification", "quality_gate_failure"],
        terminal=terminal,
        days_back=days
    )

    successes = sum(1 for e in events if e.get("verification", {}).get("verdict") == "pass")
    failures = sum(1 for e in events if e.get("verification", {}).get("verdict") == "fail")
    total = successes + failures

    return {
        "terminal": terminal,
        "success_rate": successes / total if total > 0 else 0.0,
        "total_verifications": total,
        "successes": successes,
        "failures": failures,
        "days": days
    }


def analyze_failure_patterns(days: int, intelligence_file: Path) -> List[Tuple[str, Dict[str, Any]]]:
    """Identify most common failure categories"""

    events = load_intelligence_events(
        intelligence_file,
        event_types=["quality_gate_failure"],
        days_back=days
    )

    patterns = defaultdict(lambda: {
        "count": 0,
        "terminals": set(),
        "severity": "unknown",
        "examples": []
    })

    for event in events:
        for failure in event.get("failures", []):
            category = failure["category"]
            patterns[category]["count"] += 1
            patterns[category]["terminals"].add(event["terminal"])
            patterns[category]["severity"] = failure.get("severity", "unknown")
            if len(patterns[category]["examples"]) < 3:
                patterns[category]["examples"].append(failure.get("details", ""))

    # Convert sets to lists for JSON serialization
    for pattern in patterns.values():
        pattern["terminals"] = list(pattern["terminals"])

    # Sort by frequency
    sorted_patterns = sorted(
        patterns.items(),
        key=lambda x: x[1]["count"],
        reverse=True
    )

    return sorted_patterns


def analyze_terminal_learning(terminal: str, days: int, intelligence_file: Path) -> Dict[str, Any]:
    """Track improvement over time (retry success rate)"""

    retry_events = load_intelligence_events(
        intelligence_file,
        event_types=["quality_gate_retry"],
        terminal=terminal,
        days_back=days
    )

    retry_successes = sum(1 for e in retry_events if e.get("retry_success"))
    total_retries = len(retry_events)

    # Group by month to see trend
    monthly_stats = defaultdict(lambda: {"successes": 0, "total": 0})

    for event in retry_events:
        month = event["timestamp"][:7]  # YYYY-MM
        monthly_stats[month]["total"] += 1
        if event.get("retry_success"):
            monthly_stats[month]["successes"] += 1

    # Calculate trend (improving if recent months have higher success rates)
    is_improving = False
    if len(monthly_stats) >= 2:
        months = sorted(monthly_stats.keys())
        recent_rate = monthly_stats[months[-1]]["successes"] / max(monthly_stats[months[-1]]["total"], 1)
        older_rate = monthly_stats[months[0]]["successes"] / max(monthly_stats[months[0]]["total"], 1)
        is_improving = recent_rate > older_rate

    return {
        "terminal": terminal,
        "overall_retry_success_rate": retry_successes / total_retries if total_retries > 0 else 0.0,
        "total_retries": total_retries,
        "monthly_trend": dict(monthly_stats),
        "is_improving": is_improving
    }


def generate_lessons_learned(days: int, intelligence_file: Path) -> List[Dict[str, Any]]:
    """Generate lessons learned from failure patterns"""

    patterns = analyze_failure_patterns(days, intelligence_file)
    lessons = []

    for category, data in patterns:
        if data["count"] >= 3:  # Only create lessons for recurring issues
            lesson = {
                "lesson_id": f"LESSON-{len(lessons) + 1:03d}",
                "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",  # FIX: Use UTC, not local time
                "category": category,
                "severity": data["severity"],
                "observation": f"Category '{category}' occurred {data['count']} times across terminals {', '.join(data['terminals'])}",
                "pattern": {
                    "frequency": data["count"],
                    "terminals": data["terminals"],
                    "time_period": f"Last {days} days"
                },
                "examples": data["examples"][:3]
            }
            lessons.append(lesson)

    return lessons


def generate_full_report(terminal: str, days: int, intelligence_file: Path) -> str:
    """Generate comprehensive quality gate intelligence report"""

    success_metrics = calculate_terminal_success_rate(terminal, days, intelligence_file)
    learning_metrics = analyze_terminal_learning(terminal, days, intelligence_file)
    failure_patterns = analyze_failure_patterns(days, intelligence_file)

    report = []
    report.append("=" * 60)
    report.append("QUALITY GATE INTELLIGENCE REPORT")
    report.append("=" * 60)
    report.append(f"Terminal: {terminal}")
    report.append(f"Period: Last {days} days")
    report.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    report.append("")

    # Success Rate
    report.append("SUCCESS RATE")
    report.append("-" * 60)
    success_rate = success_metrics["success_rate"] * 100
    report.append(f"Overall: {success_rate:.1f}% ({success_metrics['successes']}/{success_metrics['total_verifications']} verifications)")
    report.append(f"- Successes: {success_metrics['successes']}")
    report.append(f"- Failures: {success_metrics['failures']}")
    report.append("")

    # Learning Trajectory
    if learning_metrics["total_retries"] > 0:
        report.append("LEARNING TRAJECTORY")
        report.append("-" * 60)
        retry_rate = learning_metrics["overall_retry_success_rate"] * 100
        report.append(f"Retry Success Rate: {retry_rate:.1f}% ({learning_metrics['total_retries']} retries)")
        report.append(f"Status: {'IMPROVING ↑' if learning_metrics['is_improving'] else 'STABLE →'}")

        if learning_metrics["monthly_trend"]:
            report.append("")
            report.append("Monthly Trend:")
            for month, stats in sorted(learning_metrics["monthly_trend"].items()):
                month_rate = (stats["successes"] / stats["total"] * 100) if stats["total"] > 0 else 0
                report.append(f"  {month}: {month_rate:.1f}% ({stats['successes']}/{stats['total']})")
        report.append("")

    # Common Failures
    if failure_patterns:
        report.append("COMMON FAILURE CATEGORIES")
        report.append("-" * 60)
        for idx, (category, data) in enumerate(failure_patterns[:5], 1):
            report.append(f"{idx}. {category} ({data['count']} occurrences)")
            report.append(f"   Terminals: {', '.join(data['terminals'])}")
            report.append(f"   Severity: {data['severity']}")
            if data["examples"]:
                report.append(f"   Example: {data['examples'][0]}")
            report.append("")

    # Recommendation
    report.append("RECOMMENDATION")
    report.append("-" * 60)
    if success_rate >= 90:
        report.append("✅ Excellent performance. Continue monitoring for consistency.")
    elif success_rate >= 70:
        report.append("⚠️  Good progress. Focus on addressing common failure patterns.")
    else:
        report.append("🚨 Needs improvement. Review failure patterns and provide additional training.")

    report.append("=" * 60)

    return "\n".join(report)


def main():
    parser = argparse.ArgumentParser(description="Query quality gate intelligence for pattern analysis")

    parser.add_argument("--terminal", help="Terminal to analyze (T1, T2, T3)")
    parser.add_argument("--days", type=int, default=30, help="Days to look back (default: 30)")

    # Query types
    parser.add_argument("--failures", action="store_true", help="Analyze failure patterns")
    parser.add_argument("--learning", action="store_true", help="Analyze learning trajectory")
    parser.add_argument("--lessons", action="store_true", help="Generate lessons learned")
    parser.add_argument("--report", action="store_true", help="Generate full intelligence report")

    # Output format
    parser.add_argument("--json", action="store_true", help="Output JSON format")

    args = parser.parse_args()

    # Resolve VNX path
    vnx_home = os.environ.get("VNX_HOME") or str(Path(__file__).resolve().parents[1])
    intelligence_file = Path(os.environ.get("VNX_STATE_DIR") or Path(vnx_home) / "state") / "t0_intelligence.ndjson"

    if not intelligence_file.exists():
        print(f"❌ Intelligence file not found: {intelligence_file}", file=sys.stderr)
        print("No quality gate events have been logged yet.", file=sys.stderr)
        sys.exit(1)

    # Execute query
    if args.report:
        if not args.terminal:
            print("❌ --report requires --terminal", file=sys.stderr)
            sys.exit(1)

        report = generate_full_report(args.terminal, args.days, intelligence_file)
        print(report)

    elif args.failures:
        patterns = analyze_failure_patterns(args.days, intelligence_file)

        if args.json:
            print(json.dumps(dict(patterns), indent=2))
        else:
            print("=" * 60)
            print(f"COMMON FAILURE PATTERNS (Last {args.days} days)")
            print("=" * 60)

            if not patterns:
                print("No failures recorded in this period.")
            else:
                for idx, (category, data) in enumerate(patterns, 1):
                    print(f"{idx}. {category}")
                    print(f"   Count: {data['count']}")
                    print(f"   Terminals: {', '.join(data['terminals'])}")
                    print(f"   Severity: {data['severity']}")
                    if data["examples"]:
                        print(f"   Example: {data['examples'][0]}")
                    print()

    elif args.learning:
        if not args.terminal:
            print("❌ --learning requires --terminal", file=sys.stderr)
            sys.exit(1)

        metrics = analyze_terminal_learning(args.terminal, args.days, intelligence_file)

        if args.json:
            print(json.dumps(metrics, indent=2))
        else:
            print("=" * 60)
            print(f"LEARNING TRAJECTORY: {args.terminal} (Last {args.days} days)")
            print("=" * 60)
            print(f"Retry Success Rate: {metrics['overall_retry_success_rate'] * 100:.1f}%")
            print(f"Total Retries: {metrics['total_retries']}")
            print(f"Status: {'IMPROVING ↑' if metrics['is_improving'] else 'STABLE →'}")

            if metrics["monthly_trend"]:
                print("\nMonthly Trend:")
                for month, stats in sorted(metrics["monthly_trend"].items()):
                    rate = (stats["successes"] / stats["total"] * 100) if stats["total"] > 0 else 0
                    print(f"  {month}: {rate:.1f}% ({stats['successes']}/{stats['total']})")

    elif args.lessons:
        lessons = generate_lessons_learned(args.days, intelligence_file)

        if args.json:
            print(json.dumps(lessons, indent=2))
        else:
            print("=" * 60)
            print(f"LESSONS LEARNED (Last {args.days} days)")
            print("=" * 60)

            if not lessons:
                print("No recurring patterns identified (need 3+ occurrences).")
            else:
                for lesson in lessons:
                    print(f"\n{lesson['lesson_id']}: {lesson['category']}")
                    print(f"Severity: {lesson['severity']}")
                    print(f"Observation: {lesson['observation']}")
                    print(f"Frequency: {lesson['pattern']['frequency']} times")
                    print(f"Terminals: {', '.join(lesson['pattern']['terminals'])}")
                    if lesson["examples"]:
                        print(f"Example: {lesson['examples'][0]}")

    elif args.terminal:
        # Default: show success rate
        metrics = calculate_terminal_success_rate(args.terminal, args.days, intelligence_file)

        if args.json:
            print(json.dumps(metrics, indent=2))
        else:
            print("=" * 60)
            print(f"QUALITY GATE METRICS: {args.terminal} (Last {args.days} days)")
            print("=" * 60)
            print(f"Success Rate: {metrics['success_rate'] * 100:.1f}%")
            print(f"Total Verifications: {metrics['total_verifications']}")
            print(f"- Successes: {metrics['successes']}")
            print(f"- Failures: {metrics['failures']}")

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
