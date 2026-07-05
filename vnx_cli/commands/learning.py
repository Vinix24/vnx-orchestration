"""vnx learning — operator-gated proposal tier for the intelligence self-learning loop."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from vnx_cli import _engine
_engine.ensure_engine_on_path()

# Representative query scopes for the A/B tag-overlap simulation.
# Each tuple is a plausible set of scope tags a dispatch might carry.
_AB_QUERY_SCOPES: List[List[str]] = [
    ["dispatch", "fix_bug"],
    ["intelligence", "implement_feature"],
    ["governance_gates", "harden"],
    ["receipts_audit", "review_audit"],
    ["schema_migrations", "migrate_schema"],
    ["providers_routing", "wire_integration"],
    ["learning_loop", "implement_feature"],
    ["tests_harness", "add_test"],
]


def _resolve_state_dir(project_dir: Path) -> Path:
    """Return the canonical VNX state directory anchored on project_dir."""
    return _engine.resolve_data_root(project_dir) / "state"


def _cmd_run(args) -> int:
    """Run the daily learning cycle and write pending proposals for operator review."""
    project_dir = Path(getattr(args, "project_dir", "."))
    from_history = getattr(args, "from_history", False)

    # Validate project
    if not (_engine.resolve_data_root(project_dir).exists()):
        print("error: VNX project not initialized. Run `vnx init` first.", file=sys.stderr)
        return 1

    import learning_loop as ll  # type: ignore[import]
    loop = ll.LearningLoop()
    try:
        report = loop.daily_learning_cycle(from_history=from_history)
    finally:
        try:
            loop.conn.close()
        except Exception:
            pass

    proposal_count = report.get("statistics", {}).get("proposal_count", 0)
    print(f"\nProposals (pending rules): {proposal_count}")
    print("\nSummary:")
    print(json.dumps(report.get("statistics", {}), indent=2))
    return 0


def _cmd_status(args) -> int:
    """Show pending proposals and archival candidates."""
    project_dir = Path(getattr(args, "project_dir", "."))
    state_dir = _resolve_state_dir(project_dir)

    pending_rules_path = state_dir / "pending_rules.json"
    pending_archival_path = state_dir / "pending_archival.json"

    print("Learning loop status")
    print("=" * 40)

    # Pending rules
    pending_rules = 0
    approved_rules = 0
    if pending_rules_path.exists():
        try:
            data = json.loads(pending_rules_path.read_text(encoding="utf-8"))
            rules = data.get("pending_rules", [])
            pending_rules = sum(1 for r in rules if r.get("status") == "pending")
            approved_rules = sum(1 for r in rules if r.get("status") == "approved")
        except (json.JSONDecodeError, OSError):
            pass
    print(f"  Pending rules (awaiting operator approval): {pending_rules}")
    print(f"  Approved rules (ready for ingest):          {approved_rules}")

    # Pending archival / supersede
    pending_archival = 0
    pending_supersede = 0
    if pending_archival_path.exists():
        try:
            data = json.loads(pending_archival_path.read_text(encoding="utf-8"))
            candidates = data.get("pending_archival", [])
            for c in candidates:
                if c.get("status") != "pending":
                    continue
                if c.get("action") == "supersede":
                    pending_supersede += 1
                else:
                    pending_archival += 1
        except (json.JSONDecodeError, OSError):
            pass
    print(f"  Pending archival candidates:                {pending_archival}")
    print(f"  Pending supersede candidates (G-L4 gated): {pending_supersede}")
    print()
    print("To review proposals: vnx learning review")
    return 0


def _cmd_review(args) -> int:
    """Show pending proposals for operator review."""
    project_dir = Path(getattr(args, "project_dir", "."))
    state_dir = _resolve_state_dir(project_dir)
    mode = getattr(args, "mode", "all")

    show_rules = mode in ("all", "rules")
    show_archival = mode in ("all", "archival")

    if show_rules:
        pending_rules_path = state_dir / "pending_rules.json"
        if not pending_rules_path.exists():
            print("No pending_rules.json found. Run `vnx learning run` first.")
        else:
            try:
                data = json.loads(pending_rules_path.read_text(encoding="utf-8"))
                rules = [r for r in data.get("pending_rules", []) if r.get("status") == "pending"]
                if not rules:
                    print("No pending prevention rules.")
                else:
                    print(f"Pending prevention rules ({len(rules)}):")
                    for r in rules:
                        print(f"  [{r.get('id', '?')}] {r.get('pattern', '')[:80]}")
                        print(f"    Prevention: {r.get('prevention', '')[:80]}")
                        print(f"    Confidence: {r.get('confidence', '?')}  "
                              f"Occurrences: {r.get('occurrence_count', '?')}")
                        print()
            except (json.JSONDecodeError, OSError) as exc:
                print(f"error reading pending_rules.json: {exc}", file=sys.stderr)
                return 1

    if show_archival:
        pending_archival_path = state_dir / "pending_archival.json"
        if not pending_archival_path.exists():
            print("No pending_archival.json found.")
        else:
            try:
                data = json.loads(pending_archival_path.read_text(encoding="utf-8"))
                candidates = [c for c in data.get("pending_archival", []) if c.get("status") == "pending"]
                if not candidates:
                    print("No pending archival/supersede candidates.")
                else:
                    print(f"Pending archival/supersede candidates ({len(candidates)}):")
                    for c in candidates:
                        action = c.get("action", "archive")
                        print(f"  [{c.get('source_table', '?')}:{c.get('pattern_id', '?')}] "
                              f"action={action}  conf={c.get('confidence', '?')}")
                        print(f"    Title: {(c.get('title') or '')[:80]}")
                        print(f"    Reason: {c.get('reason', '')}")
                        print()
            except (json.JSONDecodeError, OSError) as exc:
                print(f"error reading pending_archival.json: {exc}", file=sys.stderr)
                return 1

    return 0


def _sample_patterns(db_path: Path, n: int, seed: int = 42) -> List[dict]:
    """Return up to n patterns from success_patterns, deterministically seeded.

    Falls back to an empty list when the DB does not exist or lacks the table.
    Read-only: no writes, no schema changes.
    """
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return []
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(success_patterns)").fetchall()}
    except sqlite3.Error:
        conn.close()
        return []
    if "title" not in cols:
        conn.close()
        return []
    desc_col = "COALESCE(description,'')" if "description" in cols else "''"
    tags_col = "COALESCE(tags,'')" if "tags" in cols else "''"
    try:
        rows = conn.execute(
            f"SELECT id, title, {desc_col} AS description, {tags_col} AS stored_tags"
            " FROM success_patterns"
            " ORDER BY id DESC"
            f" LIMIT ?",
            (max(n * 4, n),),  # over-fetch; we seed-shuffle below
        ).fetchall()
    except sqlite3.Error:
        conn.close()
        return []
    conn.close()

    import random as _random
    rng = _random.Random(seed)
    shuffled = list(rows)
    rng.shuffle(shuffled)
    return [{"id": r["id"], "title": r["title"] or "", "description": r["description"] or "",
             "stored_tags": r["stored_tags"] or ""} for r in shuffled[:n]]


def _tag_overlap(item_tags: set, query_scope: List[str]) -> int:
    return len(item_tags & set(query_scope))


def _run_ab_comparison(
    patterns: List[dict],
    provider_available: bool,
) -> dict:
    """Core A/B logic — pure, no I/O, injectable for tests.

    For each pattern:
    - WITHOUT arm: derive_tags(title + description)
    - WITH arm: _llm_tags_with_cost(title + description, enabled_override=True)
      (returns ([], 0.0) when provider is unavailable)

    Returns a result dict with per-pattern data and aggregate metrics.
    """
    from vnx_tag_vocabulary import derive_tags
    import vnx_tagger as _tagger

    per_pattern = []
    total_cost = 0.0
    rescued = 0  # patterns where LLM added at least 1 new tag

    for pat in patterns:
        text = f"{pat['title']} {pat['description']}".strip()
        without_tags = set(derive_tags(text))

        if provider_available:
            llm_new, cost = _tagger._llm_tags_with_cost(text, enabled_override=True)
        else:
            llm_new, cost = [], 0.0

        with_tags = without_tags | set(llm_new)
        new_tags = set(llm_new) - without_tags

        # Precision = mean tag_overlap across the representative query scopes
        overlap_without = sum(_tag_overlap(without_tags, q) for q in _AB_QUERY_SCOPES)
        overlap_with = sum(_tag_overlap(with_tags, q) for q in _AB_QUERY_SCOPES)

        if new_tags:
            rescued += 1
        total_cost += cost
        per_pattern.append({
            "id": pat["id"],
            "title": pat["title"][:60],
            "without_tags": sorted(without_tags),
            "new_llm_tags": sorted(new_tags),
            "overlap_without": overlap_without,
            "overlap_with": overlap_with,
            "cost_usd": cost,
        })

    n = len(patterns)
    rescue_rate = rescued / n if n else 0.0
    avg_overlap_without = sum(p["overlap_without"] for p in per_pattern) / n if n else 0.0
    avg_overlap_with = sum(p["overlap_with"] for p in per_pattern) / n if n else 0.0

    return {
        "n_sampled": n,
        "provider_available": provider_available,
        "rescue_rate": rescue_rate,
        "rescued": rescued,
        "avg_overlap_without": avg_overlap_without,
        "avg_overlap_with": avg_overlap_with,
        "total_cost_usd": total_cost,
        "cost_per_pattern_usd": total_cost / n if n else 0.0,
        "per_pattern": per_pattern,
    }


def _cmd_tagger_ab(args) -> int:
    """Run a read-only A/B comparison: tagger ON vs OFF on a small pattern sample.

    Measures:
    - rescue_rate: fraction of patterns where LLM added tags the deterministic
      derive_tags() missed
    - precision lift: average tag-overlap improvement across representative query
      scopes (proxy for selection probability improvement)
    - cost: tokens / USD per pattern for the LLM arm

    Does NOT modify the database or set VNX_TAGGER_ENABLED.

    Decision criterion for default-on: enable VNX_TAGGER_ENABLED once the operator
    observes rescue_rate >= 0.20 AND cost_per_pattern <= 0.001 USD from a live run.
    """
    project_dir = Path(getattr(args, "project_dir", "."))
    sample_n = int(getattr(args, "sample", 20))
    seed = int(getattr(args, "seed", 42))

    state_dir = _resolve_state_dir(project_dir)
    db_path = state_dir / "quality_intelligence.db"

    patterns = _sample_patterns(db_path, n=sample_n, seed=seed)
    if not patterns:
        print("tagger-ab: no patterns found in success_patterns table.", file=sys.stderr)
        print(f"  (DB path checked: {db_path})", file=sys.stderr)
        print("  Run `vnx learning run` first to populate the intelligence store.", file=sys.stderr)
        return 1

    # Check provider availability WITHOUT making an LLM call
    provider_available = False
    try:
        import vnx_tagger as _tagger
        from classifier_providers import get_provider
        prov = get_provider(_tagger.get_tagger_provider_name())
        provider_available = prov.is_available()
    except Exception:
        provider_available = False

    if not provider_available:
        print(
            "tagger-ab: LLM provider not available (check DEEPSEEK_API_KEY or "
            f"VNX_TAGGER_PROVIDER={_tagger.get_tagger_provider_name()!r}).",
            file=sys.stderr,
        )
        print("  Running WITHOUT arm only (deterministic derive_tags).", file=sys.stderr)

    result = _run_ab_comparison(patterns, provider_available=provider_available)

    _print_ab_report(result)
    return 0


def _print_ab_report(result: dict) -> None:
    n = result["n_sampled"]
    rescued = result["rescued"]
    rescue_rate = result["rescue_rate"]
    avg_without = result["avg_overlap_without"]
    avg_with = result["avg_overlap_with"]
    total_cost = result["total_cost_usd"]
    cost_per = result["cost_per_pattern_usd"]
    provider_ok = result["provider_available"]

    print()
    print("=" * 60)
    print("Tagger A/B — tag-overlap precision comparison")
    print("=" * 60)
    print(f"  Sample size        : {n} patterns")
    print(f"  LLM arm available  : {'yes' if provider_ok else 'no (deterministic only)'}")
    print()

    print("WITHOUT tagger (derive_tags only):")
    print(f"  Avg tag-overlap / query scope : {avg_without:.2f}")
    print()

    if provider_ok:
        lift = avg_with - avg_without
        lift_pct = (lift / avg_without * 100) if avg_without > 0 else 0.0
        print("WITH tagger (derive_tags + LLM):")
        print(f"  Avg tag-overlap / query scope : {avg_with:.2f}")
        print(f"  Overlap lift                  : +{lift:.2f} ({lift_pct:+.1f}%)")
        print(f"  Rescue rate                   : {rescued}/{n} ({rescue_rate:.0%})")
        print()
        print("Cost (LLM arm):")
        print(f"  Total                         : ${total_cost:.6f}")
        print(f"  Per pattern                   : ${cost_per:.6f}")
        print()

        # Decision criterion
        RESCUE_THRESHOLD = 0.20
        COST_CEILING = 0.001  # USD per pattern
        meets_rescue = rescue_rate >= RESCUE_THRESHOLD
        meets_cost = cost_per <= COST_CEILING
        verdict = "ENABLE" if (meets_rescue and meets_cost) else "HOLD OFF"
        print("Decision criterion (default-on threshold):")
        print(f"  rescue_rate >= 20%     : {'PASS' if meets_rescue else 'FAIL'} ({rescue_rate:.0%})")
        print(f"  cost_per_pattern <= $0.001 : {'PASS' if meets_cost else 'FAIL'} (${cost_per:.6f})")
        print()
        print(f"  => {verdict}: set VNX_TAGGER_ENABLED=1 to enable default-on tagging.")
        if verdict == "HOLD OFF":
            print("     Investigate: rescue_rate too low → LLM adds few tags the deterministic")
            print("     floor misses, OR cost too high → switch to a cheaper provider.")
    else:
        print("(LLM arm skipped — set DEEPSEEK_API_KEY and re-run to measure cost + lift)")
        print()
        print("Decision criterion (default-on threshold):")
        print("  Measure rescue_rate >= 20% AND cost_per_pattern <= $0.001 USD from a live run.")
        print("  Do NOT set VNX_TAGGER_ENABLED=1 without seeing those numbers.")

    print()

    # Per-pattern detail (only the top rescued patterns)
    rescued_patterns = [p for p in result["per_pattern"] if p["new_llm_tags"]]
    if rescued_patterns:
        print(f"Rescued patterns ({len(rescued_patterns)} of {n}):")
        for p in rescued_patterns[:5]:
            print(f"  [{p['id']}] {p['title']}")
            print(f"       det: {p['without_tags']}")
            print(f"       llm: {p['new_llm_tags']}")
        if len(rescued_patterns) > 5:
            print(f"  ... and {len(rescued_patterns) - 5} more")
    print("=" * 60)


def _cmd_grounding_shadow(args) -> int:
    """Compare V1 (substring-join) vs V2 (junction) grounding — read-only, no DB writes."""
    import sqlite3 as _sqlite3
    project_dir = Path(getattr(args, "project_dir", "."))
    limit = int(getattr(args, "limit", 50))

    state_dir = _resolve_state_dir(project_dir)
    db_path = state_dir / "quality_intelligence.db"

    if not db_path.exists():
        print(f"error: quality_intelligence.db not found at {db_path}", file=sys.stderr)
        print("Run `vnx init` to initialise the project first.", file=sys.stderr)
        return 1

    try:
        conn = _sqlite3.connect(str(db_path), timeout=10.0)
        conn.row_factory = _sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT dispatch_id, outcome_status FROM dispatch_metadata "
                "WHERE outcome_status IN ('success', 'failure') "
                "ORDER BY dispatched_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        except _sqlite3.OperationalError:
            rows = []
        finally:
            conn.close()
    except Exception as exc:
        print(f"error: could not read dispatch_metadata: {exc}", file=sys.stderr)
        return 1

    if not rows:
        print("No completed dispatches found in dispatch_metadata.")
        print("Run some dispatches first to populate outcome data.")
        return 0

    dispatches = [
        {"dispatch_id": r["dispatch_id"], "status": r["outcome_status"]}
        for r in rows
    ]

    import intelligence_persist as _ip  # type: ignore[import]
    report = _ip.shadow_grounding_compare(db_path, dispatches)

    summary = report["summary"]
    print("\nVNX Learning — Outcome Grounding Shadow (V1 vs V2)")
    print("=" * 52)
    print(f"Dispatches analysed : {summary['total_dispatches']}")
    print(f"Junction available  : {'yes' if summary['junction_available'] else 'no'}")

    if not summary["junction_available"]:
        print()
        print("No dispatch_pattern_offered junction table found.")
        print("V2 grounding requires the junction — run `vnx migrate` to create it.")
        return 0

    diverged_entries = [e for e in report["dispatches"] if e["has_divergence"]]
    if diverged_entries:
        print()
        for entry in diverged_entries:
            n_v2_only = len(entry["v2_only"])
            n_v1_only = len(entry["v1_only"])
            tag = f"[{entry['status']}]"
            print(f"  {entry['dispatch_id']} {tag}")
            if n_v2_only:
                print(f"    V2-only grounded (V1 missed): {n_v2_only} pattern(s)")
            if n_v1_only:
                print(f"    V1-only grounded (V2 skips) : {n_v1_only} pattern(s)")

    print()
    print("Divergence summary:")
    print(f"  Diverged dispatches             : {summary['diverged_dispatches']}/{summary['total_dispatches']}")
    print(f"  Patterns V2 grounds / V1 misses : {summary['v2_only_grounded']}")
    print(f"  Patterns V1 grounds / V2 skips  : {summary['v1_only_grounded']}")

    if summary["diverged_dispatches"] == 0:
        print("\nNo divergence — V1 and V2 agree on all dispatches.")
    else:
        print()
        print("To flip the default to V2 once shadow validates on real data:")
        print("  Set VNX_OUTCOME_GROUNDING_V2=1 in your environment, or")
        print("  flip the config toggle in the dashboard (requires operator approval).")

    return 0



def vnx_learning(args) -> int:
    sub = getattr(args, "learning_subcommand", None)
    dispatch = {
        "run": _cmd_run,
        "status": _cmd_status,
        "review": _cmd_review,
        "grounding-shadow": _cmd_grounding_shadow,
        "tagger-ab": _cmd_tagger_ab,
    }
    if sub in dispatch:
        return dispatch[sub](args)
    print("Usage: vnx learning {run|status|review|grounding-shadow|tagger-ab}")
    return 1
