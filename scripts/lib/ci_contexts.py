#!/usr/bin/env python3
"""ci_contexts.py — required-vs-actual CI context classification.

``gh pr checks`` lists the contexts that EXIST on a commit. Branch protection
enforces the contexts that are REQUIRED. Those are two different sets, and the
gap between them is invisible in every green-looking checks listing.

PR #1691 is the measured case: nine ``pass`` lines, zero ``fail`` lines, and
the merge was still refused — five of the fourteen required contexts had never
been created at all during a GitHub Actions outage. A reader who only counts
what is present sees "9 pass / 0 fail" and reads it as ready.

The naive fix — "a required context with no check run blocks the merge" — is
worse than the bug. PR #1701 measured the false alarm: twelve green, ``Profile
B`` and ``Profile C`` absent, which reads as exactly the #1691 shape. It was
not. Both declare ``needs: profile-a`` (vnx-ci.yml), so neither EXISTS until
Profile A finishes. Absent meant "not created yet". A guard that cannot tell
those apart blocks every PR in the first minutes of its own CI.

So this module distinguishes the states that actually differ:

  ``passed``            the context exists and concluded in a passing state
  ``failed``            the context exists and concluded in a failing state
  ``running``           the context exists and has not concluded yet
  ``waiting_upstream``  the context does not exist, but its producing workflow
                        run is still alive, so it can still appear — WAIT,
                        never a merge blocker's fault
  ``never_created``     the context does not exist and nothing on this commit
                        can still create it: the producing run is finished, or
                        a job the producing job depends on already concluded
                        in a non-passing state. This is the #1691 trap.
  ``no_run``            the producing workflow never started on this commit at
                        all — the run has to be started before anything else
                        can be said
  ``unverified``        this module could not establish which of the above
                        applies (no workflow file produces the context, the
                        graph failed to parse, a gh call failed)

``unverified`` is never collapsed into a pass and never into a fail. A guard
that cannot see must say so — the same fail-loud contract
``pre_merge_gate.check_ci_workflow`` already holds for SKIPPED_UNVERIFIED.

The dependency graph comes from the workflow YAML in the checkout, not from
GitHub: the ``needs:`` edges are what separate ``waiting_upstream`` from
``never_created``, and there is no API that reports "this job has not been
created yet, and here is why". A PR that itself edits ``.github/workflows``
therefore gets classified against the graph in ``workflows_dir``; pass the
PR's own checkout when that distinction matters.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Set

import yaml

# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------

STATE_PASSED = "passed"
STATE_FAILED = "failed"
STATE_RUNNING = "running"
STATE_WAITING_UPSTREAM = "waiting_upstream"
STATE_NEVER_CREATED = "never_created"
STATE_NO_RUN = "no_run"
STATE_UNVERIFIED = "unverified"

#: An allowlist, not a blocklist of failures: a state added later blocks until
#: someone decides otherwise, never silently mergeable.
NON_BLOCKING_STATES = frozenset({STATE_PASSED})

#: "This commit can still produce the context if you wait." Distinct from
#: blocking-ness: a waiting context blocks now, but the fix is time, not a re-run.
TRANSIENT_STATES = frozenset({STATE_RUNNING, STATE_WAITING_UPSTREAM})

#: Check-run conclusions that satisfy a required status check. ``skipped`` and
#: ``neutral`` pass for branch protection — a path-filtered job that legitimately
#: did not apply must not read as a failure.
PASSING_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})

#: A workflow run or check run in any status other than this is still alive.
COMPLETED_STATUS = "completed"


#: GitHub's sentinel in ``checks[].app_id`` for "any app may set this status"
#: (the branch-protection API's explicit allow-any value). It is an int, so a
#: naive ``isinstance(app_id, int)`` keeps it as if it named a real producer —
#: and then NO run matches it, turning every any-app required check into a
#: false ``unverified``. It has to be normalised to "no binding" at the edge.
ANY_APP_ID = -1


class RequiredCheck(NamedTuple):
    """One required status check, with the app binding branch protection gave it.

    GitHub's newer ``checks[]`` shape carries ``app_id``; the legacy
    ``contexts[]`` list does not. When an ``app_id`` names a real app the
    requirement is "a check with THIS name FROM THIS app" — matching on the
    name alone would let a same-named check from any other app satisfy it.

    ``app_id`` is None for a legacy entry AND for GitHub's explicit
    :data:`ANY_APP_ID` sentinel: both mean the name is genuinely all branch
    protection asks for.
    """

    context: str
    app_id: Optional[int] = None


@dataclass(frozen=True)
class JobNode:
    """One workflow job, keyed by the check-run context name it produces."""

    context: str
    job_key: str
    workflow_name: str
    workflow_file: str
    needs: Sequence[str] = field(default_factory=tuple)


@dataclass(frozen=True)
class WorkflowGraph:
    """Context name -> producing job, parsed from the checkout's workflows."""

    by_context: Mapping[str, JobNode]
    #: (workflow_file, job_key) -> JobNode, for resolving ``needs`` edges.
    by_job: Mapping[tuple, JobNode]
    #: Files that could not be parsed, with the reason. Never swallowed: a
    #: context whose workflow failed to parse resolves to ``unverified``,
    #: and this list says why.
    parse_errors: Sequence[str] = field(default_factory=tuple)


@dataclass(frozen=True)
class ContextState:
    """The classification of one required branch-protection context."""

    context: str
    state: str
    detail: str
    conclusion: Optional[str] = None
    workflow_name: Optional[str] = None
    job_key: Optional[str] = None

    @property
    def blocking(self) -> bool:
        return self.state not in NON_BLOCKING_STATES

    @property
    def transient(self) -> bool:
        return self.state in TRANSIENT_STATES

    def to_dict(self) -> Dict[str, Any]:
        return {
            "context": self.context,
            "state": self.state,
            "detail": self.detail,
            "conclusion": self.conclusion,
            "workflow_name": self.workflow_name,
            "job_key": self.job_key,
            "blocking": self.blocking,
            "transient": self.transient,
        }


# ---------------------------------------------------------------------------
# Workflow graph
# ---------------------------------------------------------------------------


def _normalise_needs(raw: Any) -> Sequence[str]:
    """``needs:`` is a string, a list, or absent. Return it as a tuple."""
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, list):
        return tuple(str(item) for item in raw if isinstance(item, (str, int)))
    return ()


def parse_workflow_graph(workflows_dir: Path) -> WorkflowGraph:
    """Parse ``.github/workflows/*.y[a]ml`` into a context -> job mapping.

    A job's context name is its ``name:`` when it declares one, and its job key
    otherwise — the rule GitHub itself applies. Matrix jobs are NOT resolved:
    their real context names carry the matrix values, so they never match a
    plain required-context name and correctly land in ``unverified`` rather
    than on a confidently wrong job.

    A file that fails to parse is recorded in ``parse_errors`` and contributes
    no jobs — never skipped silently, so a context a broken workflow would
    have produced lands in ``unverified``, not in "nothing requires it".
    """
    by_context: Dict[str, JobNode] = {}
    by_job: Dict[tuple, JobNode] = {}
    errors: List[str] = []

    if not workflows_dir.is_dir():
        return WorkflowGraph(
            by_context={},
            by_job={},
            parse_errors=(f"workflows directory not found: {workflows_dir}",),
        )

    paths = sorted(
        [p for p in workflows_dir.iterdir() if p.suffix in (".yml", ".yaml") and p.is_file()]
    )
    for path in paths:
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
            continue
        if not isinstance(document, dict):
            errors.append(f"{path.name}: top level is {type(document).__name__}, expected a mapping")
            continue
        workflow_name = document.get("name")
        if not isinstance(workflow_name, str) or not workflow_name.strip():
            workflow_name = path.stem
        jobs = document.get("jobs")
        if not isinstance(jobs, dict):
            errors.append(f"{path.name}: no 'jobs' mapping")
            continue
        for job_key, job in jobs.items():
            if not isinstance(job, dict):
                continue
            declared = job.get("name")
            context = declared if isinstance(declared, str) and declared.strip() else str(job_key)
            node = JobNode(
                context=context,
                job_key=str(job_key),
                workflow_name=workflow_name.strip(),
                workflow_file=path.name,
                needs=_normalise_needs(job.get("needs")),
            )
            by_job[(path.name, str(job_key))] = node
            # First writer wins, and a genuine collision is an error rather
            # than a silent overwrite: two jobs producing one context name
            # makes the "which job is late" answer ambiguous.
            if context in by_context:
                errors.append(
                    f"duplicate context {context!r}: "
                    f"{by_context[context].workflow_file}:{by_context[context].job_key} "
                    f"and {path.name}:{job_key}"
                )
                continue
            by_context[context] = node

    return WorkflowGraph(by_context=by_context, by_job=by_job, parse_errors=tuple(errors))


def _upstream_chain(graph: WorkflowGraph, node: JobNode) -> List[JobNode]:
    """All transitive ``needs`` ancestors of ``node``, nearest first.

    Cycle-safe: a ``needs`` cycle is invalid on GitHub's side, but this walk
    must terminate regardless of what the file says.
    """
    seen: Set[str] = {node.job_key}
    ordered: List[JobNode] = []
    frontier = list(node.needs)
    while frontier:
        job_key = str(frontier.pop(0))
        if job_key in seen:
            continue
        seen.add(job_key)
        parent = graph.by_job.get((node.workflow_file, job_key))
        if parent is None:
            continue
        ordered.append(parent)
        frontier.extend(parent.needs)
    return ordered


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _index_check_runs(check_runs: Iterable[Mapping[str, Any]]) -> Dict[str, List[Mapping[str, Any]]]:
    """Group check runs by name. One name can carry several runs — the same
    workflow triggered by both ``push`` and ``pull_request`` produces two.
    """
    indexed: Dict[str, List[Mapping[str, Any]]] = {}
    for run in check_runs:
        name = run.get("name")
        if isinstance(name, str) and name:
            indexed.setdefault(name, []).append(run)
    return indexed


def _split_by_app(
    runs: Sequence[Mapping[str, Any]], app_id: Optional[int]
) -> tuple:
    """Split check runs into those the requirement accepts and those it does not.

    With no app binding every same-named run counts, which is exactly what the
    legacy ``contexts[]`` shape asks for. With a binding, only runs whose
    ``app.id`` matches count — a run missing the ``app`` object does NOT match,
    since an unidentifiable producer cannot be shown to be the required one.
    """
    if app_id is None:
        return list(runs), []
    matching, foreign = [], []
    for run in runs:
        app = run.get("app")
        observed = app.get("id") if isinstance(app, dict) else None
        (matching if observed == app_id else foreign).append(run)
    return matching, foreign


def _classify_present(context: str, runs: Sequence[Mapping[str, Any]], node: Optional[JobNode]) -> ContextState:
    """Classify a context that HAS at least one check run on the commit.

    Conservative on duplicates: any non-terminal run keeps the context
    ``running``, and any terminal non-passing conclusion makes it ``failed``,
    even when a sibling run of the same name passed. A merge gate resolving a
    disagreement in favour of the passing copy is exactly the green-lie this
    module exists to remove.
    """
    workflow_name = node.workflow_name if node else None
    job_key = node.job_key if node else None

    unfinished = [r for r in runs if (r.get("status") or "") != COMPLETED_STATUS]
    if unfinished:
        return ContextState(
            context=context,
            state=STATE_RUNNING,
            detail=f"still running ({unfinished[0].get('status') or 'unknown status'})",
            conclusion=None,
            workflow_name=workflow_name,
            job_key=job_key,
        )

    conclusions = [(r.get("conclusion") or "") for r in runs]
    failing = [c for c in conclusions if c not in PASSING_CONCLUSIONS]
    if failing:
        return ContextState(
            context=context,
            state=STATE_FAILED,
            detail=f"concluded {failing[0] or 'with no conclusion'}",
            conclusion=failing[0] or None,
            workflow_name=workflow_name,
            job_key=job_key,
        )
    return ContextState(
        context=context,
        state=STATE_PASSED,
        detail=f"concluded {conclusions[0]}",
        conclusion=conclusions[0],
        workflow_name=workflow_name,
        job_key=job_key,
    )


def _pending_upstream(
    graph: WorkflowGraph,
    node: JobNode,
    runs: Sequence[Mapping[str, Any]],
    jobs_by_run_id: Mapping[Any, Sequence[Mapping[str, Any]]],
) -> Set[str]:
    """Ancestor context names that have not concluded yet, for the WAIT text.

    An ancestor absent from a run's job list counts as pending: GitHub does
    not list a job before it is created, and "not created yet" is precisely
    the state that makes the child context wait.
    """
    pending: Set[str] = set()
    for parent in _upstream_chain(graph, node):
        for run in runs:
            observed = next(
                (j for j in jobs_by_run_id.get(run.get("id"), ()) if j.get("name") == parent.context),
                None,
            )
            if observed is None or (observed.get("status") or "") != COMPLETED_STATUS:
                pending.add(parent.context)
    return pending


def _failed_upstream(
    graph: WorkflowGraph,
    node: JobNode,
    runs: Sequence[Mapping[str, Any]],
    jobs_by_run_id: Mapping[Any, Sequence[Mapping[str, Any]]],
) -> Optional[tuple]:
    """First (ancestor, conclusion) that concluded in a non-passing state.

    Returns None when every observed ancestor either passed or has not
    concluded. An unobserved ancestor is never treated as failed — absence of
    evidence stays absence of evidence here, and the caller falls through to
    the wait/never-created decision that has real evidence behind it.
    """
    for run in runs:
        job_states = {
            j.get("name"): j
            for j in jobs_by_run_id.get(run.get("id"), ())
            if isinstance(j.get("name"), str)
        }
        for parent in _upstream_chain(graph, node):
            observed = job_states.get(parent.context)
            if observed is None:
                continue
            if (observed.get("status") or "") != COMPLETED_STATUS:
                continue
            conclusion = observed.get("conclusion") or ""
            if conclusion not in PASSING_CONCLUSIONS:
                return (parent, conclusion)
    return None


def _classify_absent(
    context: str,
    node: JobNode,
    graph: WorkflowGraph,
    workflow_runs: Sequence[Mapping[str, Any]],
    jobs_by_run_id: Mapping[Any, Sequence[Mapping[str, Any]]],
) -> ContextState:
    """Classify a required context with NO check run on the commit.

    This is the whole point of the module: separating "not created yet"
    (#1701, Profile B/C behind ``needs: profile-a``) from "never created"
    (#1691, five contexts lost to an Actions outage).
    """
    own_runs = [r for r in workflow_runs if r.get("name") == node.workflow_name]
    if not own_runs:
        return ContextState(
            context=context,
            state=STATE_NO_RUN,
            detail=(
                f"workflow {node.workflow_name!r} ({node.workflow_file}) has no run on this "
                "commit — nothing is started that could produce this context"
            ),
            workflow_name=node.workflow_name,
            job_key=node.job_key,
        )

    # An ancestor that already concluded in a non-passing state settles the
    # question whether or not the run is still alive: the producing job will
    # not execute, so waiting cannot help.
    failed = _failed_upstream(graph, node, own_runs, jobs_by_run_id)
    if failed is not None:
        parent, conclusion = failed
        return ContextState(
            context=context,
            state=STATE_NEVER_CREATED,
            detail=(
                f"job {node.job_key!r} needs {parent.job_key!r}, which concluded "
                f"{conclusion or 'with no conclusion'} — this context can no longer be "
                "produced on this commit"
            ),
            workflow_name=node.workflow_name,
            job_key=node.job_key,
        )

    alive = [r for r in own_runs if (r.get("status") or "") != COMPLETED_STATUS]
    if alive:
        pending = _pending_upstream(graph, node, own_runs, jobs_by_run_id)
        if pending:
            reason = f"waiting on {', '.join(sorted(pending))}"
        elif node.needs:
            reason = f"declares needs: {', '.join(node.needs)}"
        else:
            reason = "the run has not created this job yet"
        return ContextState(
            context=context,
            state=STATE_WAITING_UPSTREAM,
            detail=(
                f"workflow {node.workflow_name!r} is still running ({reason}) — "
                "the context can still appear"
            ),
            workflow_name=node.workflow_name,
            job_key=node.job_key,
        )

    conclusions = ", ".join(sorted({(r.get("conclusion") or "none") for r in own_runs}))
    return ContextState(
        context=context,
        state=STATE_NEVER_CREATED,
        detail=(
            f"workflow {node.workflow_name!r} finished ({conclusions}) without ever creating "
            "this context — re-run the workflow; this is the #1691 shape"
        ),
        workflow_name=node.workflow_name,
        job_key=node.job_key,
    )


def classify_contexts(
    required_contexts: Iterable[Any],
    check_runs: Iterable[Mapping[str, Any]],
    workflow_runs: Iterable[Mapping[str, Any]],
    graph: WorkflowGraph,
    jobs_by_run_id: Optional[Mapping[Any, Sequence[Mapping[str, Any]]]] = None,
) -> List[ContextState]:
    """Classify every REQUIRED context against what exists on the commit.

    Pure: every input is data, so the decision table is testable without a
    network. ``jobs_by_run_id`` may be empty — the classification degrades to
    "the run is alive, so wait" instead of naming which ancestor it waits on,
    which is a weaker message but never a wrong verdict.

    Contexts are iterated in the order given so the output is stable.
    """
    runs = list(workflow_runs)
    jobs = dict(jobs_by_run_id or {})
    indexed = _index_check_runs(check_runs)

    states: List[ContextState] = []
    for required in required_contexts:
        check = required if isinstance(required, RequiredCheck) else RequiredCheck(str(required))
        context = check.context
        node = graph.by_context.get(context)
        named = indexed.get(context) or []
        present, wrong_app = _split_by_app(named, check.app_id)
        if wrong_app and not present:
            states.append(
                ContextState(
                    context=context,
                    state=STATE_UNVERIFIED,
                    detail=(
                        f"{len(wrong_app)} check run(s) named {context!r} exist on this commit "
                        f"but none is from the required app (app_id={check.app_id}) — branch "
                        "protection would not accept them, and matching on the name alone "
                        "would turn a foreign check into a green light"
                    ),
                    workflow_name=node.workflow_name if node else None,
                    job_key=node.job_key if node else None,
                )
            )
            continue
        if present:
            states.append(_classify_present(context, present, node))
            continue
        if node is None:
            hint = (
                f" (workflow parse errors: {'; '.join(graph.parse_errors)})"
                if graph.parse_errors
                else ""
            )
            states.append(
                ContextState(
                    context=context,
                    state=STATE_UNVERIFIED,
                    detail=(
                        "no check run on this commit and no workflow job in the checkout "
                        f"produces this context — cannot tell 'not yet' from 'never'{hint}"
                    ),
                )
            )
            continue
        states.append(_classify_absent(context, node, graph, runs, jobs))
    return states


def summarise(states: Sequence[ContextState]) -> Dict[str, Any]:
    """Counts + the blocking subset, for a caller that renders a verdict."""
    blocking = [s for s in states if s.blocking]
    return {
        "total": len(states),
        "passed": len([s for s in states if s.state == STATE_PASSED]),
        "blocking": len(blocking),
        "transient": len([s for s in states if s.transient]),
        "never_created": len([s for s in states if s.state == STATE_NEVER_CREATED]),
        "no_run": len([s for s in states if s.state == STATE_NO_RUN]),
        "unverified": len([s for s in states if s.state == STATE_UNVERIFIED]),
        "by_state": {s.context: s.state for s in states},
        "blocking_contexts": [s.to_dict() for s in blocking],
    }


# ---------------------------------------------------------------------------
# GitHub evidence collection
# ---------------------------------------------------------------------------


class CIContextsError(RuntimeError):
    """A required piece of evidence could not be read.

    Raised rather than returned as a degraded result: every caller in this
    fabric turns an unreadable answer into a loud blocking state, and an
    exception makes forgetting that impossible.
    """


def _gh_json(args: Sequence[str], project_root: Path, timeout: int) -> Any:
    """Run ``gh`` and parse its JSON stdout, or raise :class:`CIContextsError`."""
    try:
        proc = subprocess.run(
            ["gh", *args],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise CIContextsError(f"gh CLI not available: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CIContextsError(f"gh {' '.join(args)} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise CIContextsError(
            f"gh {' '.join(args)} failed (rc={proc.returncode}): {(proc.stderr or '').strip()[:300]}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise CIContextsError(f"gh {' '.join(args)} returned unparseable JSON: {exc}") from exc


def fetch_required_checks(
    project_root: Path, branch: str = "main", timeout: int = 20
) -> List[RequiredCheck]:
    """The branch-protection required checks — what SHOULD exist, with app bindings.

    Reads both shapes GitHub has shipped. ``checks[]`` carries ``app_id`` and
    wins over a same-named legacy ``contexts[]`` entry, because dropping the
    app binding would widen the requirement rather than narrow it.

    An empty result is returned as-is and is NOT a pass: a caller that finds no
    required checks on a branch it believes is protected is looking at a
    misconfiguration, and this function refuses to paper over it by inventing
    a default list.
    """
    payload = _gh_json(
        ["api", f"repos/{{owner}}/{{repo}}/branches/{branch}/protection"], project_root, timeout
    )
    if not isinstance(payload, dict):
        raise CIContextsError(f"branch protection payload is {type(payload).__name__}, expected a mapping")
    required = payload.get("required_status_checks") or {}

    bound: Dict[str, Optional[int]] = {}
    for name in required.get("contexts") or []:
        if isinstance(name, str) and name:
            bound.setdefault(name, None)
    for check in required.get("checks") or []:
        if not isinstance(check, dict):
            continue
        name = check.get("context")
        if not isinstance(name, str) or not name:
            continue
        app_id = check.get("app_id")
        # ANY_APP_ID is an int and would otherwise survive as a concrete
        # producer that nothing can ever match.
        bound[name] = (
            app_id if isinstance(app_id, int) and app_id != ANY_APP_ID else None
        )
    return [RequiredCheck(name, bound[name]) for name in sorted(bound)]


def fetch_check_runs(project_root: Path, head_sha: str, timeout: int = 20) -> List[Dict[str, Any]]:
    """The check runs that DO exist on ``head_sha``."""
    # --slurp is required, not decorative: this endpoint returns an OBJECT, and
    # `gh api --paginate` emits one document per page. Measured on a 16-run
    # commit at per_page=5 — four pages, and json.loads fails with "Extra data"
    # at char 15511. --slurp wraps the pages in an outer array instead, which is
    # the shape the loop below already expects.
    payload = _gh_json(
        [
            "api",
            f"repos/{{owner}}/{{repo}}/commits/{head_sha}/check-runs?per_page=100",
            "--paginate",
            "--slurp",
        ],
        project_root,
        timeout,
    )
    runs: List[Dict[str, Any]] = []
    for page in payload if isinstance(payload, list) else [payload]:
        if isinstance(page, dict):
            runs.extend([r for r in (page.get("check_runs") or []) if isinstance(r, dict)])
    return runs


#: Commit-status states mapped onto the check-run vocabulary the classifier
#: already speaks. GitHub's two mechanisms are separate APIs but branch
#: protection treats their contexts as one namespace, so the classifier must
#: too — otherwise a required context satisfied by a STATUS reads as absent.
_STATUS_STATE_TO_CONCLUSION = {
    "success": ("completed", "success"),
    "failure": ("completed", "failure"),
    "error": ("completed", "failure"),
    "pending": ("in_progress", None),
}


def fetch_commit_statuses(project_root: Path, head_sha: str, timeout: int = 20) -> List[Dict[str, Any]]:
    """Legacy commit statuses on ``head_sha``, in check-run shape.

    Branch protection's legacy ``contexts[]`` can be satisfied by a commit
    STATUS (the Statuses API) rather than a check run (the Checks API) — a
    non-Actions CI, or anything posting through ``POST /statuses``. Reading
    only check runs leaves such a context permanently unseen, so a PR whose
    required context genuinely passed would be blocked forever on evidence
    that was there all along.

    Only the most recent status per context is returned: GitHub's combined
    endpoint lists the history newest-first, and an older superseded state is
    not what branch protection evaluates.

    An unrecognised state maps to a non-terminal status rather than to a
    conclusion, so a future state value holds the verdict at "running" instead
    of silently passing or failing.
    """
    payload = _gh_json(
        ["api", f"repos/{{owner}}/{{repo}}/commits/{head_sha}/status?per_page=100"],
        project_root,
        timeout,
    )
    if not isinstance(payload, dict):
        raise CIContextsError(f"commit status payload is {type(payload).__name__}, expected a mapping")
    seen: Set[str] = set()
    out: List[Dict[str, Any]] = []
    for status in payload.get("statuses") or []:
        if not isinstance(status, dict):
            continue
        context = status.get("context")
        if not isinstance(context, str) or not context or context in seen:
            continue
        seen.add(context)
        state, conclusion = _STATUS_STATE_TO_CONCLUSION.get(
            (status.get("state") or "").lower(), ("in_progress", None)
        )
        entry: Dict[str, Any] = {"name": context, "status": state, "conclusion": conclusion}
        # A status has no Checks-API app object unless GitHub supplies one; an
        # app-bound requirement is a `checks[]` entry, which only check runs
        # satisfy, so leaving this absent is the correct answer rather than a
        # gap.
        if isinstance(status.get("app"), dict):
            entry["app"] = status["app"]
        out.append(entry)
    return out


def fetch_workflow_runs(project_root: Path, head_sha: str, timeout: int = 20) -> List[Dict[str, Any]]:
    """The workflow runs on ``head_sha``, with id/name/status/conclusion."""
    payload = _gh_json(
        ["api", f"repos/{{owner}}/{{repo}}/actions/runs?head_sha={head_sha}&per_page=100"],
        project_root,
        timeout,
    )
    if not isinstance(payload, dict):
        raise CIContextsError(f"actions/runs payload is {type(payload).__name__}, expected a mapping")
    return [r for r in (payload.get("workflow_runs") or []) if isinstance(r, dict)]


def fetch_run_jobs(project_root: Path, run_id: Any, timeout: int = 20) -> List[Dict[str, Any]]:
    """The jobs of one workflow run, with name/status/conclusion."""
    payload = _gh_json(
        ["api", f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/jobs?per_page=100"],
        project_root,
        timeout,
    )
    if not isinstance(payload, dict):
        raise CIContextsError(f"actions/runs/{run_id}/jobs payload is {type(payload).__name__}")
    return [j for j in (payload.get("jobs") or []) if isinstance(j, dict)]


def evaluate_commit(
    project_root: Path,
    head_sha: str,
    *,
    branch: str = "main",
    workflows_dir: Optional[Path] = None,
    timeout: int = 20,
    required_contexts: Optional[Sequence[Any]] = None,
) -> List[ContextState]:
    """Classify the protected branch's required contexts against ``head_sha``.

    Two-pass on purpose. The first pass runs with no per-run job data, which
    costs zero extra API calls and is already conclusive for every context
    that has a check run — the all-green case, i.e. most of them. Only when a
    context is genuinely missing does the second pass fetch the jobs of the
    runs that could still produce it, which is the only place the ``needs``
    chain has to be resolved against live state.

    Raises :class:`CIContextsError` when the branch-protection list, the check
    runs, or the workflow runs cannot be read. Job-level detail is the one
    piece allowed to degrade: a failure there costs the "waiting on X"
    wording, never the verdict.
    """
    root = Path(project_root)
    contexts = (
        list(required_contexts)
        if required_contexts is not None
        else fetch_required_checks(root, branch, timeout)
    )
    graph = parse_workflow_graph(workflows_dir or (root / ".github" / "workflows"))
    check_runs = fetch_check_runs(root, head_sha, timeout) + fetch_commit_statuses(
        root, head_sha, timeout
    )
    workflow_runs = fetch_workflow_runs(root, head_sha, timeout)

    first = classify_contexts(contexts, check_runs, workflow_runs, graph)
    interesting = {
        s.workflow_name
        for s in first
        if s.state in (STATE_WAITING_UPSTREAM, STATE_NEVER_CREATED) and s.workflow_name
    }
    if not interesting:
        return first

    jobs_by_run_id: Dict[Any, Sequence[Mapping[str, Any]]] = {}
    for run in workflow_runs:
        if run.get("name") not in interesting:
            continue
        try:
            jobs_by_run_id[run.get("id")] = fetch_run_jobs(root, run.get("id"), timeout)
        except CIContextsError as exc:
            # Degrade the WORDING, never the verdict: without job data the
            # classifier still separates alive-run from finished-run, which is
            # the decision that blocks or releases a merge.
            jobs_by_run_id[run.get("id")] = ()
            graph = WorkflowGraph(
                by_context=graph.by_context,
                by_job=graph.by_job,
                parse_errors=tuple(graph.parse_errors) + (f"jobs for run {run.get('id')}: {exc}",),
            )
    return classify_contexts(contexts, check_runs, workflow_runs, graph, jobs_by_run_id)
