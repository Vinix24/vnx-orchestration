"""verify.py for t5-02 — agent-team backend engine design (design-character profile).

The worker writes DESIGN.md. Scoring is DETERMINISTIC (no LLM judge): a 16-point
COVERAGE checklist over the seven required dimensions (agent decomposition, tools-
per-agent, the three workflows, DataForSEO usage with rate-limit/cost/caching,
backend integration, operations, governance). A dimension counts when the design
substantively addresses it (keyword/structure presence with synonyms).

  correctness = covered / 16 * 5   (partial credit)

Coverage measures BREADTH (did the design address the required ground), which is a
fair, transparent, reproducible proxy. DEPTH is preserved separately: every model's
DESIGN.md is kept as an artifact for side-by-side reading and an optional post-hoc
rubric pass. `details.coverage` lists exactly which checks hit/missed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

SEED_REL = "scripts/benchmark/field-tests/tasks/t5_review_design/02_agent_engine_design/seed"
REQUIRED = ["DESIGN.md"]

_ENDPOINT_FAMILIES = [
    r"serp\b", r"keywords?\s+data", r"\blabs\b", r"backlink", r"on[\s-]?page",
    r"domain\s+analytics", r"content\s+analysis",
]


def _any(blob: str, *needles: str) -> bool:
    for n in needles:
        if n.startswith("re:"):
            if re.search(n[3:], blob):
                return True
        elif n in blob:
            return True
    return False


def _count_agent_roles(blob: str) -> int:
    roles = set()
    for m in re.finditer(r"([a-z][a-z0-9_-]{2,})[\s-]+agent\b", blob):
        roles.add(m.group(1))
    for m in re.finditer(r"\bagent\b[\s:]+([a-z][a-z0-9_-]{2,})", blob):
        roles.add(m.group(1))
    # Generic role nouns that commonly name agents in such a design.
    for noun in ("researcher", "crawler", "scorer", "reporter", "orchestrator",
                 "planner", "monitor", "auditor", "fetcher", "scheduler"):
        if noun in blob:
            roles.add(noun)
    roles.discard("the")
    roles.discard("an")
    roles.discard("each")
    return len(roles)


def _count_endpoint_families(blob: str) -> int:
    return sum(1 for pat in _ENDPOINT_FAMILIES if re.search(pat, blob))


def _checks(blob: str) -> list[tuple[str, bool]]:
    c: list[tuple[str, bool]] = []
    c.append(("agent_decomposition_>=3_roles", _count_agent_roles(blob) >= 3))
    c.append(("decomposition_rationale", _any(blob, "responsibilit", "separation of concerns",
              "rationale", "decompos", "why this split", "single purpose")))
    c.append(("tools_per_agent", len(re.findall(r"tools?\s*[:=]", blob)) >= 2
              or _any(blob, "tools per agent", "each agent", "the agent uses", "equipped with")))
    c.append(("research_workflow", _any(blob, "research", "onboarding", "baseline", "discovery")))
    c.append(("daily_checks_workflow", _any(blob, "daily")))
    c.append(("reporting_workflow", _any(blob, "report", "digest", "dashboard", "health score")))
    c.append(("endpoint_families_>=2", _count_endpoint_families(blob) >= 2))
    c.append(("rate_limit_strategy", _any(blob, "rate limit", "rate-limit", "throttl",
              "requests/second", "requests per second", "backoff", "429", "40402")))
    c.append(("cost_control", _any(blob, "cost", "budget", "spend", "billing", "per call",
              "per task", "per result", "$")))
    c.append(("caching_strategy", "cache" in blob and _any(blob, "ttl", "cache key", "per day",
              "by day", "expire", "invalidat", "(endpoint", "do not pull twice", "not re-pull")))
    c.append(("backend_integration", _any(blob, "run_pipeline", "pipeline") and
              _any(blob, "storage", "upsert", "extractor", "write_report", "save_finding")))
    c.append(("scheduling", _any(blob, "schedule", "cron", "queue", "nightly", "trigger",
              "scheduler", "celery", "airflow", "job runner", "worker")))
    c.append(("idempotency", _any(blob, "idempot", "dedup", "upsert", "exactly once",
              "exactly-once", "tenant_id, domain, day", "re-run", "rerun", "duplicate")))
    c.append(("failure_retry_timeout", _any(blob, "retry", "timeout", "failure mode",
              "fallback", "dead letter", "dead-letter", "circuit breaker", "never return")))
    c.append(("governance_audit", _any(blob, "audit", "provenance", "traceab", "receipt",
              "decision log", "log every")))
    c.append(("human_in_the_loop", _any(blob, "human-in-the-loop", "human in the loop", "hitl",
              "human review", "approval", "manual gate", "human gate", "sign-off", "sign off")))
    return c


def _locate_design(cell: Path) -> Path | None:
    for cand in ("DESIGN.md", "design.md", "Design.md"):
        p = cell / cand
        if p.exists():
            return p
    hits = list(cell.glob("DESIGN.md")) + list(cell.glob("*/DESIGN.md")) + list(cell.glob("*.md"))
    hits = [h for h in hits if h.name not in ("README.md", "dataforseo_notes.md")]
    return hits[0] if hits else None


def verify(workdir: Path, task_meta: dict) -> dict[str, Any]:
    cell = Path(workdir) / SEED_REL
    design = _locate_design(cell)
    expected = 16
    if design is None:
        return {"pass": False, "evidence": "DESIGN.md not written",
                "details": {"files_written": [], "pass_count": 0, "expected": expected}}

    blob = design.read_text(encoding="utf-8", errors="ignore").lower()
    checks = _checks(blob)
    hit = [n for n, ok in checks if ok]
    missed = [n for n, ok in checks if not ok]
    pass_count = len(hit)
    n_roles = _count_agent_roles(blob)
    n_fam = _count_endpoint_families(blob)
    words = len(blob.split())
    evidence = (
        f"coverage {pass_count}/{expected}; roles~{n_roles}; endpoint-families~{n_fam}; "
        f"~{words}w"
        + (f"; MISSED: {', '.join(missed)}" if missed else " (all dimensions covered)")
    )
    return {
        "pass": pass_count == expected,
        "evidence": evidence[:500],
        "details": {
            "files_written": ["DESIGN.md"],
            "pass_count": pass_count,
            "expected": expected,
            "coverage": {n: ok for n, ok in checks},
            "missed": missed,
            "agent_roles": n_roles,
            "endpoint_families": n_fam,
            "word_count": words,
        },
    }
