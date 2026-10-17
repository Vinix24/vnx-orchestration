#!/usr/bin/env python3
"""xdist parity: did the two-phase shape run exactly what the serial sweep ran?

Track ``ci-profile-a-xdist``, D2. Compares a reference set (Profile A's serial
sweep: ``junit-full.xml`` + ``manifest-full.json``) with a candidate set (the
xdist shadow: ``junit-serial.xml`` + ``manifest-serial.json`` for the serial
list, ``junit-parallel.xml`` + ``manifest-parallel.json`` for the rest). The
manifests are written by ``scripts/ci/pytest_collection_manifest.py`` in the
same run as the XML.

The contract (plan "The comparison contract (D2)"):

* Key. A test is the ``(classname, name)`` pair JUnit writes, derived from each
  manifest nodeid with pytest's own ``mangle_test_address`` and
  ``bin_xml_escape`` (the pinned pytest of ``profile_a_constraints.txt``). A
  collection outcome (a file skipped or erroring at collection) is keyed by
  its file, separately from the test keys. Two nodeids on one key: exit 2.
* Execution. The records of one key in one phase. pytest's junitxml splits a
  call failure plus a teardown error into two records (SEOcrawler M16); that
  pair is one execution. Its class is fail if any record holds a failure or an
  error, else xfail, skip or pass. Two executions of a key in one phase, or
  executions in both candidate phases, is "run more than once".
* Executed set. The keys that have a record. Not the manifest's nodeids: a
  serial run with a collection error collects everything and runs nothing,
  while the xdist shape runs every other file (measured on the pinned tools).
* Differences (exit 1): not run in the candidate, extra in the candidate, run
  more than once, outcome class differs (pass, fail, xfail), skip set differs
  (a key skipped in one set and not in the other), collection outcome differs
  (a file skipped or erroring at collection in one set and not the other, or
  with another outcome), the two candidate phases do not partition the
  reference selection.
* Invalid evidence (exit 2): a manifest or XML missing, empty or unparsable
  for an active phase; a key collision; a record whose key is in neither the
  manifest's nodeids nor its collection outcomes; workers that disagree; tool
  versions that differ from the constraints file.
* Exit 0 only when the candidate executed exactly the reference set with the
  same outcome classes.

The skip set is a set of keys; skip reasons are carried in the entries for
triage but never compared. Measured on run 38951854817 (head 0d74af29): 6 of
51 skip reasons differed between the shapes only in a temporary path, a
worker's ``popen-gw<N>`` basetemp level, its own ``mktemp`` numbering, or a
``tempfile.mkdtemp`` suffix that differs between any two processes, also two
serial runs.

The script never reads pytest's summary line or the ``tests``/``skipped``
counts of ``<testsuite>``: on the xdist controller ``testscollected`` is not a
sum (SEOcrawler M27), and a conftest hook in this repository drops 31
``tests/f39`` tests from ``session.items`` without ``pytest_deselected``, so
the summary counts tests that never run.

Subcommands: ``compare`` writes ``xdist-parity-verdict.json`` (and a step
summary); ``fingerprint`` prints the selection fingerprint of an args
directory written by ``scripts/ci/profile_a_pytest_args.sh`` with
``VNX_CI_XDIST=1``. Exit codes of ``compare``: the verdict's exit (0, 1, 2),
or 0 when the run is not applicable (light profile).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

VERDICT_SCHEMA = "vnx-xdist-parity/1"
MANIFEST_SCHEMA = "vnx-collection-manifest/1"
FINGERPRINT_SCHEMA = "vnx-selection-fingerprint/1"
VERDICT_FILE = "xdist-parity-verdict.json"
ROOT_PLACEHOLDER = "<workspace>"
DIFFERENCE_KINDS = (
    "not_run_in_candidate",
    "extra_in_candidate",
    "run_more_than_once",
    "outcome_class_differs",
    "skip_set_differs",
    "collection_outcome_differs",
    "partition",
)
# phase (the file stem: junit-<phase>.xml, manifest-<phase>.json) -> the mode an
# active manifest of that phase must record
PHASE_MODES = {"full": "serial", "serial": "serial", "parallel": "xdist"}
PARITY_STEPS = ("checkout", "prelude", "selection", "download_reference", "download_candidate")
CLASS_ORDER = ("fail", "xfail", "skip", "pass")
_TEARDOWN_PREFIX = "failed on teardown with"
_XPASS_AS_SKIPPED = "xfail-marked test passes unexpectedly"
_SUMMARY_LIMIT = 20


class EvidenceError(Exception):
    """A piece of evidence that cannot be read; carries the invalid-evidence reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


# --- the selection fingerprint ---------------------------------------------------


def strip_root(arg: str, workspace_root: str) -> str:
    """*arg* with the workspace root replaced by a placeholder (whole path components only)."""
    root = workspace_root.rstrip("/")
    if not root:
        return arg
    return re.sub(re.escape(root) + r"(?=/|$)", ROOT_PLACEHOLDER, arg)


def read_vector(path: Path) -> list:
    """One NUL-delimited argument vector written by profile_a_pytest_args.sh."""
    data = path.read_bytes()
    if not data:
        return []
    if not data.endswith(b"\0"):
        raise ValueError(f"{path} is not NUL-terminated")
    return data[:-1].decode("utf-8").split("\0")


def read_args_dir(directory: Path) -> tuple:
    """(serial vector or None, parallel vector) of an ``VNX_CI_XDIST=1`` emission."""
    parallel = read_vector(directory / "parallel.args")
    serial_path = directory / "serial.args"
    serial = read_vector(serial_path) if serial_path.exists() else None
    return serial, parallel


def selection_fingerprint(serial_args, parallel_args, constraints_text: str, workspace_root: str) -> str:
    """sha256 over the two emitted vectors (root stripped) and the constraints file's contents.

    Computed from what the args script emits, not from its bytes, so a change
    to the script that leaves the xdist vectors alone leaves the fingerprint
    alone. An absent serial phase (empty serial list) is ``null``, distinct
    from any vector.
    """
    payload = {
        "schema": FINGERPRINT_SCHEMA,
        "serial": None if serial_args is None else [strip_root(a, workspace_root) for a in serial_args],
        "parallel": [strip_root(a, workspace_root) for a in parallel_args],
        "constraints": constraints_text,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def fingerprint_of_args_dir(directory: Path, constraints: Path, workspace_root: str) -> str:
    serial, parallel = read_args_dir(directory)
    return selection_fingerprint(serial, parallel, constraints.read_text(encoding="utf-8"), workspace_root)


# --- pins and the key function -------------------------------------------------------


def _dist_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def read_pins(constraints: Path) -> dict:
    """``{distribution: version}`` from the ``name==version`` lines of a constraints file."""
    pins = {}
    for line in constraints.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if "==" in line:
            name, version = line.split("==", 1)
            pins[_dist_name(name.strip())] = version.strip()
    return pins


def junit_key_function():
    """nodeid -> (classname, name), exactly as the installed pytest's junitxml writes it."""
    from _pytest.junitxml import bin_xml_escape, mangle_test_address

    def key(nodeid: str) -> tuple:
        names = mangle_test_address(nodeid)
        return ".".join(names[:-1]), bin_xml_escape(names[-1])

    return key


# --- records and executions -----------------------------------------------------------


@dataclass
class Record:
    cls: str
    has_failure: bool
    teardown_only: bool
    skip_reason: str | None


@dataclass
class Execution:
    cls: str
    has_failure: bool
    skip_reason: str | None
    records: int = 1
    paired: bool = False


def classify_record(testcase) -> Record:
    """Outcome class of one ``<testcase>`` element, from its result children."""
    failures = errors = teardown_errors = 0
    skip = xfail = None
    for child in testcase:
        message = child.get("message") or ""
        if child.tag == "failure":
            failures += 1
        elif child.tag == "error":
            errors += 1
            teardown_errors += message.startswith(_TEARDOWN_PREFIX)
        elif child.tag == "skipped":
            kind = child.get("type")
            if kind == "pytest.xfail":
                xfail = message
            elif kind is None and message == _XPASS_AS_SKIPPED:
                failures += 1
            else:
                skip = message
    if failures or errors:
        cls = "fail"
    else:
        cls = "xfail" if xfail is not None else "skip" if skip is not None else "pass"
    teardown_only = bool(errors) and errors == teardown_errors and not failures and skip is None and xfail is None
    return Record(cls, failures > 0, teardown_only, skip)


def executions_of(records: list) -> list:
    """Group one key's records (document order) into executions; the M16 pair is one."""
    executions = []
    for record in records:
        if record.teardown_only:
            target = next((e for e in executions if e.has_failure and not e.paired), None)
            if target is not None:
                target.paired = True
                target.records += 1
                continue
        executions.append(Execution(record.cls, record.has_failure, record.skip_reason))
    return executions


def combined_class(executions: list) -> str:
    classes = {e.cls for e in executions}
    return next(c for c in CLASS_ORDER if c in classes)


def combined_skip_reason(executions: list) -> str | None:
    return next((e.skip_reason for e in executions if e.cls == "skip"), None)


# --- one phase of evidence ------------------------------------------------------------


@dataclass
class Phase:
    name: str
    manifest: dict
    active: bool
    nodeids: list
    nodeid_by_key: dict = field(default_factory=dict)
    collection_by_key: dict = field(default_factory=dict)
    executions: dict = field(default_factory=dict)
    collection_records: dict = field(default_factory=dict)
    record_count: int = 0


def load_manifest(path: Path) -> dict:
    if not path.is_file():
        raise EvidenceError("missing_manifest", str(path))
    try:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            raise EvidenceError("empty_manifest", str(path))
        manifest = json.loads(text)
    except (OSError, ValueError) as exc:
        raise EvidenceError("unparsable_manifest", f"{path}: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise EvidenceError("unparsable_manifest", f"{path}: not a {MANIFEST_SCHEMA} document")
    if not isinstance(manifest.get("nodeids"), list) or not isinstance(manifest.get("collection_outcomes"), list):
        raise EvidenceError("unparsable_manifest", f"{path}: nodeids or collection_outcomes is not a list")
    return manifest


def load_testcases(path: Path) -> list:
    """The ``<testcase>`` elements of a JUnit XML, in document order."""
    if not path.is_file():
        raise EvidenceError("missing_xml", str(path))
    if path.stat().st_size == 0:
        raise EvidenceError("empty_xml", str(path))
    try:
        root = ET.parse(str(path)).getroot()
    except (OSError, ET.ParseError) as exc:
        raise EvidenceError("unparsable_xml", f"{path}: {exc}") from exc
    return list(root.iter("testcase"))


def manifest_problems(manifest: dict, phase: str, pins: dict) -> list:
    """Invalid-evidence reasons a manifest carries by itself."""
    problems = []
    if manifest.get("phase") != phase:
        problems.append(("manifest_phase_mismatch", f"manifest of phase {phase} records phase {manifest.get('phase')!r}"))
    versions = manifest.get("tool_versions") or {}
    for dist, pinned in sorted(pins.items()):
        if versions.get(dist) != pinned:
            problems.append(("tool_version_mismatch", f"{phase}: {dist} {versions.get(dist)!r}, constraints pin {pinned!r}"))
    if not manifest.get("phase_active"):
        if phase != "serial":
            problems.append(("inactive_phase", f"phase {phase} must be active"))
        return problems
    if manifest.get("mode") != PHASE_MODES[phase]:
        problems.append(("manifest_mode_mismatch", f"{phase}: mode {manifest.get('mode')!r}, expected {PHASE_MODES[phase]!r}"))
    if manifest.get("collection_finished") is not True:
        problems.append(("collection_unfinished", f"{phase}: the run did not finish collection"))
    if manifest.get("mode") == "xdist":
        problems.extend(_xdist_problems(manifest, phase))
    return problems


def _xdist_problems(manifest: dict, phase: str) -> list:
    block = manifest.get("xdist") or {}
    problems = []
    if block.get("workers_agree") is not True:
        problems.append(("workers_disagree", f"{phase}: the workers did not collect the same nodeids"))
    workers = int(manifest.get("worker_count") or 0)
    if workers < 1 or block.get("workers_reported") != workers:
        problems.append(("workers_missing", f"{phase}: {block.get('workers_reported')} of {workers} workers reported a collection"))
    return problems


def index_keys(phase: Phase, key, known: dict, problems: list) -> None:
    """Fill the phase's key maps; record every collision (also across phases, via *known*)."""
    if len(set(phase.nodeids)) != len(phase.nodeids):
        problems.append(("key_collision", f"{phase.name}: the manifest lists a nodeid more than once"))
    for nodeid in phase.nodeids:
        phase.nodeid_by_key[_claim_key(key(nodeid), nodeid, known, problems)] = nodeid
    for outcome in phase.manifest["collection_outcomes"]:
        nodeid = str(outcome.get("nodeid", ""))
        k = _claim_key(key(nodeid), nodeid, known, problems)
        if k in phase.nodeid_by_key:
            problems.append(("key_collision", f"{phase.name}: collection outcome {nodeid} shares its key with a test"))
        phase.collection_by_key.setdefault(k, []).append(outcome)


def _claim_key(k: tuple, nodeid: str, known: dict, problems: list) -> tuple:
    owner = known.setdefault(k, nodeid)
    if owner != nodeid:
        problems.append(("key_collision", f"{owner!r} and {nodeid!r} map to one JUnit key {list(k)!r}"))
    return k


def attach_records(phase: Phase, testcases: list, problems: list) -> None:
    """Match each record to a test or a collection outcome of the same phase."""
    by_key = {}
    outside = []
    for testcase in testcases:
        k = (testcase.get("classname") or "", testcase.get("name") or "")
        phase.record_count += 1
        if k in phase.nodeid_by_key:
            by_key.setdefault(k, []).append(classify_record(testcase))
        elif k in phase.collection_by_key:
            phase.collection_records[k] = phase.collection_records.get(k, 0) + 1
        else:
            outside.append(k)
    phase.executions = {k: executions_of(records) for k, records in by_key.items()}
    if outside:
        shown = ", ".join(f"{c}::{n}" for c, n in outside[:_SUMMARY_LIMIT])
        problems.append(("record_outside_manifest", f"{phase.name}: {len(outside)} record(s) not in the manifest: {shown}"))


def load_phase(directory: Path, name: str, pins: dict, key, known: dict, problems: list) -> Phase | None:
    """Read and check one phase; ``None`` (with the reason in *problems*) when unreadable."""
    try:
        manifest = load_manifest(directory / f"manifest-{name}.json")
    except EvidenceError as exc:
        problems.append((exc.reason, exc.detail))
        return None
    problems.extend(manifest_problems(manifest, name, pins))
    phase = Phase(name, manifest, bool(manifest.get("phase_active")), [str(n) for n in manifest["nodeids"]])
    index_keys(phase, key, known, problems)
    xml_path = directory / f"junit-{name}.xml"
    if not phase.active and not xml_path.exists():
        return phase
    try:
        attach_records(phase, load_testcases(xml_path), problems)
    except EvidenceError as exc:
        problems.append((exc.reason, exc.detail))
        return None
    return phase


# --- the comparison -----------------------------------------------------------------------


def _entry(k: tuple, nodeid_of: dict, **extra) -> dict:
    return {"nodeid": nodeid_of.get(k, f"{k[0]}::{k[1]}"), "key": list(k), **extra}


def compare_partition(reference: Phase, serial: Phase, parallel: Phase) -> list:
    """The candidate phases must split the reference selection: union equal, intersection empty."""
    ref, ser, par = set(reference.nodeids), set(serial.nodeids), set(parallel.nodeids)
    entries = [{"nodeid": n, "problem": "in both candidate phases"} for n in ser & par]
    entries += [{"nodeid": n, "problem": "in no candidate phase"} for n in ref - (ser | par)]
    entries += [{"nodeid": n, "problem": "not in the reference selection"} for n in (ser | par) - ref]
    ser_coll = {o.get("nodeid") for o in serial.manifest["collection_outcomes"]}
    par_coll = {o.get("nodeid") for o in parallel.manifest["collection_outcomes"]}
    entries += [{"nodeid": n, "problem": "collection outcome in both candidate phases"} for n in ser_coll & par_coll]
    return sorted(entries, key=lambda e: (e["nodeid"], e["problem"]))


def candidate_executions(serial: Phase, parallel: Phase) -> dict:
    """key -> {phase: executions} over both candidate phases."""
    merged = {}
    for phase in (serial, parallel):
        for k, executions in phase.executions.items():
            merged.setdefault(k, {})[phase.name] = executions
    return merged


def compare_runs(reference: Phase, candidate: dict, nodeid_of: dict) -> dict:
    """not run, extra, run more than once, outcome class and skip set."""
    out = {kind: [] for kind in DIFFERENCE_KINDS if kind not in ("collection_outcome_differs", "partition")}
    ref_keys, cand_keys = set(reference.executions), set(candidate)
    out["not_run_in_candidate"] = [_entry(k, nodeid_of) for k in ref_keys - cand_keys]
    out["extra_in_candidate"] = [_entry(k, nodeid_of) for k in cand_keys - ref_keys]
    for k, executions in reference.executions.items():
        if len(executions) > 1:
            out["run_more_than_once"].append(_entry(k, nodeid_of, executions={"reference": len(executions)}))
    for k, by_phase in candidate.items():
        if len(by_phase) > 1 or any(len(e) > 1 for e in by_phase.values()):
            out["run_more_than_once"].append(_entry(k, nodeid_of, executions={p: len(e) for p, e in by_phase.items()}))
    for k in ref_keys & cand_keys:
        _compare_classes(k, reference.executions[k], [e for es in candidate[k].values() for e in es], nodeid_of, out)
    return out


def _compare_classes(k: tuple, ref_execs: list, cand_execs: list, nodeid_of: dict, out: dict) -> None:
    ref_cls, cand_cls = combined_class(ref_execs), combined_class(cand_execs)
    if ref_cls == cand_cls:
        return
    if "skip" in (ref_cls, cand_cls):
        out["skip_set_differs"].append(_entry(
            k, nodeid_of, reference=ref_cls, candidate=cand_cls,
            reference_reason=combined_skip_reason(ref_execs), candidate_reason=combined_skip_reason(cand_execs),
        ))
    else:
        out["outcome_class_differs"].append(_entry(k, nodeid_of, reference=ref_cls, candidate=cand_cls))


def _collection_form(outcomes: list) -> dict:
    """nodeid -> sorted outcomes ("skipped", "failed") of the files that did not collect cleanly."""
    form = {}
    for outcome in outcomes:
        form.setdefault(str(outcome.get("nodeid")), set()).add(str(outcome.get("outcome")))
    return {nodeid: sorted(states) for nodeid, states in form.items()}


def compare_collection(reference: Phase, serial: Phase, parallel: Phase) -> list:
    ref = _collection_form(reference.manifest["collection_outcomes"])
    cand = _collection_form(serial.manifest["collection_outcomes"] + parallel.manifest["collection_outcomes"])
    return [
        {"nodeid": nodeid, "reference": ref.get(nodeid, []), "candidate": cand.get(nodeid, [])}
        for nodeid in sorted(set(ref) | set(cand))
        if ref.get(nodeid) != cand.get(nodeid)
    ]


def compare(reference: Phase, serial: Phase, parallel: Phase) -> dict:
    """Every difference list of the contract, each sorted by nodeid."""
    nodeid_of = {**reference.nodeid_by_key, **serial.nodeid_by_key, **parallel.nodeid_by_key}
    differences = compare_runs(reference, candidate_executions(serial, parallel), nodeid_of)
    differences["collection_outcome_differs"] = compare_collection(reference, serial, parallel)
    differences["partition"] = compare_partition(reference, serial, parallel)
    for kind in DIFFERENCE_KINDS:
        differences[kind] = sorted(differences[kind], key=lambda e: json.dumps(e, sort_keys=True))
    return {kind: differences[kind] for kind in DIFFERENCE_KINDS}


def phase_counts(phase: Phase | None) -> dict | None:
    if phase is None:
        return None
    classes = dict.fromkeys(CLASS_ORDER, 0)
    for executions in phase.executions.values():
        classes[combined_class(executions)] += 1
    return {
        "active": phase.active,
        "collected": len(phase.nodeids),
        "executed": len(phase.executions),
        "records": phase.record_count,
        "classes": classes,
        "collection_outcomes": len(phase.manifest["collection_outcomes"]),
        "collection_records": sum(phase.collection_records.values()),
    }


# --- the verdict ----------------------------------------------------------------------------


def read_int(path: Path) -> int | None:
    try:
        digits = "".join(ch for ch in path.read_text(encoding="utf-8") if ch.isdigit())
    except OSError:
        return None
    return int(digits) if digits else None


def read_shadow_record(candidate_dir: Path) -> dict | None:
    try:
        record = json.loads((candidate_dir / "xdist-shadow.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def shadow_block(record: dict | None, job_result: str | None) -> dict:
    if record is None:
        return {"record": "missing", "job_result": job_result}
    durations = record.get("durations_s") or {}
    parallel = durations.get("parallel")
    wall = None if parallel is None else parallel + (durations.get("serial") or 0)
    return {
        "record": "present",
        "job_result": job_result,
        "verdict": record.get("verdict"),
        "run_attempt": record.get("run_attempt"),
        "steps": record.get("steps"),
        "exit_codes": record.get("exit_codes"),
        "durations_s": durations,
        "suite_wall_s": wall,
    }


def step_failures(steps: dict, shadow: dict) -> list:
    """Every parity-job step and shadow outcome that is not success, as ``where.what=outcome``."""
    failures = [f"parity.{name}={steps.get(name) or 'unknown'}" for name in PARITY_STEPS if steps.get(name) != "success"]
    if shadow.get("record") != "present":
        failures.append("shadow.record=missing")
    elif shadow.get("verdict") != "shadow_passed":
        failures.append(f"shadow.verdict={shadow.get('verdict')}")
    if shadow.get("job_result") != "success":
        failures.append(f"shadow.job={shadow.get('job_result') or 'unknown'}")
    return failures


@dataclass
class CompareInputs:
    reference_dir: Path
    candidate_dir: Path
    constraints: Path
    args_dir: Path | None
    workspace_root: str
    profile: str
    meta: dict
    steps: dict
    upstream: dict


def resolve_profile(inputs: CompareInputs, shadow: dict | None) -> str:
    """The parity job's own classification; the shadow's when the parity prelude gave none."""
    if inputs.profile:
        return inputs.profile
    if shadow and shadow.get("profile") in ("light", "heavy"):
        return str(shadow["profile"])
    return "unknown"


def own_tool_problems(pins: dict) -> list:
    """The parity check derives keys with the installed pytest: it must be the pinned one."""
    try:
        installed = metadata.version("pytest")
        junit_key_function()
    except (metadata.PackageNotFoundError, ImportError) as exc:
        return [("pytest_unavailable", f"the pinned pytest is not importable here: {exc}")]
    if installed != pins.get("pytest"):
        return [("parity_tool_version_mismatch", f"the parity check runs pytest {installed}, constraints pin {pins.get('pytest')!r}")]
    return []


def fingerprint_block(inputs: CompareInputs, problems: list) -> tuple:
    """(fingerprint, source): this job's own emission first, the shadow's emitted args second."""
    constraints = inputs.constraints
    fingerprints = {}
    for source, directory in (("parity_job", inputs.args_dir), ("shadow", inputs.candidate_dir / "args")):
        if directory is None:
            continue
        try:
            fingerprints[source] = fingerprint_of_args_dir(directory, constraints, inputs.workspace_root)
        except (OSError, ValueError, UnicodeDecodeError):
            continue
    if len(set(fingerprints.values())) > 1:
        problems.append(("fingerprint_mismatch", f"the shadow ran another selection than this commit emits: {fingerprints}"))
    for source in ("parity_job", "shadow"):
        if source in fingerprints:
            return fingerprints[source], source
    problems.append(("fingerprint_unavailable", "neither this job nor the shadow left emitted args"))
    return None, None


def evaluate(inputs: CompareInputs) -> dict:
    """The full verdict for one run (also when the evidence is unusable)."""
    shadow_record = read_shadow_record(inputs.candidate_dir)
    profile = resolve_profile(inputs, shadow_record)
    verdict = {"schema": VERDICT_SCHEMA, **inputs.meta, "profile": profile, "steps": inputs.steps}
    verdict["shadow"] = shadow_block(shadow_record, inputs.upstream.get("shadow"))
    verdict["reference"] = {
        "job_result": inputs.upstream.get("profile_a"),
        "exit": read_int(inputs.reference_dir / "exit-full"),
        "duration_s": read_int(inputs.reference_dir / "duration-full-s"),
    }
    if profile == "light":
        verdict.update(exit=None, verdict="not_applicable", invalid_evidence=[], differences=None, counts=None,
                       tool_versions=None, selection_fingerprint=None, selection_fingerprint_source=None,
                       step_failures=[])
        return verdict
    problems = [("download_failed", f"{name}={inputs.steps[name]}")
                for name in ("download_reference", "download_candidate")
                if name in inputs.steps and inputs.steps[name] != "success"]
    verdict["selection_fingerprint"], verdict["selection_fingerprint_source"] = fingerprint_block(inputs, problems)
    verdict.update(_compare_evidence(inputs, problems))
    verdict["invalid_evidence"] = [{"reason": r, "detail": d} for r, d in problems]
    has_differences = any(verdict["differences"].values()) if verdict["differences"] else False
    verdict["exit"] = 2 if problems else (1 if has_differences else 0)
    verdict["verdict"] = {0: "parity", 1: "differences", 2: "invalid_evidence"}[verdict["exit"]]
    verdict["step_failures"] = step_failures(inputs.steps, verdict["shadow"])
    return verdict


def _compare_evidence(inputs: CompareInputs, problems: list) -> dict:
    try:
        pins = read_pins(inputs.constraints)
    except OSError as exc:
        problems.append(("unreadable_constraints", str(exc)))
        return {"differences": None, "counts": None, "tool_versions": None}
    own = own_tool_problems(pins)
    problems.extend(own)
    if own and own[0][0] == "pytest_unavailable":
        return {"differences": None, "counts": None, "tool_versions": pins}
    key, known = junit_key_function(), {}
    reference = load_phase(inputs.reference_dir, "full", pins, key, known, problems)
    serial = load_phase(inputs.candidate_dir, "serial", pins, key, known, problems)
    parallel = load_phase(inputs.candidate_dir, "parallel", pins, key, known, problems)
    counts = {"reference": phase_counts(reference), "serial": phase_counts(serial), "parallel": phase_counts(parallel)}
    if None in (reference, serial, parallel):
        return {"differences": None, "counts": counts, "tool_versions": pins}
    return {"differences": compare(reference, serial, parallel), "counts": counts, "tool_versions": pins}


def write_json_atomic(path: Path, document: dict) -> None:
    """Write *document* to *path* through a temp file in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(document, fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def summary_markdown(verdict: dict) -> str:
    """The step summary: verdict, fingerprint, one row per difference kind, the first entries."""
    lines = [
        "### xdist parity",
        "",
        f"Not a required check. Verdict: **{verdict['verdict']}** (exit {verdict['exit']}, profile {verdict['profile']}).",
        "",
    ]
    if verdict["verdict"] == "not_applicable":
        return "\n".join(lines) + "\n"
    lines.append(f"Selection fingerprint: `{verdict.get('selection_fingerprint')}`")
    lines.append("")
    for problem in verdict["invalid_evidence"]:
        lines.append(f"- invalid evidence `{problem['reason']}`: {problem['detail']}")
    differences = verdict.get("differences") or {}
    if differences:
        lines += ["", "| difference | count |", "|---|---|"]
        lines += [f"| {kind} | {len(differences[kind])} |" for kind in DIFFERENCE_KINDS]
        lines.append("")
        for kind in DIFFERENCE_KINDS:
            for entry in differences[kind][:_SUMMARY_LIMIT]:
                lines.append(f"- `{kind}` {json.dumps(entry, sort_keys=True)}")
    if verdict["step_failures"]:
        lines += ["", "Steps that were not success: " + ", ".join(verdict["step_failures"])]
    return "\n".join(lines) + "\n"


# --- CLI ------------------------------------------------------------------------------------------


def _pairs(values: list, flag: str) -> dict:
    pairs = {}
    for value in values or []:
        name, sep, outcome = value.partition("=")
        if not sep or not name:
            raise SystemExit(f"check_xdist_parity: {flag} expects NAME=VALUE, got {value!r}")
        pairs[name] = outcome
    return pairs


def _cmd_compare(args) -> int:
    meta = {
        "head_sha": args.head_sha,
        "tested_sha": args.tested_sha,
        "run_id": args.run_id,
        "run_attempt": args.run_attempt,
        "event": args.event,
        "ref": args.ref,
    }
    inputs = CompareInputs(
        reference_dir=Path(args.reference),
        candidate_dir=Path(args.candidate),
        constraints=Path(args.constraints),
        args_dir=Path(args.args_dir) if args.args_dir else None,
        workspace_root=args.workspace_root,
        profile=args.profile,
        meta=meta,
        steps=_pairs(args.step, "--step"),
        upstream=_pairs(args.upstream, "--upstream"),
    )
    verdict = evaluate(inputs)
    write_json_atomic(Path(args.output), verdict)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(summary_markdown(verdict))
    print(f"xdist parity: {verdict['verdict']} (exit {verdict['exit']}), verdict at {args.output}")
    return 0 if verdict["exit"] is None else int(verdict["exit"])


def _cmd_fingerprint(args) -> int:
    try:
        print(fingerprint_of_args_dir(Path(args.args_dir), Path(args.constraints), args.workspace_root))
    except (OSError, ValueError) as exc:
        print(f"check_xdist_parity: cannot fingerprint {args.args_dir}: {exc}", file=sys.stderr)
        return 2
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    cmp = sub.add_parser("compare", help="compare the reference with the candidate and write the verdict")
    cmp.add_argument("--reference", required=True, help="directory with junit-full.xml and manifest-full.json")
    cmp.add_argument("--candidate", required=True, help="directory with the shadow's phase XMLs and manifests")
    cmp.add_argument("--constraints", required=True, help="scripts/ci/profile_a_constraints.txt")
    cmp.add_argument("--output", required=True, help=f"path of {VERDICT_FILE}")
    cmp.add_argument("--summary", default="", help="append a markdown summary here (GITHUB_STEP_SUMMARY)")
    cmp.add_argument("--args-dir", default="", help="this job's VNX_CI_XDIST=1 emission of the args script")
    cmp.add_argument("--workspace-root", default="", help="root stripped from the args before hashing")
    cmp.add_argument("--profile", default="", help="light or heavy, from the CI profile classifier")
    for name in ("head-sha", "tested-sha", "run-id", "run-attempt", "event", "ref"):
        cmp.add_argument(f"--{name}", default="")
    cmp.add_argument("--step", action="append", help="NAME=OUTCOME of a parity-job step (repeatable)")
    cmp.add_argument("--upstream", action="append", help="NAME=RESULT of a needed job (repeatable)")
    cmp.set_defaults(func=_cmd_compare)
    fp = sub.add_parser("fingerprint", help="print the selection fingerprint of an args directory")
    fp.add_argument("--args-dir", required=True)
    fp.add_argument("--constraints", required=True)
    fp.add_argument("--workspace-root", required=True)
    fp.set_defaults(func=_cmd_fingerprint)
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
