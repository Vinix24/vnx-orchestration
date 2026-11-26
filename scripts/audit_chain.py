"""CLI for hash-chain operations: verify, walk, and the ADR-034 seal.

Operator commands for vnx-dev (receipt-hashketen-aan H4), every argument written out:

    python3 scripts/audit_chain.py seal ~/.vnx-data/vnx-dev/state/t0_receipts.ndjson \\
        --project-id vnx-dev --project-root ~/Development/vnx-orchestration \\
        --data-dir ~/.vnx-data/vnx-dev --anchor-ref origin/main
    python3 scripts/audit_chain.py verify ~/.vnx-data/vnx-dev/state/t0_receipts.ndjson \\
        --project-id vnx-dev --project-root ~/Development/vnx-orchestration \\
        --data-dir ~/.vnx-data/vnx-dev --anchor-ref origin/main

``seal`` first runs the branch-protection check of chain_branch_protection_check.py
for the --project-root repo and the --anchor-ref branch, and seals only when it is
confirmed. A copy of the ledger is verified with the identity of production by
passing the same --project-id/--project-root/--anchor-ref and a --data-dir whose
relative layout matches (copy at <dir>/state/t0_receipts.ndjson, --data-dir <dir>).
``verify`` without any identity argument walks the epochs only and checks no anchor.
"""
import argparse
import dataclasses
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

import chain_origin_anchor as coa  # noqa: E402
from ndjson_hash_chain import verify_chain, walk_chain  # noqa: E402
from project_root import resolve_central_data_dir, resolve_project_id, resolve_project_root  # noqa: E402


HEALTHY_STATUSES = ("verified", "verified-segmented")
EXIT_UNKNOWN_STATUS = 3
EXIT_IDENTITY_ERROR = 4
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TAIL_COVERAGE = (
    "only content bound by a following line or a closing anchor is covered: a torn, "
    "unparseable tail is found; removing complete last lines or changing the last valid "
    "JSON line of the open epoch is not (the open tail piece of ADR-034)"
)


class IdentityError(ValueError):
    """An identity argument that is missing and unresolvable, or invalid."""


def _verify_report(path, ok, violations, status):
    """Map a verify_chain() result to (payload, exit code), fail-closed.

    Healthy only on a positive status AND ok is True (exit 0). "unchained"
    means nothing could be checked (exit 2). "broken", or a healthy status
    with ok not True, is a real defect (exit 1, violations capped at 20 plus
    the total). Any other status is unknown, never healthy (exit 3).
    """
    if status in HEALTHY_STATUSES and ok is True:
        return {"verified": True, "status": status, "path": str(path), "tail_coverage": TAIL_COVERAGE}, 0
    if status == "unchained":
        # D1: "unchained" means verify_chain() could not check anything — no
        # entry carries prev_hash, so integrity is UNMEASURED, not confirmed.
        # Chaining stays an explicit decision through vnx config set.
        return {
            "verified": False,
            "status": "unchained",
            "path": str(path),
            "warning": (
                "Hash-chain verification not possible: no entries carry prev_hash. "
                "After sealing an existing ledger's epoch, enable chaining with "
                'vnx config set VNX_HASH_CHAIN_REQUIRED 1 --approval-id <approval-id> --reason "<why>" '
                "for the ledger's project (add --global for all projects without a project row)."
            ),
        }, 2
    payload = {
        "verified": False,
        "status": status,
        "path": str(path),
        "violations": violations[:20],
        "violation_count": len(violations),
    }
    if status == "broken" or status in HEALTHY_STATUSES:
        return payload, 1
    payload["error"] = f"unknown verify_chain status: {status!r}"
    return payload, EXIT_UNKNOWN_STATUS


def _resolve_identity(args):
    """(project_id, project_root, data_dir, branch) from the arguments, each
    validated; a missing one is resolved, an unresolvable one is IdentityError."""
    try:
        root = (args.project_root or resolve_project_root(__file__)).expanduser().resolve()
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=15)
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != root:
            raise IdentityError(f"--project-root {root} is not the top level of a git repo")
        project_id = args.project_id or resolve_project_id(root)
        if not _PROJECT_ID_RE.match(project_id):
            raise IdentityError(f"invalid --project-id {project_id!r}")
        data_dir = (args.data_dir or resolve_central_data_dir(project_id)).expanduser().resolve()
    except RuntimeError as exc:
        raise IdentityError(str(exc)) from exc
    if not data_dir.is_dir():
        raise IdentityError(f"--data-dir {data_dir} is not a directory")
    remote, _sep, branch = args.anchor_ref.partition("/")
    if remote != "origin" or not branch or ".." in branch:
        raise IdentityError(f"--anchor-ref must be origin/<branch>, got {args.anchor_ref!r}")
    try:
        identity = coa.ledger_identity(project_id, args.path.expanduser(), data_dir)
    except ValueError as exc:
        raise IdentityError(f"ledger {args.path} is not under --data-dir {data_dir}") from exc
    return project_id, root, data_dir, branch, identity


def _cmd_verify(args):
    if not any((args.project_id, args.project_root, args.data_dir, args.anchor_ref_given)):
        ok, violations, status = verify_chain(args.path)
        payload, code = _verify_report(args.path, ok, violations, status)
        payload["anchor_checked"] = False
        return payload, code
    project_id, root, data_dir, _branch, identity = _resolve_identity(args)
    ok, violations, status, provenance = coa.verify_chain(
        args.path.expanduser(), project_root=root, project_id=project_id,
        project_data_dir=data_dir, anchor_ref=args.anchor_ref,
    )
    payload, code = _verify_report(args.path, ok, violations, status)
    payload.update(anchor_checked=True, ledger_identity=identity,
                   anchor=dataclasses.asdict(provenance) if provenance else None)
    return payload, code


def _cmd_seal(args):
    import chain_branch_protection_check as cbpc  # scripts/ is sys.path[0] when run as a script

    project_id, root, data_dir, branch, identity = _resolve_identity(args)
    ledger = args.path.expanduser().resolve()
    if not ledger.is_file():
        raise IdentityError(f"ledger {ledger} does not exist")
    protection = cbpc.protection_status(root, branch)
    payload = {"ledger_identity": identity, "branch_protection": dataclasses.asdict(protection)}
    if protection.confirmed is not True:
        payload.update(sealed=False, error=f"branch protection not confirmed: {protection.reason}")
        return payload, 1
    try:
        result = coa.seal_and_commit_origin(
            ledger, root, project_id=project_id, project_data_dir=data_dir,
            branch=branch, branch_protection_confirmed=True,
        )
    except (RuntimeError, ValueError, OSError) as exc:
        payload.update(sealed=False, error=f"{type(exc).__name__}: {exc}")
        return payload, 1
    ref = f"origin/{result.branch_name}" if result.branch_name else args.anchor_ref
    record, provenance = coa.read_git_anchor(root, identity, result.epoch, ref=ref)
    payload.update(sealed=True, seal_result=dataclasses.asdict(result), anchor=dataclasses.asdict(provenance),
                   anchor_record=dataclasses.asdict(record) if dataclasses.is_dataclass(record) else record)
    if not provenance.resolved or not dataclasses.is_dataclass(record):
        payload.update(sealed=False, error=f"anchor record not readable at {ref}")
        return payload, 1
    return payload, 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_verify = sub.add_parser("verify", help="Verify chain integrity (anchor-aware with an identity)")
    p_seal = sub.add_parser("seal", help="Check branch protection, then seal and anchor (ADR-034)")
    for p in (p_verify, p_seal):
        p.add_argument("path", type=Path)
        p.add_argument("--project-id", default=None)
        p.add_argument("--project-root", type=Path, default=None, help="repo that carries the anchor")
        p.add_argument("--data-dir", type=Path, default=None, help="project data dir the ledger lives in")
        p.add_argument("--anchor-ref", default=None, help="anchor ref (default: origin/main)")

    p_walk = sub.add_parser("walk", help="Walk chain and emit hashes")
    p_walk.add_argument("path", type=Path)

    args = parser.parse_args(argv)

    if args.cmd == "walk":
        for line_no, entry, hash_ in walk_chain(args.path):
            print(f"{line_no}\t{hash_[:16]}\t{entry.get('event_type', 'plain')}")
        return
    args.anchor_ref_given = args.anchor_ref is not None
    args.anchor_ref = args.anchor_ref or "origin/main"
    try:
        payload, code = (_cmd_verify if args.cmd == "verify" else _cmd_seal)(args)
    except IdentityError as exc:
        payload, code = {"verified": False, "error": f"identity: {exc}", "path": str(args.path)}, EXIT_IDENTITY_ERROR
    print(json.dumps(payload, indent=2, default=str))
    sys.exit(code)


if __name__ == "__main__":
    main()
