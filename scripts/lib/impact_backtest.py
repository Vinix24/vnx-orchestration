"""Impact backtest: S rebuilt for old PR runs from their Profile A evidence (ADR-074, PR 4b).

The live shadow only has red runs from the day it started.  The backtest takes the completed
``VNX CI`` pull_request runs, downloads their ``profile-a-evidence`` artifact (JUnit XML plus
manifest, kept 90 days), rebuilds the five inputs of the selection from git (candidate, base,
paths, selector version, graph version), runs TODAY's selector over the old tree and writes one
``impact_ledger/1`` line per attempt to ``state/impact_backtest.ndjson``.  The live ledger stays
CI evidence only.

The candidate is the PR head (``candidate_kind: "pr_head"``); live CI uses the merge commit.
The base is the merge-base of the head and ``origin/main`` as it stood when the run was created
(last first-parent commit before ``createdAt``), never of today's main: main has absorbed many of
these heads since, and against today's main such a head is its own base with zero paths.
An input that cannot be read makes the line ``reconstructible: false`` and names the input,
never a guess.  An absent artifact is a line with ``missing``.  Any other ``gh`` failure aborts
the attempt without a line, so it is tried again next time.  Only runs that concluded
``success`` or ``failure`` are read.
"""

from __future__ import annotations

import re
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import impact_ledger as ledger



def _ci_modules() -> tuple:
    """The selector and the shadow record builder of scripts/ci/ (their single definition)."""
    ci_dir = str(Path(__file__).resolve().parent.parent / "ci")
    if ci_dir not in sys.path:
        sys.path.insert(0, ci_dir)
    import impact_select
    import impact_shadow
    return impact_select, impact_shadow


impact_select, impact_shadow = _ci_modules()

LEDGER_NAME = "impact_backtest.ndjson"
EVIDENCE_ARTIFACT = "profile-a-evidence"
CANDIDATE_KIND = "pr_head"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SHA = re.compile(r"[0-9a-f]{40}")


def default_git(argv: list) -> str:
    return ledger.default_runner(argv, cwd=REPO_ROOT)


def backtest_path(state_dir: Path) -> Path:
    return Path(state_dir) / LEDGER_NAME


def _regular_only(member: tarfile.TarInfo, dest: str) -> Optional[tarfile.TarInfo]:
    """Files and directories only, through the ``data`` filter; links are not part of the graph."""
    return tarfile.data_filter(member, dest) if member.isreg() or member.isdir() else None


def _unpack_tree(git: ledger.Runner, sha: str, tmp: Path) -> Path:
    archive, tree = tmp / "tree.tar", tmp / "tree"
    git(["git", "archive", "-o", str(archive), sha])
    tree.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(tree, filter=_regular_only)
    archive.unlink()
    return tree


def _main_at_run(git: ledger.Runner, created_at) -> Optional[str]:
    """The last first-parent commit of ``origin/main`` at the creation time of the run, else None."""
    created = ledger.parse_time(created_at)
    if created is None:
        return None
    try:
        found = git(["git", "rev-list", "-1", "--first-parent", f"--before={created.isoformat()}", "origin/main"]).strip()
    except ledger.RunnerError:
        return None
    return found if _SHA.fullmatch(found) else None


def _base(git: ledger.Runner, sha: str, created_at) -> Optional[str]:
    """Merge-base of the head and main as it was at ``created_at``; None when any step is unreadable."""
    main_at_run = _main_at_run(git, created_at)
    if main_at_run is None:
        return None
    try:
        return git(["git", "merge-base", main_at_run, sha]).strip() or None
    except ledger.RunnerError:
        return None


def _git_inputs(git: ledger.Runner, sha, created_at, inputs: dict) -> Optional[str]:
    """Fill candidate, base and paths; the name of the first input that cannot be read, else None."""
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        return "candidate"
    inputs["candidate"] = sha
    try:
        git(["git", "fetch", "--no-tags", "origin", sha])
    except ledger.RunnerError:
        return "candidate"
    inputs["base"] = _base(git, sha, created_at)
    if not inputs["base"]:
        return "base"
    if inputs["base"] == sha:
        return "paths"  # a pull request run always changes a path; a head that is its own base has no diff to read
    try:
        inputs["paths"] = [p for p in git(["git", "diff", "--name-only", inputs["base"], sha]).splitlines() if p.strip()]
    except ledger.RunnerError:
        return "paths"
    if not inputs["paths"]:
        inputs["paths"] = None
        return "paths"
    return None


def _record(git: ledger.Runner, run: dict, evidence_dir: Path, tmp: Path, inputs: dict):
    """``(record, None)`` or ``(None, name of the input that cannot be read)``."""
    unreadable = _git_inputs(git, run.get("headSha"), run.get("createdAt"), inputs)
    if unreadable:
        return None, unreadable
    try:
        tree = _unpack_tree(git, inputs["candidate"], tmp)
    except (ledger.RunnerError, tarfile.TarError, OSError):
        return None, "tree"
    ci = tree / "scripts" / "ci"
    try:
        exclusions, known = impact_shadow.known_failure_files(ci / "test_exclusions.txt", ci / "xdist_expected_skips.txt")
    except (OSError, ValueError):
        return None, "exclusions"
    try:
        selection = impact_select.select(tree, inputs["paths"])
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return None, "selection"
    inputs["selector_version"], inputs["graph_version"] = selection.get("selector_version"), selection.get("graph_version")
    run_key = (str(run["databaseId"]), str(run["attempt"]))
    identity = {"run_id": run_key[0], "run_attempt": run_key[1], "base_sha": inputs["base"],
                "candidate_sha": inputs["candidate"], "changed_paths": inputs["paths"]}
    try:
        return impact_shadow.build_record(selection, impact_shadow.read_evidence(evidence_dir, run_key), exclusions, known, identity), None
    except (KeyError, ValueError, TypeError):
        return None, "selection"


def _entry(gh: ledger.Runner, git: ledger.Runner, repo: str, run: dict, now: datetime) -> dict:
    run_id, attempt = run["databaseId"], run["attempt"]
    inputs = {"candidate": None, "base": None, "paths": None, "selector_version": None, "graph_version": None}
    entry = {"schema": ledger.SCHEMA, "source": "backtest", "candidate_kind": CANDIDATE_KIND, "run_id": run_id,
             "run_attempt": attempt, "head_sha": run.get("headSha"), "branch": run.get("headBranch"),
             "event": run.get("event"), "run_conclusion": run.get("conclusion"), "created_at": run.get("createdAt"),
             "collected_at": now.isoformat(), "inputs": inputs, "shadow": None, "job": None, "profile_a_job": None,
             "shadow_job": None, "missing": []}
    with tempfile.TemporaryDirectory(prefix="impact-backtest-") as tmp:
        evidence = Path(tmp) / "evidence"
        evidence.mkdir()
        try:
            gh(["gh", "run", "download", str(run_id), "-n", EVIDENCE_ARTIFACT, "-D", str(evidence)])
        except ledger.RunnerError as exc:
            if "no valid artifact" not in exc.stderr.lower():
                raise
            entry["missing"].append(f"artifact:{EVIDENCE_ARTIFACT}")
            return entry
        entry["profile_a_job"] = ledger.timing(ledger.fetch_jobs(gh, repo, run_id, attempt), ledger.PROFILE_A_JOB)
        if entry["profile_a_job"] is None:
            entry["missing"].append("job:profile-a")
        record, unreadable = _record(git, run, evidence, Path(tmp), inputs)
    entry["shadow"] = record or {"schema": impact_shadow.SCHEMA, "reconstructible": False, "unreadable_inputs": [unreadable]}
    return entry


def backtest(state_dir: Path, gh: ledger.Runner = ledger.default_runner, git: ledger.Runner = default_git,
             since: Optional[datetime] = None, limit: int = 50, now: Optional[datetime] = None) -> dict:
    """Append the attempts in neither ledger; returns ``{"added": [...], "skipped": [...], "failed": [...]}``."""
    now = now or datetime.now(timezone.utc)
    path = backtest_path(state_dir)
    result = {"added": [], "skipped": [], "failed": []}
    try:
        git(["git", "fetch", "--no-tags", "origin", "main"])
        repo, runs = ledger.discover(gh, limit, event="pull_request")
    except (ledger.RunnerError, ValueError, KeyError, TypeError) as exc:
        result["failed"].append({"attempt": "discovery", "error": str(exc)})
        return result
    stored = {ledger.entry_key(e) for p in (ledger.ledger_path(state_dir), path) for e in ledger.read_ledger(p)}
    for run in runs:
        name = f"{run.get('databaseId')}/{run.get('attempt')}"
        created = ledger.parse_time(run.get("createdAt"))
        if (since and (created is None or created < since)) or run.get("conclusion") not in ledger.DONE_CONCLUSIONS:
            continue
        if (run.get("databaseId"), run.get("attempt")) in stored:
            result["skipped"].append(name)
            continue
        try:
            written = ledger.append_entry(path, _entry(gh, git, repo, run, now))
        except (ledger.RunnerError, ValueError, KeyError, TypeError) as exc:
            result["failed"].append({"attempt": name, "error": str(exc)})
            continue
        result["added" if written else "skipped"].append(name)
    return result
