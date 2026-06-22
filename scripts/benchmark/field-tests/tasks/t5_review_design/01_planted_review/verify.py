"""verify.py for t5-01 — planted-issue codebase review (review-character fingerprint).

The worker reviews a small SEO-audit SaaS package (seoaudit/) that contains 15
PLANTED defects: 3 each across security / performance / correctness / operational /
maintainability. The worker writes structured findings to REVIEW.md.

Scoring is DETERMINISTIC (no LLM judge): an issue counts as DETECTED when the
review names the right file AND uses a signature keyword for that issue, either in
a structured finding scoped to the file or co-located in the prose. Detection is
keyword-driven (not line-number-driven) so models are not penalised for citing a
function name instead of a line.

  correctness = detected / 15 * 5   (partial credit)

`details` also reports the per-category detection fingerprint (this is the actual
research output: which defect classes each model is blind to), the categorisation
accuracy (did the model label the defect's class correctly), and the noise count
(findings that map to no planted issue). Those extras do not change correctness —
they characterise the model's review behaviour for the published matrix.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

SEED_REL = "scripts/benchmark/field-tests/tasks/t5_review_design/01_planted_review/seed"
REQUIRED = ["REVIEW.md"]

# Each planted issue: id, category, file basename(s) it lives in, and a set of
# signature keywords (any one, case-insensitive substring). Keywords are specific
# enough that co-occurrence with the file name is strong evidence of detection.
ISSUES: list[dict[str, Any]] = [
    {"id": "S1", "category": "security", "files": ["db.py"],
     "keywords": ["sql injection", "sqli", "f-string", "interpolat", "parameteriz",
                  "string formatting", "unsanitiz", "injection"]},
    {"id": "S2", "category": "security", "files": ["serp_client.py"],
     "keywords": ["hardcoded", "hard-coded", "hard coded", "api key", "api_key",
                  "secret", "credential", "key in source", "exposed key", "plaintext"]},
    {"id": "S3", "category": "security", "files": ["serp_client.py"],
     "keywords": ["ssrf", "server-side request", "validate", "whitelist", "allowlist",
                  "internal ip", "scheme", "arbitrary url", "untrusted url",
                  "url validation", "localhost", "metadata endpoint"]},
    {"id": "P4", "category": "performance", "files": ["report_builder.py"],
     "keywords": ["n+1", "n + 1", "query in a loop", "queries in a loop",
                  "per-domain query", "per domain query", "loop query", "batch",
                  "repeated quer", "query per"]},
    {"id": "P5", "category": "performance", "files": ["db.py"],
     "keywords": ["unbounded", "never evict", "no eviction", "memory leak",
                  "grows without", "no expiry", "never cleared", "no limit",
                  "grow unbound", "cache grow", "leak"]},
    {"id": "P6", "category": "performance", "files": ["report_builder.py"],
     "keywords": ["re.compile", "recompil", "re-compil", "precompil", "compiled inside",
                  "compile the regex", "compile in", "compiling the pattern",
                  "regex.*loop", "pattern.*loop"]},
    {"id": "C7", "category": "correctness", "files": ["report_builder.py"],
     "keywords": ["pagination", "remainder", "last page", "floor division",
                  "integer division", "partial page", "drops the last", "ceil",
                  "off-by-one", "off by one", "// page_size", "missing page"]},
    {"id": "C8", "category": "correctness", "files": ["crawl_queue.py"],
     "keywords": ["mutable default", "default argument", "default arg",
                  "shared default", "mutable arg", "collected=[]", "shared list",
                  "list as a default", "persists across calls"]},
    {"id": "C9", "category": "correctness", "files": ["serp_client.py"],
     "keywords": ["off-by-one", "off by one", "backoff", "2 * attempt", "2*attempt",
                  "first retry", "sleep(0)", "starts at 0", "starts at zero",
                  "attempt 0", "no wait", "exponential"]},
    {"id": "O10", "category": "operational", "files": ["crawl_queue.py"],
     "keywords": ["bare except", "except: pass", "except exception", "swallow",
                  "silently", "blanket except", "catch-all", "no logging",
                  "hides errors", "suppress"]},
    {"id": "O11", "category": "operational", "files": ["serp_client.py"],
     "keywords": ["timeout", "no timeout", "hang", "blocking indefinitely",
                  "never returns", "stall"]},
    {"id": "O12", "category": "operational", "files": ["db.py"],
     "keywords": ["idempot", "upsert", "on conflict", "duplicate", "dedup",
                  "unique constraint", "no unique", "inserts a new row every",
                  "re-run", "rerun"]},
    {"id": "M13", "category": "maintainability", "files": ["crawl_queue.py"],
     "keywords": ["magic number", "magic-number", "named constant", "no constant",
                  "hardcoded 73", "hardcoded threshold", "86400", " 73 ",
                  "seconds in a day", "unexplained"]},
    {"id": "M14", "category": "maintainability", "files": ["crawl_queue.py", "report_builder.py"],
     "keywords": ["duplicat", "dry", "copy-paste", "copy paste", "copied",
                  "repeated", "identical", "normalize_domain", "two copies",
                  "same logic"]},
    {"id": "M15", "category": "maintainability", "files": ["report_builder.py"],
     "keywords": ["dead code", "unreachable", "after return", "after the return",
                  "never executes", "never runs", "todo", "commented out",
                  "early return"]},
]

EXPECTED = len(ISSUES)
_CATEGORIES = ["security", "performance", "correctness", "operational", "maintainability"]
_WINDOW = 400  # chars of co-occurrence between a file mention and a keyword


def _locate_review(cell: Path) -> Path | None:
    """Find the worker's review file (top-level or one level down, any case)."""
    for cand in ("REVIEW.md", "review.md", "Review.md"):
        p = cell / cand
        if p.exists():
            return p
    hits = list(cell.glob("*.md")) + list(cell.glob("*/REVIEW.md")) + list(cell.glob("*/review.md"))
    # Ignore anything that lives inside the seed package itself.
    hits = [h for h in hits if "seoaudit" not in h.parts]
    return hits[0] if hits else None


def _parse_findings(text: str) -> list[dict[str, Any]]:
    """Split a review into finding blocks. Tolerant of header style.

    Returns dicts with: text_lc (block body lowercased), files (basenames mentioned),
    category (declared category or "").
    """
    # Split on finding headers like "### FINDING", "## Finding 3", "**Finding**", "1. Finding".
    parts = re.split(r"(?im)^\s*(?:#{1,6}\s*|\*\*\s*|\d+[.)]\s*)?finding\b.*$", text)
    # When headers exist, parts[0] is the preamble before the first finding — drop it so
    # it does not count as a (noise) finding. With no headers, keep the whole prose block.
    if len(parts) > 1:
        parts = parts[1:]
    blocks = [b for b in parts if b.strip()]
    findings: list[dict[str, Any]] = []
    for b in blocks:
        lc = b.lower()
        files = re.findall(r"[A-Za-z0-9_]+\.py", b)
        cat = ""
        m = re.search(r"category\s*[:=]\s*([a-z]+)", lc)
        if m:
            cat = m.group(1)
        else:
            for c in _CATEGORIES:
                if c in lc:
                    cat = c
                    break
        findings.append({"text_lc": lc, "files": [f.lower() for f in files], "category": cat})
    return findings


def _kw_hit(keywords: list[str], blob: str) -> bool:
    for k in keywords:
        if k.endswith(".loop") or ".*" in k:  # treat as regex
            if re.search(k, blob):
                return True
        elif k in blob:
            return True
    return False


def _detect(issue: dict[str, Any], findings: list[dict[str, Any]], full_lc: str):
    """Return (detected: bool, declared_category: str|None)."""
    files = [f.lower() for f in issue["files"]]
    kws = issue["keywords"]
    # Structured: a finding scoped to the right file that uses a signature keyword.
    for f in findings:
        scoped = any(bn in f["files"] for bn in files) or any(bn in f["text_lc"] for bn in files)
        if scoped and _kw_hit(kws, f["text_lc"]):
            return True, (f["category"] or None)
    # Prose fallback: file name and a keyword co-occur within a window anywhere.
    for bn in files:
        for m in re.finditer(re.escape(bn), full_lc):
            window = full_lc[max(0, m.start() - _WINDOW): m.end() + _WINDOW]
            if _kw_hit(kws, window):
                return True, None
    return False, None


def verify(workdir: Path, task_meta: dict) -> dict[str, Any]:
    cell = Path(workdir) / SEED_REL
    review = _locate_review(cell)
    if review is None:
        return {"pass": False, "evidence": "REVIEW.md not written",
                "details": {"files_written": [], "pass_count": 0, "expected": EXPECTED}}

    text = review.read_text(encoding="utf-8", errors="ignore")
    full_lc = text.lower()
    findings = _parse_findings(text)

    detected: list[str] = []
    per_cat = {c: 0 for c in _CATEGORIES}
    cat_correct = 0
    for issue in ISSUES:
        hit, declared = _detect(issue, findings, full_lc)
        if hit:
            detected.append(issue["id"])
            per_cat[issue["category"]] += 1
            if declared == issue["category"]:
                cat_correct += 1

    # Noise: findings that map to no planted issue (kept for the fingerprint only).
    matched_blocks = 0
    for f in findings:
        for issue in ISSUES:
            files = [x.lower() for x in issue["files"]]
            scoped = any(bn in f["files"] for bn in files) or any(bn in f["text_lc"] for bn in files)
            if scoped and _kw_hit(issue["keywords"], f["text_lc"]):
                matched_blocks += 1
                break
    noise = max(0, len(findings) - matched_blocks)

    pass_count = len(detected)
    missed = [i["id"] for i in ISSUES if i["id"] not in detected]
    fingerprint = " ".join(f"{c[:3]}={per_cat[c]}/3" for c in _CATEGORIES)
    evidence = (
        f"detected {pass_count}/{EXPECTED} [{fingerprint}]; "
        f"cat-accuracy {cat_correct}/{pass_count if pass_count else 0}; "
        f"findings={len(findings)} noise~{noise}"
        + (f"; MISSED: {', '.join(missed)}" if missed else " (all planted issues found)")
    )
    return {
        "pass": pass_count == EXPECTED,
        "evidence": evidence[:500],
        "details": {
            "files_written": ["REVIEW.md"],
            "pass_count": pass_count,
            "expected": EXPECTED,
            "detected": detected,
            "missed": missed,
            "per_category": per_cat,
            "categorization_correct": cat_correct,
            "n_findings": len(findings),
            "noise": noise,
        },
    }
