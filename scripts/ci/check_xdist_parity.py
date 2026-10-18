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
  for an active phase; a manifest field of the wrong type (also a nodeid that
  is not a string, and a collection outcome whose ``nodeid``, ``outcome``,
  ``reason`` or ``reports`` is missing or of the wrong type: such a phase is
  not compared); a key collision; a record whose key is in neither the
  manifest's nodeids nor its collection outcomes; workers that disagree; tool
  versions that differ from the constraints file.
* Stale evidence (exit 2, reason ``stale_evidence``): an input whose own
  ``run_id`` and ``run_attempt`` do not prove the run and attempt this job
  runs in. Artifacts upload with ``overwrite: true``, so a re-run that fails
  before its upload leaves the earlier attempt's artifact in the run, and
  download-artifact hands it over. Every input carries its stamp from where
  it is written: the manifests and ``result-full.json`` as fields, each JUnit
  XML as the testsuite properties ``vnx_run_id`` and ``vnx_run_attempt`` (the
  manifest plugin adds them in the run that writes the XML), each args
  emission as ``args/run.json``, the shadow record as fields. Nothing is read
  from a stale input: a stale phase is not compared, a stale shadow record
  gives no profile and no wall time, stale args give no fingerprint. The rule
  is ``stale_evidence_reason``.
* Exit 0 only when the candidate executed exactly the reference set with the
  same outcome classes.
* Step failures (``step_failures``, recorded in the verdict, never part of the
  exit): every parity-job step that is not success, Profile A's job result,
  phase result and pytest exit (an audit step can fail Profile A after its
  evidence was uploaded green), the shadow's record, verdict and job result.
  The verdict also records the blocks they are derived from (``steps``,
  ``reference``, ``shadow``). A light run is named by the same rule: its
  skipped steps and its unread evidence are in the list, so the list never
  claims more than the steps show. The comparison step itself (``parity``)
  cannot record its own outcome: ``record_xdist_parity_verdict.py`` adds it
  after this script exits (OI-2145).

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
or 0 when the run is not applicable (light profile); 2 with a reason on stderr,
never a traceback, when an argument is malformed or the verdict or its
summary cannot be written.
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
SHADOW_RECORD_FILE = "xdist-shadow.json"
SHADOW_WHAT = f"shadow record {SHADOW_RECORD_FILE}"
REFERENCE_RESULT_FILE = "result-full.json"
ARGS_STAMP_FILE = "run.json"
# testsuite property -> stamp field, written into the JUnit XML by pytest_collection_manifest.py
JUNIT_STAMP_PROPERTIES = {"vnx_run_id": "run_id", "vnx_run_attempt": "run_attempt"}
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
# The comparison step (id ``parity``) exits after writing the verdict, so only
# the record step after it can add its outcome; a recorded verdict carries all six.
COMPARE_STEP = "parity"
RECORDED_STEPS = PARITY_STEPS + (COMPARE_STEP,)
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


# --- current evidence: the one rule ------------------------------------------------------


def whole_number(value) -> int | None:
    """*value* when it is an int (a bool is not), else None."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def stamp_attempt(value) -> int | None:
    """A run_attempt as evidence carries it (a decimal string from CI, an int from gh); None when it names none."""
    if isinstance(value, str) and value.strip().isdecimal():
        value = int(value.strip())
    attempt = whole_number(value)
    return attempt if attempt is not None and attempt >= 1 else None


def stale_evidence_reason(what: str, stamp, run_id, run_attempt) -> str | None:
    """Why *what* is not evidence of run *run_id*, attempt *run_attempt*; None when its own stamp proves it is.

    *stamp* holds the ``run_id`` and ``run_attempt`` the evidence carries
    itself. Evidence counts only for the run and attempt it names: not for
    another run, not for another attempt of the same run, and not when it names
    none. A caller without a run to hold the evidence against proves nothing
    current either. The one rule for current evidence: the parity job applies
    it to every input it counts.
    """
    expected_id, expected_attempt = str(run_id or "").strip(), stamp_attempt(run_attempt)
    if not expected_id or expected_attempt is None:
        return f"no run to hold {what} against (run_id {run_id!r}, run_attempt {run_attempt!r})"
    stamp = stamp if isinstance(stamp, dict) else {}
    carried = stamp.get("run_id")
    if str(carried if carried is not None else "").strip() != expected_id:
        return f"{what} of run {carried}, not of run {expected_id}"
    attempt = stamp_attempt(stamp.get("run_attempt"))
    if attempt is None:
        return f"the {what} names no run_attempt"
    if attempt != expected_attempt:
        return f"{what} of attempt {attempt}, not of attempt {expected_attempt}"
    return None


def read_json_object(path: Path) -> dict | None:
    """The JSON object at *path*; None when it is absent, unreadable or not an object."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def junit_stamp(root) -> dict:
    """The run stamp a JUnit XML carries: its testsuite properties (a field written twice, differently, is None)."""
    values = {}
    for suite in root.iter("testsuite"):
        for prop in suite.findall("properties/property"):
            field_name = JUNIT_STAMP_PROPERTIES.get(prop.get("name") or "")
            if field_name is not None:
                values.setdefault(field_name, set()).add(prop.get("value"))
    return {name: next(iter(found)) if len(found) == 1 else None for name, found in values.items()}


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
    """A manifest whose nodeids and collection outcomes every comparison may read as they are; EvidenceError if not."""
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
    if not all(isinstance(outcome, dict) for outcome in manifest["collection_outcomes"]):
        raise EvidenceError("unparsable_manifest", f"{path}: a collection outcome is not an object")
    malformed = nodeid_problems(manifest["nodeids"]) + collection_outcome_problems(manifest["collection_outcomes"])
    if malformed:
        raise EvidenceError("malformed_manifest", f"{path}: {'; '.join(malformed[:_SUMMARY_LIMIT])}")
    return manifest


# field of a collection outcome (as pytest_collection_manifest.py writes it) -> the test its value must pass
COLLECTION_OUTCOME_FIELDS = (
    ("nodeid", lambda v: isinstance(v, str)),
    ("outcome", lambda v: isinstance(v, str) and v in ("failed", "skipped")),
    ("reason", lambda v: isinstance(v, str)),
    ("reports", lambda v: whole_number(v) is not None and v >= 1),
)


def nodeid_problems(nodeids: list) -> list:
    return [f"nodeid {index} {nodeid!r:.80} is not a string" for index, nodeid in enumerate(nodeids)
            if not isinstance(nodeid, str)]


def collection_outcome_problems(outcomes: list) -> list:
    """Every field of every collection outcome that is missing or of the wrong type."""
    problems = []
    for index, outcome in enumerate(outcomes):
        for name, valid in COLLECTION_OUTCOME_FIELDS:
            if name not in outcome:
                problems.append(f"collection outcome {index}: {name} is missing")
            elif not valid(outcome[name]):
                problems.append(f"collection outcome {index}: {name} {outcome[name]!r:.80} is not valid")
    return problems


def load_junit(path: Path):
    """The root element of a JUnit XML."""
    if not path.is_file():
        raise EvidenceError("missing_xml", str(path))
    if path.stat().st_size == 0:
        raise EvidenceError("empty_xml", str(path))
    try:
        return ET.parse(str(path)).getroot()
    except (OSError, ET.ParseError) as exc:
        raise EvidenceError("unparsable_xml", f"{path}: {exc}") from exc


def manifest_problems(manifest: dict, phase: str, pins: dict) -> list:
    """Invalid-evidence reasons a manifest carries by itself."""
    problems = []
    if manifest.get("phase") != phase:
        problems.append(("manifest_phase_mismatch", f"manifest of phase {phase} records phase {manifest.get('phase')!r}"))
    versions = manifest.get("tool_versions")
    if not isinstance(versions, dict):
        problems.append(("malformed_manifest", f"{phase}: tool_versions {versions!r} is not an object"))
        versions = {}
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
    block, workers = manifest.get("xdist"), whole_number(manifest.get("worker_count"))
    problems = []
    if not isinstance(block, dict):
        problems.append(("malformed_manifest", f"{phase}: the xdist block {block!r} is not an object"))
        block = {}
    if workers is None:
        problems.append(("malformed_manifest", f"{phase}: worker_count {manifest.get('worker_count')!r} is not a whole number"))
        return problems
    if block.get("workers_agree") is not True:
        problems.append(("workers_disagree", f"{phase}: the workers did not collect the same nodeids"))
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
        nodeid = outcome["nodeid"]
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


@dataclass
class PhaseReader:
    """What every phase is read with: the pins, the key function, the run it must prove, the keys seen so far."""

    pins: dict
    key: object
    run: tuple
    problems: list
    known: dict = field(default_factory=dict)

    def current(self, what: str, stamp) -> bool:
        """True when *stamp* proves this run and attempt; else a ``stale_evidence`` problem and False."""
        reason = stale_evidence_reason(what, stamp, *self.run)
        if reason is not None:
            self.problems.append(("stale_evidence", reason))
        return reason is None

    def load(self, directory: Path, name: str) -> Phase | None:
        """Read and check one phase; ``None`` (with the reason in *problems*) when unreadable or stale."""
        manifest_name, xml_name = f"manifest-{name}.json", f"junit-{name}.xml"
        try:
            manifest = load_manifest(directory / manifest_name)
        except EvidenceError as exc:
            self.problems.append((exc.reason, exc.detail))
            return None
        if not self.current(manifest_name, manifest):
            return None
        self.problems.extend(manifest_problems(manifest, name, self.pins))
        phase = Phase(name, manifest, bool(manifest.get("phase_active")), list(manifest["nodeids"]))
        index_keys(phase, self.key, self.known, self.problems)
        xml_path = directory / xml_name
        if not phase.active and not xml_path.exists():
            return phase
        try:
            root = load_junit(xml_path)
        except EvidenceError as exc:
            self.problems.append((exc.reason, exc.detail))
            return None
        if not self.current(xml_name, junit_stamp(root)):
            return None
        attach_records(phase, list(root.iter("testcase")), self.problems)
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
    ser_coll = {o["nodeid"] for o in serial.manifest["collection_outcomes"]}
    par_coll = {o["nodeid"] for o in parallel.manifest["collection_outcomes"]}
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
        form.setdefault(outcome["nodeid"], set()).add(outcome["outcome"])
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


def suite_wall_s(durations) -> int | None:
    """Parallel plus serial seconds of the shadow; None when either is there but not a whole number."""
    durations = durations if isinstance(durations, dict) else {}
    parallel, serial = whole_number(durations.get("parallel")), durations.get("serial")
    if parallel is None or (serial is not None and whole_number(serial) is None):
        return None
    return parallel + (serial or 0)


def shadow_block(record: dict | None, job_result: str | None, stale: str | None) -> dict:
    """The shadow as the verdict shows it; a stale record shows only whose it is, nothing it says."""
    if record is None:
        return {"record": "missing", "job_result": job_result}
    if stale is not None:
        return {"record": "stale", "job_result": job_result, "stale": stale,
                "run_id": record.get("run_id"), "run_attempt": record.get("run_attempt")}
    return {
        "record": "present",
        "job_result": job_result,
        "verdict": record.get("verdict"),
        "run_id": record.get("run_id"),
        "run_attempt": record.get("run_attempt"),
        "steps": record.get("steps"),
        "exit_codes": record.get("exit_codes"),
        "durations_s": record.get("durations_s"),
        "suite_wall_s": suite_wall_s(record.get("durations_s")),
    }


def reference_block(inputs: CompareInputs, run: tuple) -> tuple:
    """(Profile A's job result and phase result as the verdict shows them, the result's stale reason or None)."""
    block = {"job_result": inputs.upstream.get("profile_a"), "result": "missing", "exit": None, "duration_s": None}
    record = read_json_object(inputs.reference_dir / REFERENCE_RESULT_FILE)
    if record is None:
        return block, None
    stale = stale_evidence_reason(REFERENCE_RESULT_FILE, record, *run)
    if stale is not None:
        return {**block, "result": "stale"}, stale
    return {**block, "result": "present", "exit": whole_number(record.get("exit")),
            "duration_s": whole_number(record.get("duration_s"))}, None


def step_failures(steps: dict, shadow: dict, reference: dict, names: tuple = PARITY_STEPS) -> list:
    """Every parity-job step, reference and shadow outcome that is not success, as ``where.what=outcome``.

    *steps*, *shadow* and *reference* are the verdict's own blocks. The
    reference is Profile A: its job result (an audit can fail it after its
    evidence was uploaded green), its phase result and that result's pytest
    exit. The verdict records this list next to the blocks it is derived from.
    *names* are the steps that must be success: this script's own
    ``PARITY_STEPS``; a reader of a recorded verdict passes ``RECORDED_STEPS``,
    so a verdict without the comparison step's outcome is not clean.
    """
    failures = [f"parity.{name}={steps.get(name) or 'unknown'}" for name in names if steps.get(name) != "success"]
    if reference.get("job_result") != "success":
        failures.append(f"reference.job={reference.get('job_result') or 'unknown'}")
    if reference.get("result") != "present":
        failures.append(f"reference.result={reference.get('result')}")
    elif whole_number(reference.get("exit")) != 0:
        failures.append(f"reference.exit={reference.get('exit')}")
    if shadow.get("record") != "present":
        failures.append(f"shadow.record={shadow.get('record')}")
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


def run_of(inputs: CompareInputs) -> tuple:
    """(run_id, run_attempt) of the run this job runs in: what every input must prove."""
    return inputs.meta.get("run_id"), inputs.meta.get("run_attempt")


def resolve_profile(inputs: CompareInputs, shadow: dict | None) -> str:
    """The parity job's own classification; a current shadow record's when the parity prelude gave none."""
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
    """(fingerprint, source): this job's own emission first, the shadow's emitted args second.

    An emission counts only when its ``args/run.json`` proves this run and
    attempt; one without args is no emission, one with stale args is a
    ``stale_evidence`` problem and gives no fingerprint.
    """
    constraints = inputs.constraints
    fingerprints = {}
    sources = (("parity_job", "parity-job", inputs.args_dir), ("shadow", "shadow", inputs.candidate_dir / "args"))
    for source, label, directory in sources:
        if directory is None:
            continue
        try:
            fingerprint = fingerprint_of_args_dir(directory, constraints, inputs.workspace_root)
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        stale = stale_evidence_reason(f"{label} args/{ARGS_STAMP_FILE}", read_json_object(directory / ARGS_STAMP_FILE),
                                      *run_of(inputs))
        if stale is not None:
            problems.append(("stale_evidence", stale))
            continue
        fingerprints[source] = fingerprint
    if len(set(fingerprints.values())) > 1:
        problems.append(("fingerprint_mismatch", f"the shadow ran another selection than this commit emits: {fingerprints}"))
    for source in ("parity_job", "shadow"):
        if source in fingerprints:
            return fingerprints[source], source
    problems.append(("fingerprint_unavailable", "neither this job nor the shadow left emitted args"))
    return None, None


def evaluate(inputs: CompareInputs) -> dict:
    """The full verdict for one run (also when the evidence is unusable)."""
    run = run_of(inputs)
    shadow_record = read_json_object(inputs.candidate_dir / SHADOW_RECORD_FILE)
    shadow_stale = None if shadow_record is None else stale_evidence_reason(SHADOW_WHAT, shadow_record, *run)
    profile = resolve_profile(inputs, None if shadow_stale else shadow_record)
    verdict = {"schema": VERDICT_SCHEMA, **inputs.meta, "profile": profile, "steps": inputs.steps}
    verdict["shadow"] = shadow_block(shadow_record, inputs.upstream.get("shadow"), shadow_stale)
    verdict["reference"], reference_stale = reference_block(inputs, run)
    if profile == "light":
        verdict.update(exit=None, verdict="not_applicable", invalid_evidence=[], differences=None, counts=None,
                       tool_versions=None, selection_fingerprint=None, selection_fingerprint_source=None,
                       step_failures=step_failures(inputs.steps, verdict["shadow"], verdict["reference"]))
        return verdict
    problems = [("download_failed", f"{name}={inputs.steps[name]}")
                for name in ("download_reference", "download_candidate")
                if name in inputs.steps and inputs.steps[name] != "success"]
    problems += [("stale_evidence", reason) for reason in (shadow_stale, reference_stale) if reason is not None]
    verdict["selection_fingerprint"], verdict["selection_fingerprint_source"] = fingerprint_block(inputs, problems)
    verdict.update(_compare_evidence(inputs, problems))
    verdict["invalid_evidence"] = [{"reason": r, "detail": d} for r, d in problems]
    has_differences = any(verdict["differences"].values()) if verdict["differences"] else False
    verdict["exit"] = 2 if problems else (1 if has_differences else 0)
    verdict["verdict"] = {0: "parity", 1: "differences", 2: "invalid_evidence"}[verdict["exit"]]
    verdict["step_failures"] = step_failures(inputs.steps, verdict["shadow"], verdict["reference"])
    return verdict


def _compare_evidence(inputs: CompareInputs, problems: list) -> dict:
    try:
        pins = read_pins(inputs.constraints)
    except (OSError, ValueError) as exc:
        problems.append(("unreadable_constraints", f"{inputs.constraints}: {exc}"))
        return {"differences": None, "counts": None, "tool_versions": None}
    own = own_tool_problems(pins)
    problems.extend(own)
    if own and own[0][0] == "pytest_unavailable":
        return {"differences": None, "counts": None, "tool_versions": pins}
    reader = PhaseReader(pins, junit_key_function(), run_of(inputs), problems)
    reference = reader.load(inputs.reference_dir, "full")
    serial = reader.load(inputs.candidate_dir, "serial")
    parallel = reader.load(inputs.candidate_dir, "parallel")
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


def _pair(value: str) -> tuple:
    """NAME=VALUE of ``--step`` and ``--upstream``; argparse turns the error into its usage exit 2."""
    name, sep, outcome = value.partition("=")
    if not sep or not name:
        raise argparse.ArgumentTypeError(f"expects NAME=VALUE, got {value!r}")
    return name, outcome


def _write_outputs(verdict: dict, output: str, summary: str) -> str | None:
    """Write the verdict, then append its summary; why not, or None when both are written."""
    try:
        write_json_atomic(Path(output), verdict)
    except OSError as exc:
        return f"cannot write the verdict to {output}: {exc}"
    if not summary:
        return None
    try:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(summary_markdown(verdict))
    except OSError as exc:
        return f"cannot append the summary to {summary}: {exc}"
    return None


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
        steps=dict(args.step or []),
        upstream=dict(args.upstream or []),
    )
    verdict = evaluate(inputs)
    problem = _write_outputs(verdict, args.output, args.summary)
    if problem is not None:
        print(f"check_xdist_parity: {problem}", file=sys.stderr)
        return 2
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
    cmp.add_argument("--step", action="append", type=_pair, help="NAME=OUTCOME of a parity-job step (repeatable)")
    cmp.add_argument("--upstream", action="append", type=_pair, help="NAME=RESULT of a needed job (repeatable)")
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
