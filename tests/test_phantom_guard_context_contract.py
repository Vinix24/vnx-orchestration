"""tests/test_phantom_guard_context_contract.py — OI-1614: a call-site guard for
phantom_guard.PhantomDecisionContext, mirroring tests/test_sessionstart_hook_beacon_health.py's
AST-scan pattern (PR #1742, OI-1594) rather than inventing a new one.

Defect this closes: ``phantom_guard.record_phantom_if_any`` accepted ``role``, ``status``,
``task_class``, ``read_only``, ``worktree_diff`` as five independent keyword arguments with
defaults. Each of the three lane call sites (scripts/lib/envelope_govern.py,
scripts/lib/dispatch_govern.py, scripts/lib/tmux_interactive_dispatch.py) picked its own subset
— NONE of them passed ``task_class`` or ``read_only`` — and Python's defaults silently supplied
``None`` for whatever a caller forgot. The guard's own exemption rule
(task_class="research_structured" or read_only=True exempts a no-diff completion) was, and is,
correct; the bug was entirely upstream — a dispatch with role="research-analyst",
task_class="research_structured" was rejected as a phantom because task_class never arrived.

The fix (scripts/lib/phantom_guard.py) bundles the five decision-relevant fields into ONE
required dataclass, ``PhantomDecisionContext``, with no defaults — a caller that forgets a
field at construction gets a ``TypeError``, not a silently-wrong verdict. That is real
protection, but it only fires when the code path actually EXECUTES (e.g. only on the rare
review-role/empty-diff combination). This test is the STATIC backstop: an AST scan over every
git-tracked call site of ``record_phantom_if_any``, checking that its ``context=`` argument is
an INLINE ``PhantomDecisionContext(...)`` construction naming all five required fields as
keywords — so a NEW, fourth lane that drops one is caught in CI before it ever runs, the same
"catch the lane that doesn't exist yet" property the beacon test's call-site guard has for
``all_beacons``/``beacon_summary``.

Three fixture-tree test classes prove the mechanism can actually fail, mirroring
TestCallSiteGuardCanActuallyFail in the beacon test: a dropped field, an entirely omitted
``context=`` keyword (the literal historic bug — an omitted keyword, not a wrong value), and a
non-inline (variable) ``context=`` value the scanner cannot verify statically all turn the
assertion red with a clear message, never a silent pass.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "scripts" / "lib"
sys.path.insert(0, str(LIB))

import phantom_guard as pg  # noqa: E402

_TARGET_CALL = "record_phantom_if_any"
_CONTEXT_CTOR = "PhantomDecisionContext"
_REQUIRED_CONTEXT_FIELDS = frozenset({"role", "task_class", "read_only", "status", "worktree_diff"})
_EXCLUDE_DIRS = frozenset({"tests"})


def _git_tracked_py_files(root: Path) -> list[Path]:
    """Absolute paths to every git-tracked ``.py`` file under ``root``.

    Same property as the beacon test's identically-named helper (OI-1597): tracked-ness, not a
    directory namelist, keeps a build artifact / nested worktree / CI rollout copy out of the
    scan regardless of what it's named or where it lives. Fails CLOSED — raises ``RuntimeError``
    — when git is unavailable, rather than silently falling back to an incomplete namelist.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", "*.py"],
            cwd=str(root), capture_output=True, timeout=10,
        )
    except OSError as exc:
        raise RuntimeError(
            f"git is not available to determine tracked .py files under {root}"
        ) from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"`git ls-files` failed under {root} (not a git work tree?): {stderr}")
    stdout = result.stdout.decode("utf-8")
    return [root / rel for rel in stdout.split("\0") if rel]


def _call_target_name(node: ast.Call) -> Optional[str]:
    """The bare callee name of an ast.Call — 'foo' for both foo(...) and mod.foo(...)."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _find_record_phantom_call_sites(
    root: Path, exclude_dirs: frozenset[str] = _EXCLUDE_DIRS,
) -> dict[str, list[ast.Call]]:
    """Map of relative-path -> list of ast.Call nodes invoking record_phantom_if_any(...),
    over the git-tracked .py files rooted at ``root``. A name that only appears in a
    docstring/comment (e.g. this file's own module docstring above) is not a call site — an
    AST scan for real ast.Call nodes can tell the two apart, a grep cannot (OI-1594).

    An unparseable or non-UTF-8 file raises a clean AssertionError naming the file, instead of
    letting the raw SyntaxError/UnicodeDecodeError traceback escape (OI-1597 advisory).
    """
    found: dict[str, list[ast.Call]] = {}
    for full_path in _git_tracked_py_files(root):
        rel = full_path.relative_to(root)
        if any(part in exclude_dirs for part in rel.parts[:-1]):
            continue
        try:
            source = full_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(full_path))
        except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
            raise AssertionError(
                f"cannot parse {rel} while scanning for record_phantom_if_any call sites: {exc}"
            ) from exc
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _call_target_name(node) == _TARGET_CALL
        ]
        if calls:
            found[str(rel)] = calls
    return found


def _context_field_names(call: ast.Call, *, file_label: str) -> frozenset[str]:
    """The keyword-argument names passed to the inline ``PhantomDecisionContext(...)``
    supplied as this record_phantom_if_any(...) call's ``context=`` argument.

    Fails LOUD (raises AssertionError), never returns an empty/partial result silently, in
    the two cases a real regression would take:
      1. ``context=`` is missing from the call entirely — the exact historic OI-1614 shape:
         an argument the signature accepts but a caller never writes, silently defaulting.
         (record_phantom_if_any's ``context`` parameter has NO default, so this would also be
         a TypeError at runtime — this is the static, pre-runtime half of that protection.)
      2. ``context=`` is present but its value is not an inline ``PhantomDecisionContext(...)``
         call the scanner can read fields from (e.g. a bare variable) — the scanner refuses to
         guess, rather than silently treating an unverifiable call site as compliant.
    """
    for kw in call.keywords:
        if kw.arg == "context":
            value = kw.value
            if isinstance(value, ast.Call) and _call_target_name(value) == _CONTEXT_CTOR:
                return frozenset(k.arg for k in value.keywords if k.arg is not None)
            raise AssertionError(
                f"{file_label}:{call.lineno}: record_phantom_if_any(context=...) is not an "
                f"inline PhantomDecisionContext(...) construction — this guard cannot "
                f"statically verify which decision-relevant fields it carries. Construct the "
                f"context inline at the call site (see the three lane call sites for the "
                f"pattern), or extend this guard to resolve the indirection."
            )
    raise AssertionError(
        f"{file_label}:{call.lineno}: record_phantom_if_any(...) is missing the required "
        f"context= keyword argument entirely — this is the exact OI-1614 defect this guard "
        f"exists to catch (a decision-relevant signal silently never arriving at the guard)."
    )


_KNOWN_LANE_CALL_SITES = frozenset({
    "scripts/lib/envelope_govern.py",
    "scripts/lib/dispatch_govern.py",
    "scripts/lib/tmux_interactive_dispatch.py",
})


class TestEveryCallSiteCarriesAllDecisionRelevantFields:
    """The wachter: every real, git-tracked call site of record_phantom_if_any must construct
    its context= inline with all five required keywords. This does not enumerate a fixed
    baseline of files (unlike the beacon test's TestCallSiteCountDoesNotGrow) — a brand-new,
    fourth lane that calls record_phantom_if_any is picked up automatically by the
    git-tracked-.py scan and held to the same bar, without this test needing to be edited."""

    def test_known_lane_call_sites_are_found(self):
        sites = _find_record_phantom_call_sites(REPO)
        missing = _KNOWN_LANE_CALL_SITES - set(sites)
        assert not missing, f"expected call site(s) not found by the scan: {missing}"

    def test_every_call_site_supplies_all_required_context_fields(self):
        sites = _find_record_phantom_call_sites(REPO)
        assert sites, "expected at least the three known lane call sites"
        problems = []
        for rel, calls in sites.items():
            for call in calls:
                fields = _context_field_names(call, file_label=rel)
                missing = _REQUIRED_CONTEXT_FIELDS - fields
                if missing:
                    problems.append(f"{rel}:{call.lineno} missing={sorted(missing)}")
        assert not problems, (
            "call site(s) dropped a decision-relevant PhantomDecisionContext field:\n"
            + "\n".join(problems)
        )


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)


def _git_add(root: Path, *relative_paths: str) -> None:
    subprocess.run(["git", "add", "--", *relative_paths], cwd=str(root), check=True)


_GOOD_CALL_SITE = (
    "from phantom_guard import PhantomDecisionContext, record_phantom_if_any\n"
    "record_phantom_if_any(\n"
    "    dispatch_id='d',\n"
    "    context=PhantomDecisionContext(\n"
    "        role=role, task_class=task_class, read_only=None,\n"
    "        status=status, worktree_diff=diff,\n"
    "    ),\n"
    ")\n"
)


class TestCallSiteGuardCanActuallyFail:
    """A wachter die niet kan falen, faalt stil. ``_find_record_phantom_call_sites`` and
    ``_context_field_names`` are exercised directly against isolated fixture trees (never the
    real repo) so this guard's MECHANISM is proven, not just today's three call sites."""

    def test_a_compliant_call_site_passes(self, tmp_path):
        _git_init(tmp_path)
        (tmp_path / "good.py").write_text(_GOOD_CALL_SITE, encoding="utf-8")
        _git_add(tmp_path, "good.py")

        sites = _find_record_phantom_call_sites(tmp_path)
        for rel, calls in sites.items():
            for call in calls:
                fields = _context_field_names(call, file_label=rel)
                assert not (_REQUIRED_CONTEXT_FIELDS - fields)

    def test_a_dropped_field_turns_the_assertion_red(self, tmp_path):
        _git_init(tmp_path)
        (tmp_path / "bad.py").write_text(
            "from phantom_guard import PhantomDecisionContext, record_phantom_if_any\n"
            "record_phantom_if_any(\n"
            "    dispatch_id='d',\n"
            "    context=PhantomDecisionContext(\n"
            # task_class dropped — the exact shape of the real OI-1614 bug.
            "        role=role, read_only=None, status=status, worktree_diff=diff,\n"
            "    ),\n"
            ")\n",
            encoding="utf-8",
        )
        _git_add(tmp_path, "bad.py")

        sites = _find_record_phantom_call_sites(tmp_path)
        problems = []
        for rel, calls in sites.items():
            for call in calls:
                fields = _context_field_names(call, file_label=rel)
                missing = _REQUIRED_CONTEXT_FIELDS - fields
                if missing:
                    problems.append((rel, missing))
        assert problems == [("bad.py", frozenset({"task_class"}))]

    def test_an_omitted_context_keyword_turns_the_assertion_red(self, tmp_path):
        """The literal historic defect: context= never written at all (old-style flat
        kwargs, or a call that simply forgot it), not a wrong value for it."""
        _git_init(tmp_path)
        (tmp_path / "bad.py").write_text(
            "from phantom_guard import record_phantom_if_any\n"
            "record_phantom_if_any(dispatch_id='d', role=role, status=status)\n",
            encoding="utf-8",
        )
        _git_add(tmp_path, "bad.py")

        sites = _find_record_phantom_call_sites(tmp_path)
        with pytest.raises(AssertionError, match="missing the required context"):
            for rel, calls in sites.items():
                for call in calls:
                    _context_field_names(call, file_label=rel)

    def test_a_non_inline_context_value_raises_a_clean_error_not_a_silent_pass(self, tmp_path):
        _git_init(tmp_path)
        (tmp_path / "bad.py").write_text(
            "from phantom_guard import record_phantom_if_any\n"
            "ctx = build_context()\n"
            "record_phantom_if_any(dispatch_id='d', context=ctx)\n",
            encoding="utf-8",
        )
        _git_add(tmp_path, "bad.py")

        sites = _find_record_phantom_call_sites(tmp_path)
        with pytest.raises(AssertionError, match="cannot statically verify"):
            for rel, calls in sites.items():
                for call in calls:
                    _context_field_names(call, file_label=rel)

    def test_a_docstring_mention_does_not_trip_it(self, tmp_path):
        _git_init(tmp_path)
        (tmp_path / "mentions_only.py").write_text(
            '"""Eventually calls record_phantom_if_any(context=...)."""\n'
            "# see record_phantom_if_any for details\n",
            encoding="utf-8",
        )
        _git_add(tmp_path, "mentions_only.py")

        sites = _find_record_phantom_call_sites(tmp_path)
        assert sites == {}

    def test_an_untracked_file_does_not_trip_it(self, tmp_path):
        """A fourth-lane call site that exists on disk but was never `git add`-ed (a
        build artifact, a nested worktree, a scratch file) is invisible to the scan —
        same property the beacon test's guard relies on (OI-1597)."""
        _git_init(tmp_path)
        (tmp_path / "existing.py").write_text(_GOOD_CALL_SITE, encoding="utf-8")
        _git_add(tmp_path, "existing.py")
        baseline_files = set(_find_record_phantom_call_sites(tmp_path))

        (tmp_path / "untracked_bad.py").write_text(
            "from phantom_guard import record_phantom_if_any\n"
            "record_phantom_if_any(dispatch_id='d')\n",
            encoding="utf-8",
        )
        # Deliberately never git add-ed.
        matched_files = set(_find_record_phantom_call_sites(tmp_path))
        # AST nodes from two separate parses are never == to each other even for identical
        # source, so this compares the set of matched FILES, not the ast.Call objects.
        assert matched_files == baseline_files

    def test_an_unparseable_file_gives_a_clean_assertion_not_a_traceback(self, tmp_path):
        _git_init(tmp_path)
        (tmp_path / "broken.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
        _git_add(tmp_path, "broken.py")

        with pytest.raises(AssertionError, match="broken.py"):
            _find_record_phantom_call_sites(tmp_path)


# ---------------------------------------------------------------------------
# Part 4: behavioral verdict test — the guard's OUTCOME, not the call-site shape.
# A test that only asserted "task_class is in kwargs" would test this PR's own
# mechanism and break on the next refactor; this asserts the actual PhantomVerdict.
# ---------------------------------------------------------------------------


class TestResearchDispatchIsNeverPhantom:
    """The concrete OI-1614 incident, replayed through record_phantom_if_any end to end
    (not the pure phantom_guard() decision alone, which was already correct and already
    covered by test_phantom_guard.py — this proves the WIRING is correct too)."""

    def test_research_analyst_role_with_research_structured_task_class_is_not_phantom(
        self, tmp_path,
    ):
        # The measured real dispatch: role='research-analyst', task_class='research_structured',
        # status='success', worktree_diff='', token_usage=28428. A non-phantom verdict never
        # touches the corrective-receipt append path, so no mocking of append_receipt is needed.
        verdict = pg.record_phantom_if_any(
            dispatch_id="20260903-oi1609-wie-schuift-de-state-op-r2",
            context=pg.PhantomDecisionContext(
                role="research-analyst",
                task_class="research_structured",
                read_only=None,
                status="success",
                worktree_diff="",
            ),
            token_usage=28428,
            receipts_file=str(tmp_path / "r.ndjson"),
        )
        assert not verdict.is_phantom, verdict.reason

    def test_research_analyst_role_alone_is_not_phantom_even_without_task_class(self):
        # Defense in depth: the role-string exemption (REVIEW_ROLES) must hold even if a
        # future caller somehow fails to thread task_class — the two signals are independent.
        verdict = pg.phantom_guard(
            status="success", worktree_diff="", token_usage=28428, role="research-analyst",
        )
        assert not verdict.is_phantom, verdict.reason

    def test_an_actual_delivery_failure_with_the_same_shape_is_still_phantom(self):
        """Guard rail: this fix must not blanket-exempt every empty-diff completion — a
        genuine backend-developer delivery failure with the same status/diff shape must
        still be rejected."""
        verdict = pg.phantom_guard(
            status="success", worktree_diff="", token_usage=28428, role="backend-developer",
        )
        assert verdict.is_phantom, verdict.reason


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
