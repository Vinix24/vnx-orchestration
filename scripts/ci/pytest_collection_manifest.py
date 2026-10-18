#!/usr/bin/env python3
"""Collection manifest: what one pytest run collected, written by that same run.

A pytest plugin for CI Profile A and its xdist shadow (track
``ci-profile-a-xdist``). Loaded with ``-p pytest_collection_manifest`` (the
caller puts ``scripts/ci`` on ``PYTHONPATH``), it writes one JSON file per run
phase from the same run that writes the JUnit XML:

* ``nodeids`` — the collected test nodeids after deselection, in collection
  order;
* ``collection_outcomes`` — every collection report that did not pass (a file
  skipped at collection, such as a module-level ``pytest.importorskip``, or a
  file erroring at collection), keyed by the report's nodeid (the file);
* the phase, whether it was active, the versions of the pinned tools, and the
  worker count;
* ``run_id`` and ``run_attempt``, the run stamp: ``GITHUB_RUN_ID`` and
  ``GITHUB_RUN_ATTEMPT`` as GitHub Actions sets them in every step, ``null``
  outside CI. The same run's JUnit XML gets the stamp as the testsuite
  properties ``vnx_run_id`` and ``vnx_run_attempt``. The parity check
  (``check_xdist_parity.py``) counts a manifest or an XML only for the run and
  attempt its own stamp names, so an earlier attempt's artifact that a failed
  re-run left in place is stale evidence, not this attempt's.

A separate ``pytest --collect-only`` cannot give this: it disables xdist, and a
module-level skip leaves no nodeid in its output.

Where the data comes from:

* serial run (no xdist, or ``-p no:xdist``): ``pytest_collection_finish``
  for the nodeids and ``pytest_collectreport`` for the outcomes;
* xdist controller: the controller does not collect. Nodeids come from
  ``pytest_xdist_node_collection_finished`` (one list per worker; the manifest
  keeps the first worker's list and a count and sha256 per worker). Outcomes
  come from the collection reports xdist forwards to the controller's
  ``pytest_collectreport``. Measured on pytest 9.1.1 + pytest-xdist 3.8.0
  (2026-10-16, ``-n 2``): xdist forwards every report that did not pass. A
  module-level skip arrives once (xdist deduplicates it by its longrepr
  tuple); a collection error arrives once per worker. The manifest keeps one
  entry per (nodeid, outcome) with the number of reports received;
* xdist worker: writes nothing.

The module is also a small CLI for the callers that cannot run the plugin:
``inactive`` writes the manifest of a phase that did not run (an empty serial
list), and ``nodeids`` prints the nodeids of a manifest, one per line, for
``scripts/ci/profile_a_pytest_args.sh``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from importlib import metadata
from pathlib import Path

import pytest
from _pytest.junitxml import xml_key

SCHEMA = "vnx-collection-manifest/1"
PINNED_TOOLS = ("pytest", "pytest-xdist", "pytest-timeout")
_PLUGIN_NAME = "vnx-collection-manifest-recorder"


def run_stamp() -> dict:
    """The run and attempt this process runs in, from the GitHub Actions environment; None outside CI."""
    return {"run_id": os.environ.get("GITHUB_RUN_ID") or None,
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT") or None}


def stamp_junit_xml(config, stamp: dict) -> None:
    """Give the JUnit XML this run writes (if any) the run stamp, as testsuite properties ``vnx_<field>``."""
    xml = config.stash.get(xml_key, None)
    if xml is None:
        return
    for name, value in stamp.items():
        if value is not None:
            xml.add_global_property(f"vnx_{name}", value)


def tool_versions() -> dict:
    """Installed version of each pinned tool, ``None`` when it is absent."""
    versions = {}
    for dist in PINNED_TOOLS:
        try:
            versions[dist] = metadata.version(dist)
        except metadata.PackageNotFoundError:
            versions[dist] = None
    return versions


def write_manifest(path: Path, manifest: dict) -> None:
    """Write *manifest* to *path* atomically (temp file in the same dir)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=1)
            fh.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def inactive_manifest(phase: str) -> dict:
    """Manifest of a phase that did not run; it needs no JUnit XML."""
    return {
        "schema": SCHEMA,
        "phase": phase,
        "phase_active": False,
        **run_stamp(),
        "tool_versions": tool_versions(),
        "worker_count": 0,
        "nodeid_count": 0,
        "nodeids": [],
        "collection_outcomes": [],
    }


def _outcome_reason(report) -> str:
    """Short reason of a collection report that did not pass."""
    longrepr = report.longrepr
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2])
    lines = [line.strip() for line in str(longrepr).splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _sha256_lines(items) -> str:
    return hashlib.sha256("\n".join(items).encode("utf-8")).hexdigest()


class ManifestRecorder:
    """Collects one phase's manifest; registered on the serial or controller process."""

    def __init__(self, config, path: Path, phase: str) -> None:
        self._config = config
        self._path = path
        self._phase = phase
        self._stamp = run_stamp()
        self._session_nodeids = None
        self._worker_nodeids = {}
        self._outcomes = {}

    def pytest_sessionstart(self, session) -> None:
        # After every pytest_configure, so the junitxml plugin has set up its writer.
        stamp_junit_xml(self._config, self._stamp)

    def pytest_collectreport(self, report) -> None:
        if report.passed:
            return
        key = (report.nodeid, report.outcome)
        entry = self._outcomes.get(key)
        if entry is None:
            self._outcomes[key] = {
                "nodeid": report.nodeid,
                "outcome": report.outcome,
                "reason": _outcome_reason(report),
                "reports": 1,
            }
        else:
            entry["reports"] += 1

    def pytest_collection_finish(self, session) -> None:
        self._session_nodeids = [item.nodeid for item in session.items]

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node, ids) -> None:
        self._worker_nodeids[node.gateway.id] = list(ids)

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus) -> None:
        write_manifest(self._path, self.build(int(exitstatus)))

    def build(self, exitstatus: int) -> dict:
        xdist_active = self._config.pluginmanager.has_plugin("dsession")
        nodeids, xdist_block = self._nodeids_and_xdist(xdist_active)
        return {
            "schema": SCHEMA,
            "phase": self._phase,
            "phase_active": True,
            **self._stamp,
            "mode": "xdist" if xdist_active else "serial",
            "tool_versions": tool_versions(),
            "worker_count": self._worker_count(xdist_active),
            "xdist": xdist_block,
            "collection_finished": nodeids is not None,
            "exitstatus": exitstatus,
            "nodeid_count": len(nodeids or []),
            "nodeids": nodeids or [],
            "collection_outcomes": list(self._outcomes.values()),
        }

    def _worker_count(self, xdist_active: bool) -> int:
        if not xdist_active:
            return 0
        return int(self._config.getoption("numprocesses", default=0) or 0)

    def _nodeids_and_xdist(self, xdist_active: bool):
        if not xdist_active:
            return self._session_nodeids, None
        workers = {
            gw: {"nodeid_count": len(ids), "nodeids_sha256": _sha256_lines(ids)}
            for gw, ids in sorted(self._worker_nodeids.items())
        }
        hashes = {w["nodeids_sha256"] for w in workers.values()}
        first = sorted(self._worker_nodeids)[0] if self._worker_nodeids else None
        block = {
            "dist": self._config.getoption("dist", default=None),
            "workers_reported": len(workers),
            "workers_agree": len(hashes) == 1,
            "workers": workers,
        }
        return (self._worker_nodeids[first] if first else None), block


def pytest_addoption(parser) -> None:
    group = parser.getgroup("collection-manifest", "VNX CI collection manifest")
    group.addoption(
        "--collection-manifest",
        default=None,
        metavar="PATH",
        help="Write this run's collection manifest (JSON) to PATH.",
    )
    group.addoption(
        "--collection-manifest-phase",
        default=None,
        metavar="NAME",
        help="Phase name recorded in the manifest (required with --collection-manifest).",
    )


def pytest_configure(config) -> None:
    path = config.getoption("collection_manifest")
    if not path or hasattr(config, "workerinput"):
        return
    phase = config.getoption("collection_manifest_phase")
    if not phase:
        raise pytest.UsageError("--collection-manifest-phase is required with --collection-manifest")
    config.pluginmanager.register(ManifestRecorder(config, Path(path), phase), _PLUGIN_NAME)


def _cmd_inactive(args) -> int:
    write_manifest(Path(args.output), inactive_manifest(args.phase))
    return 0


def _cmd_nodeids(args) -> int:
    try:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        nodeids = manifest["nodeids"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"pytest_collection_manifest: unreadable manifest {args.manifest}: {exc}", file=sys.stderr)
        return 2
    for nodeid in nodeids:
        print(nodeid)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    inactive = sub.add_parser("inactive", help="write the manifest of a phase that did not run")
    inactive.add_argument("--phase", required=True)
    inactive.add_argument("--output", required=True)
    inactive.set_defaults(func=_cmd_inactive)
    nodeids = sub.add_parser("nodeids", help="print a manifest's nodeids, one per line")
    nodeids.add_argument("manifest")
    nodeids.set_defaults(func=_cmd_nodeids)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
