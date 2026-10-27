#!/usr/bin/env python3
"""governed_lane.py — the one GovernedLane adapter for the harness-lane review gates.

ADR-046 §4 names three adapter kinds: ``GovernedLane`` (``glm_gate``, ``kimi_gate``,
``deepseek_gate``), ``Deterministic`` and ``PathBinary``. This module is the
``GovernedLane``: the dispatcher wiring that used to live in
``plan_gate_panel._make_default_dispatcher``, the report reader that used to live
in ``plan_gate_panel._read_report``, and the one verdict parser the two standalone
gates used to carry as separate copies (``glm_gate._extract_verdict``,
``kimi_gate._extract_verdict``).

GE-5a is a pure extraction: every caller keeps importing the same names from
``plan_gate_panel`` (which re-exports the dispatcher and the reader under their
old names) and ``glm_gate``/``kimi_gate`` keep a module-level ``_extract_verdict``
alias, so no call site changes and no behaviour changes.

``deepseek_gate`` has no parser of its own: it runs on gate_runner's harness-lane
path and its report is read by the provider-agnostic ``gate_report_recovery``
reader. It is therefore deliberately absent from ``GOVERNED_LANE_PROVIDERS`` —
measured on the tree, not assumed.

The dispatcher below still reaches back into ``plan_gate_panel`` for the transport
helpers it was built on (``_resolve_data_dir``, ``_seat_child_env``,
``_run_provider_lane_child``, ``_run_claude_headless_seat`` and the file-ref
instruction builders). They are resolved through :func:`_panel` at call time, not
at import time, so this module and ``plan_gate_panel`` do not import each other in
a cycle: the panel imports this adapter at module load, and the adapter imports
the panel only when a dispatcher is actually built or called. Resolving them off
the module object also keeps every existing monkeypatch seam (tests patch
``plan_gate_panel._run_provider_lane_child`` and friends) working unchanged.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from gate_lane_contract import canonical_review_verdict  # after the scripts/lib path guard

# A dispatcher takes (provider, model_arg, instruction, dispatch_id) and returns
# the panelist's report text. Same shape as ``plan_gate_panel.DispatcherFn`` —
# re-declared here because this module must not import the panel at import time.
DispatcherFn = Callable[[str, str, str, str], str]


def _panel():
    """The plan-gate module, imported on first use (see the module docstring)."""
    # Lazy on purpose: the panel imports this module back (see the module docstring).
    import plan_gate_panel

    return plan_gate_panel


def _last_json_verdict(text: str) -> dict:
    """Extract the worker's verdict: the LAST ```json``` block whose ``verdict`` is a
    real verdict (pass/fail/blocked).

    One rule, both providers (measured, see ``GOVERNED_LANE_PROVIDERS``): scan the
    ```json``` fences from the end and take the first body that decodes to a dict
    whose ``verdict`` is a canonical review verdict. The governed worker report
    echoes the instruction, so the FIRST ```json``` block is our own contract
    example (its ``verdict`` is the literal "pass|fail|blocked", which is not a
    valid single verdict and is skipped) — scanning backwards selects the worker's
    actual answer, never the echoed template.
    """
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text or "", re.DOTALL)
    for block in reversed(blocks):
        try:
            obj = json.loads(block)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and canonical_review_verdict(obj.get("verdict")):
            return obj
    return {}


@dataclass(frozen=True)
class ProviderRow:
    """One provider's row of the parser register."""

    parser: Callable[[str], dict]
    origin: str


# Per-provider difference, measured at GE-5a (not assumed): there is NONE in the
# executable body. ``ast.dump()`` of the two pre-GE-5a parsers is identical once
# the docstring is stripped — glm's docstring cross-references kimi's, and that
# prose (not the rule) is what the inventory's 0.68 positional ratio measured on
# raw text. Both rows therefore carry the one rule:
#   scan ```json fences from the LAST one backwards; take the first body that
#   json-decodes to a dict whose "verdict" is a canonical review verdict
#   (pass/fail/blocked), else {}.
# Kept as one row per provider anyway: a quirk discovered for one provider
# becomes a row edit here and can never silently change its neighbour's gate.
GOVERNED_LANE_PROVIDERS: Dict[str, ProviderRow] = {
    "glm": ProviderRow(parser=_last_json_verdict, origin="scripts/glm_gate.py:142 (pre-GE-5a)"),
    "kimi": ProviderRow(parser=_last_json_verdict, origin="scripts/kimi_gate.py:153 (pre-GE-5a)"),
}


def extract_verdict(text: str, provider: str) -> dict:
    """Read *text* with the register row of *provider*.

    An unknown provider RAISES instead of falling back to a neighbour's parser:
    a wrong parser that answers "no verdict" would silently book a real review
    as unreadable. Missing or malformed text reads as "no verdict" ({}), the
    answer every caller already maps to ``unavailable`` — never to a pass.
    """
    row = GOVERNED_LANE_PROVIDERS.get((provider or "").strip().lower())
    if row is None:
        raise ValueError(
            f"governed_lane: no parser registered for provider {provider!r}; "
            f"known providers: {', '.join(sorted(GOVERNED_LANE_PROVIDERS))}"
        )
    return row.parser(text)


def read_report(base: Optional[Path], dispatch_id: str, stderr: str) -> Optional[str]:
    """Locate a panelist's unified report. Authoritative source: the ``Report: <path>``
    line provider_dispatch prints to stderr; falls back to the deterministic path.

    Only a path whose filename is exactly ``{dispatch_id}.md`` is accepted — a foreign
    or stale ``Report:`` line must never feed this panelist a different dispatch's
    verdict (the gate's verdict-source integrity)."""
    expected = f"{dispatch_id}.md"
    for line in (stderr or "").splitlines():
        if line.startswith("Report: "):
            p = Path(line[len("Report: "):].strip())
            if p.name == expected and p.is_file():
                return p.read_text(encoding="utf-8")
    if base is not None:
        p = base / "unified_reports" / expected
        if p.is_file():
            return p.read_text(encoding="utf-8")
    return None


def _make_default_dispatcher(
    data_dir: Optional[str], timeout_seconds: int,
    *, role: str = "plan-reviewer", base_ref: Optional[str] = None,
    total_deadline_seconds: Optional[int] = None,
) -> DispatcherFn:
    """Real dispatcher: run a panelist through its governed lane, return the report text.

    INTERIM — PR-12 consolidation target. This dispatches WITHOUT going through the
    single-entry door (`vnx dispatch` / dispatch_bridge), which is a side door. Once the
    door is wired and flipped, this MUST route through that one door instead. The split
    below is exactly what the door decides internally:
      claude            -> headless lane, `claude -p` via ClaudeSubprocessAdapter, on
                           the SUBSCRIPTION (measured 2026-08-11; the door's default for
                           claude since A2, 2026-08-26). NOT provider_dispatch — it
                           refuses claude.
      kimi/glm/deepseek -> provider_dispatch (constraint-safe per provider).

    ``role``: OI-811 — this factory is reused by callers whose instruction is NOT a plan
    review (e.g. the deliberation panel's diverge/contrarian/verify/synthesis stages).
    Defaults to "plan-reviewer" for backward compatibility with ``run_panel``. A caller
    with a different role gets the generic (non-plan-framed) file-ref instruction on the
    claude seat, and that role is stamped on both lanes so govern() and the phantom-guard
    evaluate it correctly instead of being told it is a plan review.

    ``base_ref``: OI-1887 — threaded straight into ``provider_dispatch.py --base-ref``
    (which already threads it into ``create_dispatch_worktree``, see that module) so a
    review-gate caller reviewing a PR can isolate the provider's worktree on
    ``origin/<pr-branch>`` instead of the default ``origin/main``. None (the default)
    leaves the plan-gate and deliberation-panel callers unchanged: neither reviews a PR
    branch, so both keep isolating on main. Never applies to the claude branch below —
    that lane isolates via ``dispatch_worktree_isolation`` on its own dispatch_id and has
    no PR branch to isolate on either.

    ``total_deadline_seconds``: OI-2066 — the lane's OWN deadline, set as
    ``VNX_TOTAL_DEADLINE`` in the provider_dispatch child's environment (the drainer and
    the claude-harness adapter read it there). ``timeout_seconds`` stays the outer
    subprocess timeout; a review gate passes it with a margin on top so the lane's own
    deadline fires first and the lane writes its own report. Only the child env carries
    it — this process's ``os.environ`` is never touched. None (the default) leaves the
    plan-gate and deliberation-panel callers exactly as they were. The claude branch
    keeps running its seat with ``timeout_seconds`` as its deadline; no review gate
    routes through it.
    """
    base = _panel()._resolve_data_dir(data_dir)

    def _dispatch(provider: str, model_arg: str, instruction: str, dispatch_id: str) -> str:
        # OI-1153: pin the seat subprocess to the SAME data dir this dispatcher
        # resolved (`base`), or the lane re-resolves its own. provider_dispatch
        # honors the two-key VNX_DATA_DIR + VNX_DATA_DIR_EXPLICIT=1 contract (see
        # its _resolve_data_dir); without it the seat's report write path can land
        # outside the dir read_report reads back from (e.g. the legacy
        # ~/.vnx-data/unified_reports root), and the seat report is silently
        # never found. The claude branch pins the same two keys — see
        # _run_claude_headless_seat, which sets them on os.environ because on that
        # lane they must bind in this process as well as in the child.
        env = _panel()._seat_child_env(base, total_deadline_seconds)
        _tmp_doc_path: Optional[str] = None
        try:
            if provider in _panel()._CLAUDE_PROVIDERS:
                # BUG-2 FIX (file-ref): the instruction already has the full plan doc inlined
                # by run_panel's build_plan_review_instruction call. For the claude seat we
                # replace it with a compact file-ref instruction so the ~50k-char body never
                # has to travel as one prompt argument.
                #
                # We write the original inline instruction to a temp file so the worker can
                # read the plan + rubric + verdict contract from a stable on-disk path.
                # The expected report path is derived from data_dir (resolved above, never
                # None) so the file-ref instruction's write path and read_report's read path
                # agree.
                report_path_str = str(base / "unified_reports" / f"{dispatch_id}.md")

                # Write the full inline instruction (plan + rubric + contract) to a temp file.
                # The worker reads this file; the short file-ref instruction points to it.
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    suffix=".vnx_plan_review.md",
                    delete=False,
                    encoding="utf-8",
                    prefix=f"plan_gate_{dispatch_id}_",
                ) as fh:
                    fh.write(instruction)
                    _tmp_doc_path = fh.name

                # Short instruction: rubric + verdict contract + explicit report-path directive.
                # No 50k doc body — the worker reads it from _tmp_doc_path. A non-plan-review
                # caller (OI-811) gets the generic file-ref framing instead — never told it is
                # reviewing an IMPLEMENTATION PLAN.
                if role == "plan-reviewer":
                    claude_instruction = _panel().build_plan_review_instruction_fileref(
                        doc_path=_tmp_doc_path,
                        track_id="<see file>",
                        report_path=report_path_str,
                    )
                else:
                    claude_instruction = _panel().build_generic_fileref_instruction(
                        doc_path=_tmp_doc_path,
                        report_path=report_path_str,
                    )

                return _panel()._run_claude_headless_seat(
                    dispatch_id=dispatch_id,
                    model=model_arg,
                    instruction=claude_instruction,
                    role=role,
                    base=base,
                    timeout_seconds=timeout_seconds,
                )

            cmd = [
                sys.executable, str(_panel().PROVIDER_DISPATCH),
                "--provider", provider,
                "--terminal-id", _panel()._PANEL_TERMINAL_ID,
                "--dispatch-id", dispatch_id,
                "--model", model_arg,
                "--role", role,
                # OI-1422: every seat this dispatcher builds (plan-reviewer,
                # deliberation-panelist) is a read-only verdict/analysis worker —
                # it reads a doc and writes a report, it never touches repo files.
                # Without an explicit --task-class, provider_dispatch.py's
                # argparse default ("") falls through to VNX_TASK_CLASS, which is
                # unset here, so _build_frontmatter's own fallback stamps
                # task_class="implementation" on the receipt (provider_dispatch.py
                # _build_frontmatter). kimi_spawn.py's completion-vs-execution
                # fabrication guard (_finalize_kimi_result) keys off task_class
                # ALONE — it has no role parameter — so "implementation" leaves a
                # clean-worktree kimi seat classified as a delivery worker that
                # produced nothing, and the guard fires a false "completion
                # without execution" failure on real panel/review output.
                # "research_structured" is the fabric's own canonical task_class
                # for reviewer/architect/planner/security-engineer/data-analyst
                # work (dispatch_router.py SKILL_TO_TASK_CLASS) and is already a
                # member of phantom_guard.REVIEW_TASK_CLASSES, so this exempts the
                # seat through both guards without widening either SSOT.
                "--task-class", "research_structured",
                "--instruction", instruction,
                "--no-auto-commit",
            ]
            if base_ref:
                cmd += ["--base-ref", base_ref]
            proc = _panel()._run_provider_lane_child(
                cmd, env=env, timeout_seconds=timeout_seconds, dispatch_id=dispatch_id,
                data_dir=base)
            report = _panel()._read_report(base, dispatch_id, proc.stderr)
            if report is None:
                # provider_dispatch prints its actionable detail across both streams —
                # surfacing only stderr once masked the real cause behind an unrelated
                # last-stderr-line red herring for a full day.
                raise RuntimeError(
                    f"no report for {dispatch_id} (rc={proc.returncode}): "
                    f"stdout: {(proc.stdout or '')[-800:]} | stderr: {(proc.stderr or '')[-400:]}"
                )
            return report
        finally:
            # Always clean up the temp doc file regardless of success or failure.
            if _tmp_doc_path is not None:
                try:
                    os.unlink(_tmp_doc_path)
                except OSError:
                    pass

    return _dispatch


