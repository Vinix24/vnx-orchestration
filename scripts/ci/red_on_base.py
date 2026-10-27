#!/usr/bin/env python3
"""``red_on_base``: run a PR's own tests on base code and on head code, verdict per test.

Two worktrees, both removed in a ``finally``; HEAD's selected test files and changed
conftests are copied into the base tree and the conftests the PR deleted are deleted
there too, so only the code under test differs. One pytest run per side writes JUnit XML
plus a collection manifest (``pytest_collection_manifest``), keyed by
``check_xdist_parity.junit_key_function()``; input is the selection of
``classify_ci_profile.py --tests-only``. ``behavior`` needs a demonstrable assertion
failure on base: a failure record naming an ``AssertionError`` (or pytest's rewritten
assert); any other exception on base is ``error_on_base`` and confirms no claim.

Exit codes: 1 on any ``fails_on_head``, ``incomplete`` or false claim (a claimed nodeid
whose verdict is not ``behavior``), 0 otherwise, 2 on unusable input. Fail-closed:
missing or unparsable evidence, a pytest run that did not complete (exit other than 0 or
1, a signal, a timeout), a collection failure or skip, an unrecognised result child, a
test neither side collected and a copy or mirrored deletion that would leave either tree
never pass.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import namedtuple
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

SCRIPTS_CI = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_CI))

from check_xdist_parity import EvidenceError, junit_key_function, load_manifest

VERDICTS = ("behavior", "error_on_base", "not_behavior", "green_on_base", "fails_on_head", "skipped",
            "incomplete")
(BEHAVIOR, ERROR_ON_BASE, NOT_BEHAVIOR, GREEN_ON_BASE, FAILS_ON_HEAD, SKIPPED, INCOMPLETE) = VERDICTS
PASSED, FAILED, ERRORED = "passed", "failed", "error"
COLLECTION_ERROR, COLLECTION_SKIPPED, MISSING, UNKNOWN = (
    "collection_error", "collection_skipped", "missing", "unknown")
ASSERTION_EXCEPTION = "AssertionError"
EXCEPTION_TAIL = re.compile(r":\d+:\s*([A-Za-z_][\w.]*)\s*$")  # pytest longrepr: "path:line: ExceptionName"
EXCEPTION_PREFIX = re.compile(r"([A-Za-z_][\w.]*)\s*:")  # a failure message: "ExceptionName: message"
REWRITTEN_ASSERTION = re.compile(r"^E\s+assert\b", re.MULTILINE)  # pytest's rewritten assert line
COMPLETED_RUN = (0, 1)  # pytest exit codes of a run that went to the end: all passed, some failed
_RESULT_TAGS = ("failure", "error", "skipped")
_IGNORED_TAGS = frozenset({"system-out", "system-err", "properties"})


class InputError(Exception):
    """The caller's input cannot be used; exit 2 and write no verdict."""


@dataclass(frozen=True)
class Outcome:
    """What one side says about one test; *message* carries the exception evidence and *exception*
    the exception class its record names (``""`` when the evidence names none)."""

    kind: str
    message: str = ""
    exception: str = ""


# One side's evidence: JUnit outcomes by key, collected nodeids, collection outcomes, problems.
Side = namedtuple("Side", "outcomes nodeids collection problems")


def outcome_for(side: Side, key: tuple, nodeid: str) -> Outcome:
    """The side's outcome for one test; ``missing`` (blocking) when it has none."""
    if key in side.outcomes:
        return side.outcomes[key]
    for unit, outcome in side.collection:  # a module (or directory) outcome covers its tests
        if nodeid == unit or nodeid.startswith(unit + "::") or nodeid.startswith(unit.rstrip("/") + "/"):
            return outcome
    return Outcome(MISSING, "; ".join(side.problems))


def named_exception(kind: str | None, message: str | None, text: str) -> str:
    """The exception class one failure record names: its ``type`` attribute, the longrepr tail
    (``path:line: ExceptionName``), the message prefix (``ExceptionName: ...``) or pytest's rewritten
    assert line. ``""`` when the record names none, which never proves an assertion (fail-closed)."""
    if kind:
        return kind.strip()
    tails = EXCEPTION_TAIL.findall(text or "")
    if tails:
        return tails[-1]
    prefix = EXCEPTION_PREFIX.match(message or "")
    if prefix:
        return prefix.group(1)
    return ASSERTION_EXCEPTION if REWRITTEN_ASSERTION.search(text or "") else ""


def outcome_of(testcase) -> Outcome:
    """Outcome of one ``<testcase>``; an unrecognised result child is ``unknown`` (fail-closed)."""
    result = None
    for child in testcase:
        if child.tag in _IGNORED_TAGS:
            continue
        if child.tag not in _RESULT_TAGS:
            return Outcome(UNKNOWN, child.tag)
        text = (child.text or "").strip()
        lines = text.splitlines()
        evidence = " ".join(p for p in (child.get("type"), child.get("message"),
                                        lines[0] if lines else None) if p)
        exception = named_exception(child.get("type"), child.get("message"), text)
        if child.tag == "error":
            return Outcome(ERRORED, evidence, exception)
        if child.tag == "failure" or result is None:
            result = Outcome(FAILED if child.tag == "failure" else SKIPPED, evidence, exception)
    return result or Outcome(PASSED)


def is_assertion_failure(base: Outcome) -> bool:
    """Whether a base failure proves asserted behaviour: its record names an ``AssertionError`` (or a
    subclass). Any other exception, and a record that names none, is not behaviour."""
    name = base.exception or named_exception(None, base.message, "")
    return name == ASSERTION_EXCEPTION or name.endswith("." + ASSERTION_EXCEPTION)


def classify_verdict(base: Outcome, head: Outcome) -> str:
    """The single verdict for one test from its two sides (closed set, fail-closed)."""
    if head.kind in (MISSING, UNKNOWN, COLLECTION_SKIPPED) or base.kind in (MISSING, UNKNOWN):
        return INCOMPLETE
    if head.kind in (FAILED, ERRORED, COLLECTION_ERROR):
        return FAILS_ON_HEAD
    if head.kind == SKIPPED or base.kind == SKIPPED:
        return SKIPPED
    if base.kind == COLLECTION_SKIPPED:
        return INCOMPLETE
    if base.kind in (COLLECTION_ERROR, ERRORED):
        return NOT_BEHAVIOR
    if base.kind == PASSED:
        return GREEN_ON_BASE
    if base.kind == FAILED:
        return BEHAVIOR if is_assertion_failure(base) else ERROR_ON_BASE
    return INCOMPLETE


def read_junit(path: Path) -> dict:
    """``{key: Outcome}`` for every test record; ``ValueError`` when unreadable."""
    try:
        root = ET.parse(str(path)).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ValueError(f"unreadable JUnit XML {path}: {exc}") from exc
    return {(case.get("classname") or "", case.get("name") or ""): outcome_of(case)
            for case in root.iter("testcase") if case.get("classname")}


def read_manifest(path: Path, exitstatus: int) -> tuple:
    """``({key: nodeid}, [(collection unit, Outcome)])`` of the completed run that exited *exitstatus*;
    ``ValueError`` when unusable or not that run's manifest of a finished collection."""
    try:
        manifest = load_manifest(path)  # the shared schema/nodeid validation of check_xdist_parity
    except EvidenceError as exc:
        raise ValueError(f"unreadable collection manifest {exc}") from exc
    status = manifest.get("exitstatus")
    if (manifest.get("phase_active") is not True or manifest.get("collection_finished") is not True
            or type(status) is not int or status != exitstatus):
        raise ValueError(f"collection manifest {path} is not a finished collection of a run exiting "
                         f"{exitstatus}: phase_active={manifest.get('phase_active')!r} "
                         f"collection_finished={manifest.get('collection_finished')!r} exitstatus={status!r}")
    kinds = {FAILED: COLLECTION_ERROR, "skipped": COLLECTION_SKIPPED}
    collection = [(item["nodeid"], Outcome(kinds.get(item["outcome"], UNKNOWN),
                                           f"collection {item['outcome']}: {item['reason']}"))
                  for item in manifest["collection_outcomes"]]
    return {junit_key_function()(nodeid): nodeid for nodeid in manifest["nodeids"]}, collection


def _git(repo: Path, *args: str, strict: bool = False) -> str:
    """Stdout of one git call; *strict* raises ``InputError`` naming its stderr when it fails."""
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if strict and (result.returncode != 0 or not result.stdout.strip()):
        raise InputError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def copy_refusal(head_tree: Path, base_tree: Path, rel: str) -> str:
    """Why copying *rel* from head to base could read or write outside the trees; ``""`` when it cannot."""
    source, target = head_tree / rel, base_tree / rel
    for path, tree in ((source, head_tree), (target.parent, base_tree)):  # resolve() follows every parent link
        if not path.resolve().is_relative_to(tree.resolve()):
            return f"refused to copy {rel}: {path} resolves to {path.resolve()}, outside {tree}"
    for path in (source, target):
        if path.is_symlink():
            return f"refused to copy {rel}: {path} is a symlink"
    return ""


def delete_refusal(base_tree: Path, rel: str) -> str:
    """Why deleting *rel* from the base tree could leave or write through a link outside it; ``""`` when
    it cannot (the file, when base has it, is then gone)."""
    target = base_tree / rel
    if not target.parent.resolve().is_relative_to(base_tree.resolve()):
        return (f"refused to delete {rel}: {target.parent} resolves to {target.parent.resolve()}, "
                f"outside {base_tree}")
    if target.is_symlink():
        return f"refused to delete {rel}: {target} is a symlink"
    if target.is_file():
        target.unlink()
    return ""


def copy_head_tests(head_tree: Path, base_tree: Path, files: list, conftests: list) -> dict:
    """Copy HEAD's test files and changed conftests into the base tree, and delete the conftests the PR
    deleted from it; ``{file: reason}`` for every file that cannot be compared: absent from head, a copy
    or a mirrored deletion refused, or any conftest that could not be mirrored."""
    reasons = {}
    for rel in conftests + files:
        present = (head_tree / rel).is_file()
        if present or rel in files:
            reasons[rel] = copy_refusal(head_tree, base_tree, rel)
            if not reasons[rel] and present:
                (base_tree / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(head_tree / rel, base_tree / rel)
            elif not reasons[rel]:
                reasons[rel] = "test file is absent from the head tree"
        else:  # a conftest the PR deleted leaves the base tree too, so it cannot fail base only
            reasons[rel] = delete_refusal(base_tree, rel)
    refused_conftests = "; ".join(reasons[rel] for rel in conftests if reasons[rel])
    return {rel: refused_conftests or reasons[rel] for rel in files if refused_conftests or reasons[rel]}


def read_side(xml_path: Path, manifest_path: Path, exitstatus: int) -> Side:
    """The evidence of a pytest run that exited *exitstatus*: none, with the reason as ``problem``, unless
    the run completed, its manifest says so and every collected test has a JUnit result."""
    if exitstatus not in COMPLETED_RUN:  # interrupted, internal or usage error, no tests, a signal
        return Side({}, {}, [], [f"exit status {exitstatus} is not a completed pytest run (0 or 1)"])
    try:
        outcomes, (nodeids, collection) = read_junit(xml_path), read_manifest(manifest_path, exitstatus)
    except ValueError as exc:
        return Side({}, {}, [], [str(exc)])
    unrecorded = sorted(nodeid for key, nodeid in nodeids.items() if key not in outcomes)
    if unrecorded:
        return Side({}, {}, [], [f"{xml_path} has no result for {len(unrecorded)} collected test(s), "
                                 f"first {unrecorded[0]}"])
    return Side(outcomes, nodeids, collection, [])


def run_side(tree: Path, files: list, prefix: Path, timeout: float) -> Side:
    """One pytest run in *tree*, returned as its evidence (``read_side``) or as the ``problem`` why none."""
    xml_path, manifest_path = prefix.with_suffix(".xml"), prefix.with_suffix(".json")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(tree), str(SCRIPTS_CI), env.get("PYTHONPATH")]))
    argv = [sys.executable, "-m", "pytest", "--continue-on-collection-errors",
            "-p", "no:cacheprovider", "-p", "pytest_collection_manifest",
            "--collection-manifest", str(manifest_path), "--collection-manifest-phase", prefix.name,
            "--junitxml", str(xml_path), *files]
    try:
        process = subprocess.run(argv, cwd=str(tree), env=env, capture_output=True, text=True,
                                 timeout=timeout, start_new_session=True)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return Side({}, {}, [], [f"pytest in {tree} did not complete: {exc!r}"])
    side = read_side(xml_path, manifest_path, process.returncode)
    tail = " | ".join((process.stdout + process.stderr).strip().splitlines()[-3:])
    return side._replace(problems=[f"pytest in {tree}: {problem}: {tail}" for problem in side.problems])


def _incomplete(nodeid: str, message: str) -> dict:
    return {"nodeid": nodeid, "verdict": INCOMPLETE, "base_outcome": MISSING,
            "head_outcome": MISSING, "base_message": message}


def compare(base: Side, head: Side, claims: list) -> tuple:
    """Per-test verdicts and false claims from both sides' evidence."""
    known = {**base.nodeids, **head.nodeids}
    claimed = {junit_key_function()(claim): claim for claim in claims}
    pairs = [(known.get(item) or claimed.get(item) or f"{item[0]}::{item[1]}", item)
             for item in set(known) | set(claimed) | set(base.outcomes) | set(head.outcomes)]
    entries, false_claims = [], []
    for nodeid, item in sorted(pairs):
        base_outcome, head_outcome = outcome_for(base, item, nodeid), outcome_for(head, item, nodeid)
        verdict = classify_verdict(base_outcome, head_outcome)
        entries.append({"nodeid": nodeid, "verdict": verdict, "base_outcome": base_outcome.kind,
                        "head_outcome": head_outcome.kind, "base_message": base_outcome.message or None})
        if item in claimed and verdict != BEHAVIOR:
            false_claims.append(nodeid)
    return entries, false_claims


def verify(repo: Path, base_rev: str, head_rev: str, files: list, conftests: list,
           claims: list, timeout: float) -> tuple:
    """Run both sides in their own worktrees; ``(entries, false_claims)``."""
    tmp = Path(tempfile.mkdtemp(prefix="red-on-base-"))
    base_tree, head_tree = tmp / "base", tmp / "head"
    try:
        for tree, rev in ((base_tree, base_rev), (head_tree, head_rev)):
            _git(repo, "worktree", "add", "--detach", str(tree),
                 _git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}", strict=True), strict=True)
        unusable = copy_head_tests(head_tree, base_tree, files, conftests)
        runnable = [rel for rel in files if rel not in unusable]
        entries, false_claims = [], list(claims)  # a claim no comparison confirmed is a false claim
        if runnable:  # an empty argument list would make pytest collect the whole tree
            base, head = (run_side(tree, runnable, tmp / tree.name, timeout) for tree in (base_tree, head_tree))
            if base.problems or head.problems:  # one side without evidence leaves no test comparable
                entries = [_incomplete(rel, "; ".join(base.problems + head.problems)) for rel in runnable]
            else:
                entries, false_claims = compare(base, head, claims)
        entries += [_incomplete(rel, reason) for rel, reason in unusable.items()]
        covered = {entry["nodeid"].split("::")[0] for entry in entries}
        entries += [_incomplete(rel, "no per-test result was collected for this file") for rel in files
                    if not any(path == rel or path.startswith(rel.rstrip("/") + "/") for path in covered)]
        return sorted(entries, key=lambda entry: entry["nodeid"]), false_claims
    finally:
        for tree in (base_tree, head_tree):
            if tree.exists():
                _git(repo, "worktree", "remove", "--force", str(tree))
                shutil.rmtree(tree, ignore_errors=True)
        _git(repo, "worktree", "prune")
        shutil.rmtree(tmp, ignore_errors=True)


def _paths(what: str, raws: list) -> list:
    """Deduplicated repo-relative paths; ``InputError`` when one is not usable."""
    for raw in raws:
        path = PurePosixPath(raw or "")
        if not raw or path.is_absolute() or ".." in path.parts:
            raise InputError(f"{what}: {raw!r} is not a repo-relative path inside the tree")
    return list(dict.fromkeys(raws))


def load_inputs(selection_path: Path, claims_path: Path | None) -> tuple:
    """``(test files, changed conftests, claims)``; ``InputError`` when the input is unusable."""
    try:
        data = json.loads(selection_path.read_text(encoding="utf-8"))
        selected, conftests = data["selected"], data.get("conftest_changed", [])
        lines = [] if claims_path is None else claims_path.read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise InputError(f"unreadable selection or claims file: {exc}") from exc
    if not isinstance(selected, list) or not isinstance(conftests, list) or not all(
            isinstance(item, str) for item in selected + conftests):
        raise InputError(f"selection {selection_path}: expected lists of paths")
    claims = [line.strip() for line in lines if line.strip()]
    return (_paths("selection", selected + [claim.split("::")[0] for claim in claims]),
            _paths("conftest_changed", conftests), claims)


def write_verdict(path: Path, base: str, head: str, entries: list, false_claims: list, no_test: bool) -> dict:
    """Write the verdict JSON atomically and return it; the summary covers the closed set."""
    summary = {verdict: sum(entry["verdict"] == verdict for entry in entries) for verdict in VERDICTS}
    verdict = {"base": base, "head": head, "tests": entries, "false_claims": false_claims,
               "summary": summary, "no_test": no_test}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(verdict, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return verdict


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="base commit (sha or any rev)")
    parser.add_argument("--head", required=True, help="head commit (sha or any rev)")
    parser.add_argument("--selection", required=True, help="JSON file from classify_ci_profile.py --tests-only")
    parser.add_argument("--claims", help="file with one claimed-red-on-base nodeid per line")
    parser.add_argument("--out", required=True, help="where to write the verdict JSON")
    parser.add_argument("--timeout", type=float, default=600.0, help="wall-clock seconds per pytest side")
    args = parser.parse_args(argv)
    try:
        files, conftests, claims = load_inputs(Path(args.selection), Path(args.claims) if args.claims else None)
        if not files and not claims:
            write_verdict(Path(args.out), args.base, args.head, [], [], True)
            print("red_on_base: no_test (empty selection and no claims)")
            return 0
        entries, false_claims = verify(Path(_git(Path.cwd(), "rev-parse", "--show-toplevel", strict=True)),
                                       args.base, args.head, files, conftests, claims, args.timeout)
    except InputError as exc:
        print(f"red_on_base: {exc}", file=sys.stderr)
        return 2
    verdict = write_verdict(Path(args.out), args.base, args.head, entries, false_claims, False)
    counts = " ".join(f"{name}={count}" for name, count in verdict["summary"].items() if count)
    print(f"red_on_base: {counts or 'no tests'} false_claims={len(false_claims)}")
    return 1 if false_claims or any(entry["verdict"] in (FAILS_ON_HEAD, INCOMPLETE) for entry in entries) else 0


if __name__ == "__main__":
    sys.exit(main())
