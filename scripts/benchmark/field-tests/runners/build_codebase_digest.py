#!/usr/bin/env python3
"""build_codebase_digest.py — pack a real codebase into ONE fair review digest.

Used by the t6 "real codebase review" tier. A large repo (100k-400k LOC) cannot
fit a single context, so we build a curated, capped, DETERMINISTIC digest of the
architecturally important source + docs and hand the SAME digest to every model's
cell. Equal context = a fair "who reviews better" comparison (subagent-driven
full-repo exploration would measure harness capability, not the model).

The digest itself is NEVER committed (it is proprietary); it is written to an
external, gitignored location and injected into each worker cell at dispatch via
the materialize external-seed overlay. This generator is generic and publishable.

Selection:
  * docs   — every --doc-glob match (README, FEATURE_PLAN, audits, ADRs, …), in full
  * source — files under each --src-dir with a code extension, EXCLUDING tests,
             vendored deps, build output, minified and asset files. Ranked so the
             architectural core (core/engine/api/security/…) lands before the rest.
Docs first, then ranked source, concatenated until --cap-chars. What is dropped or
truncated is logged in the manifest header for full transparency.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

CODE_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".rb"}
EXCLUDE_SUBSTR = (
    "node_modules/", "dist/", "build/", ".min.", "/tests/", "/test/",
    "test_", "_test.", ".spec.", ".test.", "/__pycache__/", "vendor/",
    ".map", "package-lock", "yarn.lock", "poetry.lock",
    "/archive/", "/deprecated/", "/.archive/",   # stale plans/docs — not the live codebase
)
# A code review needs CODE, not just docs. Cap the docs section so source dominates.
DOC_BUDGET_FRACTION = 0.30
# Dir-name importance for ranking the source section (higher = earlier).
PRIORITY = {
    "core": 9, "engine": 9, "security": 9, "api": 8, "agency": 8, "agent": 8,
    "agents": 8, "services": 7, "service": 7, "pipeline": 7, "crawler": 7,
    "extractor": 7, "extractors": 7, "connectors": 6, "connector": 6,
    "storage": 6, "infrastructure": 5, "shared": 5, "mcp": 5, "lib": 4,
}


def _git_files(repo: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout
    return [l for l in out.splitlines() if l.strip()]


def _excluded(path: str) -> bool:
    p = "/" + path
    return any(s in p for s in EXCLUDE_SUBSTR)


def _rank(path: str) -> tuple:
    parts = path.split("/")
    score = max((PRIORITY.get(seg.lower(), 0) for seg in parts), default=0)
    return (-score, len(parts), path)  # higher priority, then shallower, then name


def _glob_match(files: list[str], globs: list[str]) -> list[str]:
    import fnmatch
    hits: list[str] = []
    for g in globs:
        for f in files:
            if fnmatch.fnmatch(f, g) and f not in hits:
                hits.append(f)
    return hits


def build(repo: Path, out_dir: Path, src_dirs: list[str], doc_globs: list[str],
          cap_chars: int) -> None:
    files = _git_files(repo)
    docs = [d for d in _glob_match(files, doc_globs) if not _excluded(d)]
    src = sorted(
        (f for f in files
         if any(f == d or f.startswith(d.rstrip("/") + "/") for d in src_dirs)
         and Path(f).suffix in CODE_EXT and not _excluded(f)),
        key=_rank,
    )

    chunks: list[str] = []
    used = 0
    included: list[str] = []
    dropped: list[str] = []
    truncated: list[str] = []

    def add(relpath: str, body: str, ceiling: int) -> bool:
        nonlocal used
        header = f"\n\n===== FILE: {relpath} =====\n"
        budget = ceiling - used - len(header)
        if budget <= 200:
            dropped.append(relpath)
            return False
        if len(body) > budget:
            body = body[:budget] + f"\n... [TRUNCATED {len(body) - budget} chars]\n"
            truncated.append(relpath)
        chunks.append(header + body)
        used += len(header) + len(body)
        included.append(relpath)
        return True

    # Phase 1: docs, capped so they cannot starve the source section.
    doc_ceiling = int(cap_chars * DOC_BUDGET_FRACTION)
    for rel in docs:
        try:
            body = (repo / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        add(rel, body, doc_ceiling)
    # Phase 2: ranked source fills the remaining budget up to the full cap.
    for rel in src:
        try:
            body = (repo / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        add(rel, body, cap_chars)

    manifest = (
        f"# CODEBASE DIGEST — {repo.name}\n\n"
        f"Curated, capped review digest. NOT the full repo.\n"
        f"- files included: {len(included)} (docs: {len([d for d in docs if d in included])}, "
        f"source: {len([s for s in src if s in included])})\n"
        f"- approx size: {used} chars (~{used // 4} tokens)\n"
        f"- truncated files: {len(truncated)}; dropped (over cap): {len(dropped)}\n"
        f"- selection: docs (full) + architectural source ranked by subsystem; "
        f"tests/deps/build/assets excluded.\n"
    )
    if dropped:
        manifest += f"- DROPPED (cap reached): {', '.join(dropped[:30])}{' …' if len(dropped) > 30 else ''}\n"

    out_dir.mkdir(parents=True, exist_ok=True)
    digest_path = out_dir / "CODEBASE_DIGEST.md"
    digest_path.write_text(manifest + "\n" + "".join(chunks), encoding="utf-8")

    print(f"[digest] {repo.name}: {len(included)} files, {used} chars (~{used // 4} tok) -> {digest_path}")
    print(f"[digest] truncated={len(truncated)} dropped={len(dropped)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Build a capped codebase review digest")
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--src-dir", action="append", default=[], help="source root(s) to include")
    ap.add_argument("--doc-glob", action="append", default=[], help="doc glob(s) to include in full")
    ap.add_argument("--cap-chars", type=int, default=320_000, help="~80k tokens (fits all lanes)")
    a = ap.parse_args()
    build(a.repo.expanduser().resolve(), a.out.expanduser().resolve(),
          a.src_dir, a.doc_glob, a.cap_chars)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
