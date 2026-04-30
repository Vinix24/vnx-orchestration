#!/usr/bin/env python3
"""Recommendation aggregator — merges F57, ARC-3 classifier, and learning loop signals.

Reads from three sources:
  - F57 dispatch parameter insights (Karpathy correlations)
  - ARC-3 receipt classifier queue (receipt_classifier_queue.ndjson)
  - Learning loop confidence trends (pattern_usage table)

Clusters suggestions by target_file; upgrades confidence when the same
suggestion appears across N >= 3 sources.  Writes to t0_recommendations.json.

CLI:
    python3 scripts/lib/recommendation_aggregator.py [--state-dir PATH]

BILLING SAFETY: No Anthropic SDK. SQLite + stdlib only.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

from dispatch_parameter_tracker import _STATE_DIR
from f57_insights_reader import read_insights

_CLUSTER_THRESHOLD = 3


def _read_classifier_queue(state_dir: Path) -> list[dict]:
    """Read (non-destructively) the ARC-3 receipt classifier queue."""
    queue_path = state_dir / "receipt_classifier_queue.ndjson"
    if not queue_path.is_file():
        return []
    records: list[dict] = []
    try:
        for line in queue_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                receipt = obj.get("receipt") if isinstance(obj, dict) else obj
                if isinstance(receipt, dict):
                    records.append(receipt)
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return records


def _read_confidence_trends(state_dir: Path) -> list[dict]:
    """Return patterns with declining confidence from the learning loop DB."""
    db_path = state_dir / "quality_intelligence.db"
    if not db_path.is_file():
        return []
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT pattern_id, pattern_title, confidence, failure_count, used_count
                FROM pattern_usage
                WHERE confidence < 0.95
                ORDER BY confidence ASC
                LIMIT 20
                """
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        return []


def _suggestions_from_f57(insights_output: dict) -> list[dict]:
    suggestions: list[dict] = []
    for text in insights_output.get("all_time_insights") or []:
        suggestions.append(
            {"target_file": "CLAUDE.md", "suggestion_text": text, "source": "f57", "confidence": 0.75}
        )
    for text in insights_output.get("window_insights") or []:
        suggestions.append(
            {"target_file": "CLAUDE.md", "suggestion_text": text, "source": "f57", "confidence": 0.70}
        )
    return suggestions


def _suggestions_from_classifier(receipts: list[dict]) -> list[dict]:
    suggestions: list[dict] = []
    for receipt in receipts:
        for key in ("suggested_improvements", "open_items", "recommendations"):
            items = receipt.get(key)
            if isinstance(items, list):
                for item in items[:3]:
                    text = str(item).strip()
                    if len(text) > 10:
                        suggestions.append(
                            {
                                "target_file": "CLAUDE.md",
                                "suggestion_text": text[:200],
                                "source": "classifier",
                                "confidence": 0.65,
                            }
                        )
            elif isinstance(items, str) and len(items.strip()) > 10:
                suggestions.append(
                    {
                        "target_file": "CLAUDE.md",
                        "suggestion_text": items.strip()[:200],
                        "source": "classifier",
                        "confidence": 0.65,
                    }
                )
    return suggestions


def _suggestions_from_learning_loop(patterns: list[dict]) -> list[dict]:
    suggestions: list[dict] = []
    for p in patterns:
        title = p.get("pattern_title") or p.get("pattern_id") or ""
        confidence = float(p.get("confidence") or 0.5)
        text = f"Pattern '{title}' has low confidence ({confidence:.2f}) — review and update"
        pid = str(p.get("pattern_id") or "")
        target = f"skills/{pid.split('/')[-1]}.md" if "skill" in pid.lower() else "CLAUDE.md"
        suggestions.append(
            {
                "target_file": target,
                "suggestion_text": text,
                "source": "learning_loop",
                "confidence": min(0.60 + (1.0 - confidence), 0.90),
            }
        )
    return suggestions


def _cluster(all_suggestions: list[dict]) -> list[dict]:
    """Deduplicate by suggestion_text (first 80 chars, lowercased) and upgrade
    confidence when the same suggestion is seen >= _CLUSTER_THRESHOLD times."""
    clusters: dict[str, dict] = {}
    for s in all_suggestions:
        key = s["suggestion_text"].lower().strip()[:80]
        if key not in clusters:
            clusters[key] = {
                "target_file": s["target_file"],
                "suggestion_text": s["suggestion_text"],
                "confidence": s["confidence"],
                "source": [s["source"]],
                "count": 1,
            }
        else:
            entry = clusters[key]
            entry["count"] += 1
            if s["source"] not in entry["source"]:
                entry["source"].append(s["source"])
            entry["confidence"] = min(1.0, entry["confidence"] + 0.05)
            if entry["count"] >= _CLUSTER_THRESHOLD:
                entry["confidence"] = min(1.0, entry["confidence"] + 0.10)
    return sorted(clusters.values(), key=lambda x: x["confidence"], reverse=True)


def aggregate(state_dir: Optional[Path] = None) -> list[dict]:
    """Aggregate suggestions from all three sources."""
    sdir = state_dir or _STATE_DIR
    insights_output = read_insights(days=7, state_dir=sdir)
    classifier_receipts = _read_classifier_queue(sdir)
    confidence_trends = _read_confidence_trends(sdir)

    all_suggestions = (
        _suggestions_from_f57(insights_output)
        + _suggestions_from_classifier(classifier_receipts)
        + _suggestions_from_learning_loop(confidence_trends)
    )
    return _cluster(all_suggestions)


def write_recommendations(output_path: Path, suggestions: list[dict]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "aggregator_version": "1.0.0",
        "cluster_threshold": _CLUSTER_THRESHOLD,
        "total_suggestions": len(suggestions),
        "suggestions": suggestions,
    }
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="VNX recommendation aggregator")
    parser.add_argument("--state-dir", metavar="PATH", help="Override VNX state directory")
    args = parser.parse_args(argv)

    sdir = Path(args.state_dir) if args.state_dir else _STATE_DIR
    suggestions = aggregate(state_dir=sdir)
    output_path = sdir / "t0_recommendations.json"
    write_recommendations(output_path, suggestions)
    print(
        json.dumps({"status": "ok", "suggestions": len(suggestions), "output": str(output_path)})
    )


if __name__ == "__main__":
    main()
