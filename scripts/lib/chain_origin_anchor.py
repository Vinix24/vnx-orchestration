"""ADR-034: external git-committed chain-origin anchor.

Anchors a hash-chain ledger's per-epoch fingerprint as a git-committed record
in the governed project's OWN source repo (``governance/chain-origin.ndjson``),
read back only from git-committed history — never the working-tree copy. This
is what makes ``verify_chain`` fail CLOSED against a local actor who can strip
and re-chain the ledger: the anchor lives outside the trust boundary the
ledger itself is inside.

Full design: docs/governance/decisions/ADR-034-external-chain-origin-anchor.md
Three prior attempts (#1085, #1086, #1171) anchored the origin somewhere the
same actor who edits the ledger can also edit; all three were HELD by codex
review for that reason. This ADR moves the anchor's source of truth outside
that boundary. Implement literally — do not redesign.

``VNX_CHAIN_RECEIPTS`` stays default-OFF. Nothing in this module runs unless a
caller explicitly invokes it; landing this file changes no runtime behavior.
"""
from __future__ import annotations

import json
import subprocess
import sys as _sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in _sys.path:
    _sys.path.insert(0, str(_LIB_DIR))

from gh_pr_ensure import ensure_pr  # noqa: E402  (thin PR-creation helper, reused not reinvented)
from ndjson_hash_chain import (  # noqa: E402
    EPOCH_MARKER_TYPE,
    _append_epoch_marker_locked,
    _ledger_locked,
    compute_entry_hash,
    epoch_state,
    walk_chain,
)
from ndjson_hash_chain import verify_chain as _base_verify_chain  # noqa: E402


ANCHOR_REL_PATH = Path("governance/chain-origin.ndjson")  # NOT under docs/ — see ADR §3 placement note


class BranchProtectionUnconfirmedError(RuntimeError):
    """Raised by seal_and_commit_origin when branch protection isn't confirmed active.

    ADR-034 §6 step 2b is a hard activation precondition, not best-effort: the
    caller must independently confirm (``gh api
    repos/:owner/:repo/branches/:branch/protection``) that the anchor-
    immutability check is a required status check with enforce_admins on
    BEFORE calling seal_and_commit_origin. That gh-api check itself is PR-2
    scope (out of scope here) — this library only refuses to proceed without
    an explicit caller-supplied confirmation.
    """


# ---------------------------------------------------------------------------
# Data types (ADR §7 lists field shapes, not concrete types — dataclasses
# chosen here for a stable, typed, JSON-serializable contract)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OriginFingerprint:
    """An epoch's OPENING fingerprint — computed at its chain_epoch_start marker."""

    epoch: int
    origin_type: str
    origin_hash: str
    origin_line_number: int
    entries_before_origin: int


@dataclass(frozen=True)
class ClosureFingerprint:
    """An epoch's CLOSURE commitment — its last entry, at the moment the next epoch opens."""

    epoch: int
    closure_hash: str
    closure_line_number: int
    entries_in_epoch: int


@dataclass(frozen=True)
class AnchorRecord:
    """One parsed line from the git-committed ``governance/chain-origin.ndjson``."""

    key: str
    ledger_identity: str
    epoch: int
    record_type: str  # "open" | "close"
    origin_type: str | None = None
    origin_hash: str | None = None
    origin_line_number: int | None = None
    entries_before_origin: int | None = None
    closure_hash: str | None = None
    closure_line_number: int | None = None
    entries_in_epoch: int | None = None
    sealed_at: str | None = None


@dataclass(frozen=True)
class AnchorProvenance:
    """What was resolved and from where — populated even on a None/"corrupt" record
    result, so a caller can always show what was checked (ADR §2 off-host cross-check)."""

    ref: str
    resolved: bool
    anchor_commit_sha: str | None = None
    remote_url: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class SealResult:
    action: str  # "sealed" | "noop" | "resumed"
    epoch: int
    origin: OriginFingerprint | None
    branch_name: str | None
    pr_url: str | None
    closed_epoch: int | None


# ---------------------------------------------------------------------------
# Keys (ADR §1, §7)
# ---------------------------------------------------------------------------


def ledger_identity(project_id: str, ledger_path: Path, project_data_dir: Path) -> str:
    """Stable logical key: '{project_id}:{ledger_path relative to project_data_dir}'.

    Never an absolute local filesystem path (ADR §1) — combined with an epoch
    number to form the actual anchor-file key (anchor_key/closure_key).
    """
    rel = ledger_path.resolve().relative_to(project_data_dir.resolve())
    return f"{project_id}:{rel.as_posix()}"


def anchor_key(identity: str, epoch: int) -> str:
    """'{identity}#{epoch}' — one committed record per SEALED EPOCH (ADR §1 R4 fix)."""
    return f"{identity}#{epoch}"


def closure_key(identity: str, epoch: int) -> str:
    """'{anchor_key(identity, epoch)}:close' — the sibling key for epoch's CLOSURE
    commitment (ADR §1 C6). A brand-new key distinct from anchor_key, so §3's
    "only new keys may be appended" CI rule covers it with no change."""
    return f"{anchor_key(identity, epoch)}:close"


# ---------------------------------------------------------------------------
# Pure fingerprint computation over the ledger tail (ADR §7)
# ---------------------------------------------------------------------------


def _find_epoch_marker_line(ledger_path: Path, epoch: int) -> tuple[int, dict]:
    for line_no, entry, _hash in walk_chain(ledger_path):
        if entry.get("type") == EPOCH_MARKER_TYPE and _coerce_epoch(entry) == epoch:
            return line_no, entry
    raise ValueError(f"no chain_epoch_start marker for epoch {epoch} in {ledger_path}")


def _coerce_epoch(entry: dict) -> int | None:
    try:
        return int(entry.get("epoch", -1))
    except (TypeError, ValueError):
        return None


def compute_epoch_fingerprint(ledger_path: Path, epoch: int) -> OriginFingerprint:
    """Pure computation over the CURRENT tail of ledger_path for the given sealed
    epoch. Caller must hold ledger_lock_path(ledger_path)'s flock when calling
    this for a WRITE (seal_and_commit_origin) — a read-only verify call is a
    best-effort snapshot, same as the existing unlocked verify_chain walk."""
    marker_line, marker_entry = _find_epoch_marker_line(ledger_path, epoch)
    origin_hash = compute_entry_hash(marker_entry)
    entries_before = sum(1 for line_no, _e, _h in walk_chain(ledger_path) if line_no < marker_line)
    return OriginFingerprint(
        epoch=epoch,
        origin_type=EPOCH_MARKER_TYPE,
        origin_hash=origin_hash,
        origin_line_number=marker_line,
        entries_before_origin=entries_before,
    )


def compute_epoch_closure(
    ledger_path: Path, epoch: int, *, next_epoch_marker_line: int
) -> ClosureFingerprint:
    """Pure computation of epoch's CLOSURE commitment (ADR §1 C6): the hash and
    absolute line number of epoch's last entry — the line immediately before
    next_epoch_marker_line — plus the entry count that fell within epoch.

    A zero-entry epoch (force_new_epoch called with nothing appended since
    the epoch opened — a legitimate "shrink the residual window" pattern,
    ADR §1) has no entry of its own to close on; its closure fingerprint
    degenerates to the epoch's OWN opening marker (closure_hash ==
    origin_hash, closure_line_number == origin_line_number,
    entries_in_epoch == 0), which is well-defined and still detects any
    tamper of the marker itself.
    """
    marker_line, marker_entry = _find_epoch_marker_line(ledger_path, epoch)
    last_line_no = marker_line
    last_hash = compute_entry_hash(marker_entry)
    count = 0
    for line_no, _entry, hash_ in walk_chain(ledger_path):
        if marker_line < line_no < next_epoch_marker_line:
            count += 1
            last_line_no = line_no
            last_hash = hash_
    return ClosureFingerprint(
        epoch=epoch,
        closure_hash=last_hash,
        closure_line_number=last_line_no,
        entries_in_epoch=count,
    )


def _observed_epochs(ledger_path: Path) -> list[tuple[int, int]]:
    """Every (epoch, marker_line) observed in the ledger walk, sorted by line."""
    epochs: list[tuple[int, int]] = []
    for line_no, entry, _hash in walk_chain(ledger_path):
        if entry.get("type") == EPOCH_MARKER_TYPE:
            epoch = _coerce_epoch(entry)
            if epoch is not None:
                epochs.append((epoch, line_no))
    epochs.sort(key=lambda pair: pair[1])
    return epochs


# ---------------------------------------------------------------------------
# Git-committed-history reads (ADR §2, §3) — NEVER the working tree
# ---------------------------------------------------------------------------


def _git_run(project_root: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(project_root), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _git_run_checked(project_root: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    proc = _git_run(project_root, *args, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {project_root}: {(proc.stderr or '').strip()[:500]}"
        )
    return proc


def _resolve_remote_name(ref: str) -> str:
    return ref.split("/", 1)[0] if "/" in ref else "origin"


def _qualify_remote_ref(project_root: Path, ref: str) -> str | None:
    """Rewrite a `<remote>/<branch>` ref into its EXPLICIT `refs/remotes/<remote>/<branch>`
    form so `git rev-parse`/`git show` can never resolve it against a
    same-named LOCAL branch instead.

    Per gitrevisions(7)'s disambiguation order, an unqualified `<refname>`
    checks `refs/heads/<refname>` BEFORE `refs/remotes/<refname>` — a local
    attacker who creates a branch literally named e.g. `origin/main` (git
    allows slashes in branch names) gets it trusted over the real
    remote-tracking ref unless callers qualify explicitly (Finding 5,
    ADR-034 fix-r1).

    Returns None when `ref` isn't a `<remote>/<branch>` shape against a
    CONFIGURED remote — callers must fail closed in that case, never fall
    back to the ambiguous short form.
    """
    remote, sep, branch = ref.partition("/")
    if not sep or not remote or not branch:
        return None
    remote_check = _git_run(project_root, "remote", "get-url", remote)
    if remote_check.returncode != 0:
        return None
    return f"refs/remotes/{remote}/{branch}"


def _resolve_anchor_content(
    project_root: Path, ref: str, anchor_rel_path: Path
) -> tuple[str | None, AnchorProvenance]:
    """Fetch (best-effort) + resolve ref, then `git show ref:path` from git-committed
    history only. Read order per ADR §2: live fetch first (strongest guarantee),
    falling back to whatever ref already resolves to locally (last successful
    fetch) if the fetch itself fails (network unavailable) — `git show` below
    reads through git's object store either way, never the working tree.

    `ref` is always resolved via its EXPLICIT `refs/remotes/<remote>/<branch>`
    form (see `_qualify_remote_ref`) — never the bare `<remote>/<branch>`
    shorthand, which a local branch of the same name could shadow (Finding 5).
    A `ref` that isn't a resolvable `<remote>/<branch>` form against a
    configured remote fails closed (`resolved=False`) rather than silently
    falling back to the ambiguous form.

    Returns (content, provenance):
      - content is the raw NDJSON text when ref resolves and the anchor file
        exists at that ref (possibly zero anchor lines if brand new).
      - content is None when ref resolves but the anchor file doesn't exist
        YET at that ref — the legitimate "nothing sealed so far" case, NOT a
        failure (provenance.resolved is still True).
      - provenance.resolved is False only when ref itself cannot be resolved
        at all (no network + no cached tracking ref, or project_root isn't a
        git repo) — the fail-closed case (ADR §2 step 3: "can't check" is
        never "assume fine").
    """
    remote = _resolve_remote_name(ref)
    _git_run(project_root, "fetch", remote, timeout=20)  # best-effort; failure handled by rev-parse below

    qualified_ref = _qualify_remote_ref(project_root, ref)
    if qualified_ref is None:
        return None, AnchorProvenance(
            ref=ref,
            resolved=False,
            error=f"ref {ref!r} is not a resolvable <remote>/<branch> form against a configured remote (fail-closed)",
        )

    rev = _git_run(project_root, "rev-parse", qualified_ref)
    if rev.returncode != 0:
        return None, AnchorProvenance(
            ref=ref, resolved=False, error=(rev.stderr or "").strip()[:300] or "ref did not resolve"
        )

    sha = rev.stdout.strip()
    remote_url_proc = _git_run(project_root, "remote", "get-url", remote)
    remote_url = remote_url_proc.stdout.strip() if remote_url_proc.returncode == 0 else None
    provenance = AnchorProvenance(ref=ref, resolved=True, anchor_commit_sha=sha, remote_url=remote_url)

    show = _git_run(project_root, "show", f"{qualified_ref}:{anchor_rel_path.as_posix()}")
    if show.returncode != 0:
        return None, provenance  # path doesn't exist at this ref yet — legitimate, not a fault
    return show.stdout, provenance


def _iter_anchor_lines(content: str):
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            yield parsed


def _parse_anchor_line(raw: dict) -> AnchorRecord:
    return AnchorRecord(
        key=raw["key"],
        ledger_identity=raw["ledger_identity"],
        epoch=int(raw["epoch"]),
        record_type=raw["record_type"],
        origin_type=raw.get("origin_type"),
        origin_hash=raw.get("origin_hash"),
        origin_line_number=raw.get("origin_line_number"),
        entries_before_origin=raw.get("entries_before_origin"),
        closure_hash=raw.get("closure_hash"),
        closure_line_number=raw.get("closure_line_number"),
        entries_in_epoch=raw.get("entries_in_epoch"),
        sealed_at=raw.get("sealed_at"),
    )


def read_git_anchor(
    project_root: Path,
    identity: str,
    epoch: int,
    *,
    kind: Literal["open", "close"] = "open",
    anchor_rel_path: Path = ANCHOR_REL_PATH,
    ref: str = "origin/main",
) -> tuple[AnchorRecord | Literal["corrupt"] | None, AnchorProvenance]:
    """Resolves anchor_key(identity, epoch)'s record (kind="open") or
    closure_key(identity, epoch)'s record (kind="close") from GIT-COMMITTED
    content only (ADR §2). NEVER reads the working-tree file. Returns the
    FIRST matching record in file order; "corrupt" if more than one record
    matches the same key (dupes=broken, ADR §3 — identical rule for open and
    close keys). None only when ref resolves cleanly with genuinely no record
    for this key.
    """
    target_key = anchor_key(identity, epoch) if kind == "open" else closure_key(identity, epoch)
    content, provenance = _resolve_anchor_content(project_root, ref, anchor_rel_path)
    if not provenance.resolved or content is None:
        return None, provenance

    matches = [raw for raw in _iter_anchor_lines(content) if raw.get("key") == target_key]
    if not matches:
        return None, provenance
    if len(matches) > 1:
        return "corrupt", provenance
    return _parse_anchor_line(matches[0]), provenance


def read_git_anchors_for_identity(
    project_root: Path,
    identity: str,
    *,
    anchor_rel_path: Path = ANCHOR_REL_PATH,
    ref: str = "origin/main",
) -> tuple[list[AnchorRecord], AnchorProvenance]:
    """Full-identity scan (ADR §2 C7): every record whose key matches
    '{identity}#*' (open) or '{identity}#*:close' (closure), across ALL
    epochs. Exists because read_git_anchor requires a known epoch, and the
    reverse fail-closed rule needs to ask "does ANY anchor exist for this
    identity" precisely when the ledger is missing/empty/reset and supplies
    no epoch to look up. An empty list from a cleanly-resolved ref (check
    provenance.resolved) means genuinely no anchor exists anywhere for this
    identity — distinct from a resolution failure.
    """
    content, provenance = _resolve_anchor_content(project_root, ref, anchor_rel_path)
    if not provenance.resolved or content is None:
        return [], provenance

    prefix = f"{identity}#"
    records = [
        _parse_anchor_line(raw) for raw in _iter_anchor_lines(content) if str(raw.get("key", "")).startswith(prefix)
    ]
    return records, provenance


def _read_local_head_anchor(
    project_root: Path, key: str, *, anchor_rel_path: Path = ANCHOR_REL_PATH
) -> dict | None:
    """Local-HEAD (not origin/main) lookup used ONLY for seal_and_commit_origin's
    own idempotency check ("read from the actor's own current HEAD, pre-push" —
    ADR §7). Never used by verify_chain/read_git_anchor, which must only trust
    git-committed remote history."""
    show = _git_run(project_root, "show", f"HEAD:{anchor_rel_path.as_posix()}")
    if show.returncode != 0:
        return None
    for raw in _iter_anchor_lines(show.stdout):
        if raw.get("key") == key:
            return raw
    return None


def _read_remote_base_anchor(
    project_root: Path, branch: str, key: str, *, anchor_rel_path: Path = ANCHOR_REL_PATH
) -> dict | None:
    """Has `key` already landed on the REMOTE tracking ref for the seal's base
    branch (``origin/<branch>``, post-fetch, resolved through the same
    remote-ref qualification as ``read_git_anchor``)?

    Distinguishes a TRUE noop (the anchor already reached origin — e.g. a
    prior PR merged) from a partial-seal retry where a previous attempt
    committed LOCALLY on its own seal branch but the ``git push`` itself
    raised (Finding 3, ADR-034 fix-r1) — local HEAD alone can never answer
    that question, since HEAD stays parked on the uncommitted seal branch
    after a push failure. Same read discipline as read_git_anchor: fetch
    best-effort, `git show` through git's object store, never the working
    tree.
    """
    content, provenance = _resolve_anchor_content(project_root, f"origin/{branch}", anchor_rel_path)
    if not provenance.resolved or content is None:
        return None
    for raw in _iter_anchor_lines(content):
        if raw.get("key") == key:
            return raw
    return None


def check_anchor_immutability(base_content: str, head_content: str) -> list[dict]:
    """Pure diff-logic for ADR §3's write-side rule: any key present in BOTH
    base and head whose content changed, or present in base but removed in
    head, is a violation. Brand-new keys in head are allowed. This is the
    LOGIC the PR-2 GitHub Actions check invokes (base branch vs PR head) — no
    CI/workflow wiring lives in this module.
    """
    base_by_key = {raw["key"]: raw for raw in _iter_anchor_lines(base_content) if "key" in raw}
    head_by_key = {raw["key"]: raw for raw in _iter_anchor_lines(head_content) if "key" in raw}

    violations: list[dict] = []
    for key, base_record in base_by_key.items():
        if key not in head_by_key:
            violations.append({"key": key, "violation": "removed"})
        elif head_by_key[key] != base_record:
            violations.append({"key": key, "violation": "modified"})
    return violations


# ---------------------------------------------------------------------------
# Write side: seal_and_commit_origin (ADR §4, §7)
# ---------------------------------------------------------------------------


def _origin_record(identity: str, fp: OriginFingerprint, *, sealed_at: str) -> dict:
    return {
        "key": anchor_key(identity, fp.epoch),
        "ledger_identity": identity,
        "epoch": fp.epoch,
        "record_type": "open",
        "origin_type": fp.origin_type,
        "origin_hash": fp.origin_hash,
        "origin_line_number": fp.origin_line_number,
        "entries_before_origin": fp.entries_before_origin,
        "sealed_at": sealed_at,
    }


def _closure_record(identity: str, fp: ClosureFingerprint, *, sealed_at: str) -> dict:
    return {
        "key": closure_key(identity, fp.epoch),
        "ledger_identity": identity,
        "epoch": fp.epoch,
        "record_type": "close",
        "closure_hash": fp.closure_hash,
        "closure_line_number": fp.closure_line_number,
        "entries_in_epoch": fp.entries_in_epoch,
        "sealed_at": sealed_at,
    }


def _pr_url_for(project_root: Path, pr_number: int) -> str | None:
    proc = subprocess.run(
        ["gh", "pr", "view", str(pr_number), "--json", "url", "--jq", ".url"],
        capture_output=True,
        text=True,
        timeout=20,
        cwd=str(project_root),
    )
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


def _seal_branch_name(identity: str, epoch: int) -> str:
    """Deterministic from (identity, epoch) alone — recomputed by both the
    fresh-seal path and the partial-seal resume path (Finding 3) so a retry
    in a NEW process can find the exact same local branch a prior attempt
    committed to, with no state to persist between them."""
    safe_identity = identity.replace("/", "-").replace(":", "-").replace("#", "-")
    return f"chain-origin-seal/{safe_identity}/epoch-{epoch}"


def _commit_and_push_anchor(
    project_root: Path,
    base_branch: str,
    records: list[dict],
    *,
    identity: str,
    epoch: int,
) -> tuple[str, str | None]:
    """Thin commit-orchestration helper (dispatch scope: file-write + SealResult;
    CI wiring is PR-2). Appends `records` to the working-tree copy of
    governance/chain-origin.ndjson, commits on a fresh branch, pushes, and
    best-effort opens a PR via the shared gh_pr_ensure helper (never raises on
    PR-creation failure — see gh_pr_ensure.ensure_pr). Git add/commit/push
    failures DO raise (RuntimeError via _git_run_checked): a half-sealed
    ledger with no anchor commit must read as `broken` per verify_chain's
    fail-closed contract, never silently swallowed (ADR §4).
    """
    anchor_path = project_root / ANCHOR_REL_PATH
    anchor_path.parent.mkdir(parents=True, exist_ok=True)
    with anchor_path.open("a", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")

    branch_name = _seal_branch_name(identity, epoch)

    _git_run_checked(project_root, "checkout", "-B", branch_name)
    _git_run_checked(project_root, "add", str(ANCHOR_REL_PATH))
    _git_run_checked(
        project_root, "commit", "-m", f"chore(chain-origin): seal epoch {epoch} for {identity}"
    )
    _git_run_checked(project_root, "push", "-u", "origin", branch_name)

    pr_result = ensure_pr(
        branch_name,
        project_root,
        title=f"chore(chain-origin): seal epoch {epoch} for {identity}",
        body=(
            "Automated ADR-034 chain-origin seal.\n\n"
            f"- ledger_identity: `{identity}`\n"
            f"- epoch: {epoch}\n"
            f"- base branch: {base_branch}\n"
        ),
    )
    pr_url = _pr_url_for(project_root, pr_result["pr_number"]) if pr_result.get("pr_number") is not None else None
    return branch_name, pr_url


def _missing_closure_fingerprints(
    ledger_path: Path,
    project_root: Path,
    branch: str,
    identity: str,
    epochs: list[tuple[int, int]],
) -> list[ClosureFingerprint]:
    """Finding 2 fix (ADR-034 fix-r2): every epoch the CURRENT ledger tail
    shows as CLOSED (a later epoch's marker exists) whose closure_key hasn't
    yet landed on ``origin/<branch>`` — computed and returned regardless of
    whether THIS seal call is the one that freshly opened the newer epoch.

    Without this, a crash between appending a new epoch's marker (durable on
    the ledger) and this call's own commit/push (never durable anywhere)
    leaves epoch_state reporting the new epoch as already-current on retry —
    ``opened_new_epoch=False``, ``prior_epoch=None`` — so a "only close what
    I just opened" check would silently skip the prior epoch's closure
    forever, and verify_chain would stay broken ("no closure record for a
    CLOSED epoch") permanently. Idempotent per the same remote-check
    discipline as ``seal_and_commit_origin``'s own noop check
    (``_read_remote_base_anchor``): an epoch already closed on origin is
    never recomputed/re-emitted. The currently-open (last) epoch is exempt —
    there is nothing to close yet.
    """
    missing: list[ClosureFingerprint] = []
    for idx, (epoch, _marker_line) in enumerate(epochs):
        if idx + 1 >= len(epochs):
            continue  # still open — no closure expected yet
        if _read_remote_base_anchor(project_root, branch, closure_key(identity, epoch)) is not None:
            continue  # already sealed by a prior (possibly different) seal call
        next_marker_line = epochs[idx + 1][1]
        missing.append(compute_epoch_closure(ledger_path, epoch, next_epoch_marker_line=next_marker_line))
    return missing


def _resume_partial_seal(project_root: Path, base_branch: str, identity: str, epoch: int) -> SealResult:
    """Finding 3 fix (ADR-034 fix-r1): a prior seal attempt committed the
    anchor record LOCALLY (on its deterministic seal branch, see
    _seal_branch_name) but the ``git push`` itself raised before origin ever
    saw it. Resume by re-pushing that SAME local branch/commit and
    re-ensuring its PR — NEVER by calling _commit_and_push_anchor again,
    which would re-append the record to the working-tree file and create a
    SECOND commit (a duplicate record). The local branch ref is stable
    across process restarts, so no state needs to be threaded through: just
    recompute its name and push it.
    """
    branch_name = _seal_branch_name(identity, epoch)
    _git_run_checked(project_root, "push", "-u", "origin", branch_name)

    pr_result = ensure_pr(
        branch_name,
        project_root,
        title=f"chore(chain-origin): seal epoch {epoch} for {identity}",
        body=(
            "Automated ADR-034 chain-origin seal (resumed after a prior push failure).\n\n"
            f"- ledger_identity: `{identity}`\n"
            f"- epoch: {epoch}\n"
            f"- base branch: {base_branch}\n"
        ),
    )
    pr_url = _pr_url_for(project_root, pr_result["pr_number"]) if pr_result.get("pr_number") is not None else None

    return SealResult(
        action="resumed", epoch=epoch, origin=None, branch_name=branch_name, pr_url=pr_url, closed_epoch=None
    )


def seal_and_commit_origin(
    ledger_path: Path,
    project_root: Path,
    *,
    project_id: str,
    project_data_dir: Path,
    branch: str = "main",
    force_new_epoch: bool = False,
    branch_protection_confirmed: bool = False,
) -> SealResult:
    """Operator/T0-governed entrypoint (ADR §7). Holds
    ledger_lock_path(ledger_path)'s flock across the full
    read-current-epoch -> compute -> anchor sequence.

    REFUSES (raises BranchProtectionUnconfirmedError, no seal attempted) unless
    the caller passes branch_protection_confirmed=True — ADR §6 step 2b's hard
    activation precondition, verified independently by the caller (a future
    gh-api branch-protection check, PR-2 scope) BEFORE calling this. This
    library never performs that gh-api call itself.

    Idempotent per epoch: an anchor_key(identity, epoch) already present in
    the anchor file (read from the actor's own current HEAD, pre-push) is a
    candidate no-op for that epoch — but the noop is only REAL when that
    anchor has also reached origin's base branch (Finding 3, ADR-034
    fix-r1). A commit that landed locally (on its own seal branch) but whose
    `git push` itself raised leaves HEAD parked on that seal branch with the
    anchor already committed; local-HEAD alone can't tell that apart from a
    genuine prior success, so it's cross-checked against
    ``origin/<branch>``. Local-only resumes the push (action="resumed") —
    same branch, same commit, no new record appended. A NEW epoch since the
    last seal, or a previously marker-opened-but-never-anchored epoch (crash
    BEFORE any commit), produces a new anchor record (action="sealed").
    force_new_epoch=True opens a new epoch (and closes the prior one) even
    when chaining hasn't lapsed.
    """
    if not branch_protection_confirmed:
        raise BranchProtectionUnconfirmedError(
            f"seal_and_commit_origin refuses to seal {ledger_path} against branch "
            f"{branch!r}: branch_protection_confirmed=False. ADR-034 §6 step 2b is a "
            "hard activation precondition, not best-effort — the caller must "
            "independently confirm (gh api repos/:owner/:repo/branches/:branch/"
            "protection) that the anchor-immutability check is a required status "
            "check with enforce_admins on before calling this. No commit or push "
            "was attempted."
        )

    with _ledger_locked(ledger_path):
        max_epoch, chaining_active = epoch_state(ledger_path)
        opened_new_epoch = False
        prior_epoch: int | None = None

        # A new marked epoch is needed when chaining lapsed (existing ADR-029
        # trigger), the caller forces it, OR max_epoch==0 — the ledger has
        # NEVER had a chain_epoch_start marker, whether because it's empty or
        # because it's a marker-less GENESIS chain ("verified", not
        # "verified-segmented"). Epoch 0 has no marker to anchor by
        # construction (ADR §1: "the immutable pre-adoption entries form
        # epoch 0"), so max_epoch==0 can never itself be sealed/anchored —
        # only a newly-opened epoch 1 can.
        if force_new_epoch or not chaining_active or max_epoch == 0:
            prior_epoch = max_epoch if max_epoch > 0 else None
            new_epoch = max_epoch + 1
            _append_epoch_marker_locked(ledger_path, new_epoch)
            epoch = new_epoch
            opened_new_epoch = True
        else:
            epoch = max_epoch

        identity = ledger_identity(project_id, ledger_path, project_data_dir)
        local_existing = _read_local_head_anchor(project_root, anchor_key(identity, epoch))
        needs_resume = False
        if local_existing is not None and not opened_new_epoch:
            remote_existing = _read_remote_base_anchor(project_root, branch, anchor_key(identity, epoch))
            if remote_existing is not None:
                return SealResult(
                    action="noop", epoch=epoch, origin=None, branch_name=None, pr_url=None, closed_epoch=None
                )
            # Anchor committed locally (prior attempt's seal branch) but
            # never reached origin — a partial-seal retry (Finding 3), not a
            # genuine noop. Resume outside the lock below instead of
            # re-appending/re-committing a duplicate record.
            needs_resume = True

        origin_fp: OriginFingerprint | None = None
        closure_fps: list[ClosureFingerprint] = []
        if not needs_resume:
            origin_fp = compute_epoch_fingerprint(ledger_path, epoch)
            # Backfill EVERY closed-but-unanchored epoch (Finding 2, ADR-034
            # fix-r2) — not just `prior_epoch` (the epoch THIS call happens to
            # have freshly closed). A prior crash between the marker-append
            # and the commit/push can leave an older epoch's closure missing
            # even when this call itself doesn't open a new epoch.
            closure_fps = _missing_closure_fingerprints(
                ledger_path, project_root, branch, identity, _observed_epochs(ledger_path)
            )

    # Git/commit/push below intentionally happens OUTSIDE the ledger lock — it
    # touches project_root's git state, not the ledger file, and a slow push
    # must not hold receipt appends hostage.
    if needs_resume:
        return _resume_partial_seal(project_root, branch, identity, epoch)

    sealed_at = datetime.now(timezone.utc).isoformat()
    records = [_origin_record(identity, origin_fp, sealed_at=sealed_at)]
    for closure_fp in closure_fps:
        records.append(_closure_record(identity, closure_fp, sealed_at=sealed_at))

    branch_name, pr_url = _commit_and_push_anchor(project_root, branch, records, identity=identity, epoch=epoch)

    return SealResult(
        action="sealed",
        epoch=epoch,
        origin=origin_fp,
        branch_name=branch_name,
        pr_url=pr_url,
        closed_epoch=prior_epoch,
    )


# ---------------------------------------------------------------------------
# Anchor-aware verify_chain (ADR §2, §7) — a NEW function, distinct from
# ndjson_hash_chain.verify_chain (imported above as _base_verify_chain, which
# stays byte-for-byte unchanged so its five existing callers keep working
# unmodified; threading project_root through those call sites is the ADR §6
# step-3 migration, gated on step 2b, and is explicitly PR-2+ scope).
# ---------------------------------------------------------------------------


def verify_chain(
    path: Path,
    *,
    project_root: Path | None = None,
    project_id: str | None = None,
    project_data_dir: Path | None = None,
    anchor_ref: str = "origin/main",
) -> tuple[bool, list[dict], str, AnchorProvenance | None]:
    """Existing ADR-029 epoch-aware walk (via ndjson_hash_chain.verify_chain),
    PLUS the ADR-034 anchor-aware contract (§2):

    - Forward (open): every chain_epoch_start marker observed requires a
      matching git anchor for anchor_key(identity, epoch) — absence,
      resolution failure, or "corrupt" (duplicate) all report "broken".
    - Forward (closed): every epoch that has closed (a later epoch's marker
      is present) requires a matching closure record whose closure_hash/
      closure_line_number reproduce exactly what the walk observes. The
      currently-open (latest) epoch is exempt.
    - Reverse: resolved BEFORE the unchained/empty short-circuit via
      read_git_anchors_for_identity — a missing/reset ledger with any
      committed anchor for its identity is "broken", not "unchained".

    Ledgers that never enabled chaining AND have no anchor anywhere are
    unaffected. project_root/project_id/project_data_dir all missing on a
    ledger the base walk shows as unchained is the "additive, read path
    unchanged" case (ADR §6 step 1) — no anchor check is attempted, result
    passes through unchanged. Missing identity params on a CHAINING-ENABLED
    ledger is itself "broken" (cannot silently skip the anchor check).
    """
    base_ok, base_violations, base_status = _base_verify_chain(path)
    saw_chain = base_status != "unchained"
    identity_available = project_root is not None and project_id is not None and project_data_dir is not None

    if not saw_chain:
        if not identity_available:
            return base_ok, list(base_violations), base_status, None

        identity = ledger_identity(project_id, path, project_data_dir)  # type: ignore[arg-type]
        anchors, provenance = read_git_anchors_for_identity(project_root, identity, ref=anchor_ref)  # type: ignore[arg-type]
        if not provenance.resolved:
            violations = list(base_violations) + [
                {"note": "anchor resolution failed (fail-closed)", "error": provenance.error}
            ]
            return False, violations, "broken", provenance
        if anchors:
            violations = list(base_violations) + [
                {
                    "note": "git anchor exists for this ledger_identity but the ledger is missing/empty/unchained",
                    "anchor_keys": [a.key for a in anchors],
                }
            ]
            return False, violations, "broken", provenance
        return base_ok, list(base_violations), base_status, provenance

    if not identity_available:
        violations = list(base_violations) + [
            {"note": "project_root/project_id/project_data_dir required for a chaining-enabled ledger (ADR-034 §2)"}
        ]
        return False, violations, "broken", None

    identity = ledger_identity(project_id, path, project_data_dir)  # type: ignore[arg-type]
    epochs = _observed_epochs(path)
    observed_epoch_numbers = {epoch for epoch, _marker_line in epochs}
    max_observed_epoch = max(observed_epoch_numbers) if observed_epoch_numbers else None
    violations = list(base_violations)
    last_provenance: AnchorProvenance | None = None
    anchor_failed = False

    # Reverse scan (Findings 1+2, ADR-034 fix-r1): MUST run unconditionally in
    # the chained path, not only when the base walk reports "unchained". Two
    # bypasses this closes:
    #   (a) a markerless GENESIS rechain strips every chain_epoch_start
    #       marker — _observed_epochs(path) is then [], so the per-epoch loop
    #       below never runs and an existing origin anchor is never consulted;
    #   (b) a rollback to an earlier epoch while origin holds a LATER epoch's
    #       anchor — the earlier epoch still matches its own fingerprint and,
    #       unqualified, would read as "the open latest epoch, no closure
    #       required".
    # Both require checking every anchor this identity has EVER had against
    # what the CURRENT ledger tail actually still shows — not just the
    # epochs the tail happens to observe right now.
    anchors_for_identity, id_provenance = read_git_anchors_for_identity(project_root, identity, ref=anchor_ref)  # type: ignore[arg-type]
    last_provenance = id_provenance
    max_anchored_open_epoch: int | None = None
    if not id_provenance.resolved:
        violations.append({"note": "identity anchor scan failed (fail-closed)", "error": id_provenance.error})
        anchor_failed = True
    else:
        if not anchors_for_identity:
            # Finding 1 (ADR-034 fix-r2): a chaining-active ledger (saw_chain
            # True — we're already inside that branch here) with ZERO anchors
            # anywhere for its identity. The per-epoch loop below can't catch
            # this on its own when epochs == [] (every chain_epoch_start
            # marker stripped, a markerless GENESIS rechain) — it just never
            # runs. A ledger with markers but genuinely no anchor yet is
            # already caught per-epoch further down; this closes the
            # markerless variant of the same gap so it fails identically.
            violations.append(
                {
                    "note": "chaining-active ledger has no git anchor anywhere for this "
                    "ledger_identity (unanchored or markerless chain)",
                }
            )
            anchor_failed = True
        open_anchor_epochs = {r.epoch for r in anchors_for_identity if r.record_type == "open"}
        if open_anchor_epochs:
            max_anchored_open_epoch = max(open_anchor_epochs)
        if open_anchor_epochs and not observed_epoch_numbers:
            violations.append(
                {
                    "note": "git anchors exist for this ledger_identity but the ledger shows no "
                    "chain_epoch_start markers (markerless rechain)",
                    "anchor_epochs": sorted(open_anchor_epochs),
                }
            )
            anchor_failed = True
        missing_anchored_epochs = open_anchor_epochs - observed_epoch_numbers
        if missing_anchored_epochs:
            violations.append(
                {
                    "note": "git anchor(s) exist for epoch(s) the ledger no longer observes",
                    "missing_epochs": sorted(missing_anchored_epochs),
                }
            )
            anchor_failed = True
        if (
            max_anchored_open_epoch is not None
            and max_observed_epoch is not None
            and max_anchored_open_epoch > max_observed_epoch
        ):
            violations.append(
                {
                    "note": "highest anchored epoch exceeds highest observed epoch (rollback)",
                    "anchored_max_epoch": max_anchored_open_epoch,
                    "observed_max_epoch": max_observed_epoch,
                }
            )
            anchor_failed = True

    for idx, (epoch, _marker_line) in enumerate(epochs):
        record, provenance = read_git_anchor(project_root, identity, epoch, kind="open", ref=anchor_ref)  # type: ignore[arg-type]
        last_provenance = provenance
        if not provenance.resolved:
            violations.append({"epoch": epoch, "note": "anchor resolution failed (fail-closed)", "error": provenance.error})
            anchor_failed = True
            continue
        if record is None:
            violations.append({"epoch": epoch, "note": "no git anchor for this epoch's chain_epoch_start marker"})
            anchor_failed = True
            continue
        if record == "corrupt":
            violations.append({"epoch": epoch, "note": "duplicate git anchor records for this epoch (corrupt)"})
            anchor_failed = True
            continue

        observed = compute_epoch_fingerprint(path, epoch)
        if (
            record.origin_hash != observed.origin_hash
            or record.origin_line_number != observed.origin_line_number
            or record.entries_before_origin != observed.entries_before_origin
        ):
            violations.append(
                {
                    "epoch": epoch,
                    "note": "epoch opening fingerprint mismatch (tamper)",
                    "anchor_origin_hash": record.origin_hash,
                    "observed_origin_hash": observed.origin_hash,
                }
            )
            anchor_failed = True

        if idx + 1 >= len(epochs):
            # This is the last epoch the CURRENT ledger tail observes, so
            # there's no local next-epoch marker to bound a closure
            # computation against. That's only a legitimate "still open,
            # no closure expected" exemption when this is ALSO truly the
            # highest ANCHORED epoch (Finding 2's rollback-tightening) — a
            # ledger rolled back to an earlier epoch while origin holds a
            # LATER epoch's anchor must NOT read as "still open" just
            # because it happens to be the local tail. The reverse scan
            # above already flags that case broken (max_anchored_open_epoch
            # > max_observed_epoch / missing anchored epoch), so there is
            # nothing further to compute here either way.
            continue

        next_marker_line = epochs[idx + 1][1]
        closure_record, cprovenance = read_git_anchor(project_root, identity, epoch, kind="close", ref=anchor_ref)  # type: ignore[arg-type]
        last_provenance = cprovenance
        if not cprovenance.resolved:
            violations.append({"epoch": epoch, "note": "closure anchor resolution failed (fail-closed)", "error": cprovenance.error})
            anchor_failed = True
            continue
        if closure_record is None:
            violations.append({"epoch": epoch, "note": "no closure record for a CLOSED epoch"})
            anchor_failed = True
            continue
        if closure_record == "corrupt":
            violations.append({"epoch": epoch, "note": "duplicate closure records for this epoch (corrupt)"})
            anchor_failed = True
            continue

        observed_closure = compute_epoch_closure(path, epoch, next_epoch_marker_line=next_marker_line)
        if (
            closure_record.closure_hash != observed_closure.closure_hash
            or closure_record.closure_line_number != observed_closure.closure_line_number
            or closure_record.entries_in_epoch != observed_closure.entries_in_epoch
        ):
            violations.append(
                {
                    "epoch": epoch,
                    "note": "epoch closure fingerprint mismatch (tamper)",
                    "anchor_closure_hash": closure_record.closure_hash,
                    "observed_closure_hash": observed_closure.closure_hash,
                }
            )
            anchor_failed = True

    final_ok = base_ok and not anchor_failed
    final_status = base_status if final_ok else "broken"
    return final_ok, violations, final_status, last_provenance
