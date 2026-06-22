#!/usr/bin/env python3
"""harvest_t6_reviews.py — collect t6 model reviews + synthesize, into a project's claudedocs.

t6 has each benchmark lane review a real codebase digest and write REVIEW.md into its
preserved worker cell. This tool harvests every lane's REVIEW.md for one t6 task, drops
them into <project>/claudedocs/<date>-AI-MODEL-REVIEW-PANEL/reviews/, and (optionally)
runs an opus synthesis: convergence (multi-model = fix-first), per-dimension leaders, and
a consolidated prioritized action list — the actually-usable feedback for the project.

Idempotent + re-runnable: re-run after more lanes land (kimi/GLM on provider recovery) to
refresh the panel. Synthesis uses `claude --print` (CLI, not SDK — no-anthropic-sdk intact).
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from pathlib import Path

WT_ROOT = Path.home() / ".vnx-bench-worktrees"
CELL = ".vnx-benchmark-cell"
SYNTH_MODEL = "claude-opus-4-8"


def _cells_for(task_id: str) -> list[tuple[str, str, Path]]:
    """Return (lane, rep, review_path) for every preserved cell of this task."""
    out: list[tuple[str, str, Path]] = []
    for wt in WT_ROOT.glob(f"dispatch-bench-*-{task_id}-r*"):
        m = re.match(rf"dispatch-bench-(.+?)-{re.escape(task_id)}-r(\d+)-", wt.name)
        if not m:
            continue
        review = wt / CELL / "REVIEW.md"
        if review.exists():
            out.append((m.group(1), m.group(2), review))
    # newest cell per (lane, rep)
    best: dict[tuple[str, str], tuple[float, Path]] = {}
    for lane, rep, rv in out:
        mt = rv.stat().st_mtime
        if (lane, rep) not in best or mt > best[(lane, rep)][0]:
            best[(lane, rep)] = (mt, rv)
    return sorted((lane, rep, p) for (lane, rep), (_mt, p) in best.items())


def _synthesize(project_label: str, reviews: list[tuple[str, str, str]]) -> str:
    """reviews = [(lane, rep, text)]. Returns SYNTHESIS.md body (opus) or '' on failure."""
    parts = [f"=== REVIEW by {lane} (rep {rep}) ===\n{text[:24000]}" for lane, rep, text in reviews]
    corpus = "\n\n".join(parts)
    prompt = (
        f"You are consolidating {len(reviews)} INDEPENDENT AI model code reviews of a real "
        f"codebase: {project_label}. Each review covers SECURITY, CODE QUALITY, and "
        f"PRODUCT/FUTURE FEATURES with file-grounded findings.\n\n"
        f"Produce a synthesis in markdown with these sections:\n"
        f"1. ## Fix first (convergence) — issues flagged by MULTIPLE models, highest confidence. "
        f"For each: the issue, the files, how many/which models raised it.\n"
        f"2. ## Per-dimension read — for SECURITY, CODE QUALITY, PRODUCT/FUTURE: which model gave "
        f"the deepest/most-useful findings, and the single best insight in that dimension.\n"
        f"3. ## Consolidated action list — a deduped, PRIORITIZED list (P0/P1/P2) of the concrete "
        f"things to fix or build, each with the file(s) and a one-line why.\n"
        f"4. ## Unique insights — sharp findings only one model surfaced that are worth keeping.\n"
        f"5. ## Model strengths/weaknesses — one line per model on what it was good/weak at here.\n\n"
        f"Be concrete, cite real files, no filler. Output ONLY the synthesis markdown — "
        f"no preamble, and do NOT reference any external filename or where this is saved.\n\n"
        f"Here are the reviews:\n\n{corpus}"
    )
    try:
        proc = subprocess.run(
            ["claude", "--print", "--model", SYNTH_MODEL, prompt],
            capture_output=True, text=True, timeout=600, check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return f"<!-- synthesis failed: {exc} -->\n"
    return f"<!-- synthesis failed: rc={proc.returncode} {proc.stderr[:300]} -->\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--project-label", required=True, help="human label for the synthesis prompt")
    ap.add_argument("--target", required=True, type=Path, help="project claudedocs dir")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD (from environment)")
    ap.add_argument("--no-synthesis", action="store_true")
    a = ap.parse_args()

    cells = _cells_for(a.task_id)
    if not cells:
        print(f"[harvest] no reviews found for {a.task_id} under {WT_ROOT}")
        return 1

    panel = a.target.expanduser() / f"{a.date}-AI-MODEL-REVIEW-PANEL"
    rev_dir = panel / "reviews"
    rev_dir.mkdir(parents=True, exist_ok=True)

    reviews: list[tuple[str, str, str]] = []
    for lane, rep, rv in cells:
        text = rv.read_text(encoding="utf-8", errors="ignore")
        (rev_dir / f"{lane}-r{rep}.md").write_text(text, encoding="utf-8")
        reviews.append((lane, rep, text))
    lanes = sorted({lane for lane, _r, _t in reviews})
    print(f"[harvest] {a.task_id}: {len(reviews)} reviews from {len(lanes)} lanes -> {rev_dir}")

    index = (
        f"# AI model review panel — {a.project_label}\n\n"
        f"Date: {a.date}. {len(reviews)} independent reviews from {len(lanes)} models, each over the "
        f"same curated codebase digest (security / code quality / product-future-features).\n\n"
        f"Models: {', '.join(lanes)}\n\n"
        f"- `SYNTHESIS.md` — consolidated, prioritized read (start here).\n"
        f"- `reviews/<model>-r<n>.md` — each model's raw review.\n"
    )
    (panel / "README.md").write_text(index, encoding="utf-8")

    if not a.no_synthesis:
        print(f"[harvest] synthesizing {len(reviews)} reviews via {SYNTH_MODEL} …")
        body = _synthesize(a.project_label, reviews)
        header = f"# {a.project_label} — synthesis of {len(reviews)} AI model reviews ({a.date})\n\n"
        (panel / "SYNTHESIS.md").write_text(header + body, encoding="utf-8")
        print(f"[harvest] wrote {panel / 'SYNTHESIS.md'}")
    print(f"[harvest] panel ready: {panel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
