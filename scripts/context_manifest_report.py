#!/usr/bin/env python3
"""context_manifest_report.py: read ``context_manifests.ndjson`` per provider (agents-als-map PR-4c).

Read-only. Per provider: the number of sidecar lines, the share of lines whose
manifest records an instruction file in the config home, a user skill, or a
hook, and the lines per ``status``. Over the whole sidecar: ``foreign_project_lines``
(lines whose ``project_id`` is not the store's own, ADR-050 contract 6), the
unreadable lines by line number, and the receipts whose ``context_manifest``
hash has no sidecar line (ADR-050 risk on readers).

The provider of a line comes from the receipt with the same ``dispatch_id`` in
``t0_receipts.ndjson``. A line without such a receipt is ``unknown``. Prints
paths, counts and hashes only: no file content, no address.

Usage: context_manifest_report.py [--json] [--by provider] [--state-dir DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

_LIB = Path(__file__).resolve().parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

LEDGER = "context_manifests.ndjson"
RECEIPTS = "t0_receipts.ndjson"
UNKNOWN_PROVIDER = "unknown"


def _iter_lines(path: Path) -> Iterator[Tuple[int, str]]:
    """``(line number, text)`` of every non-blank line; undecodable bytes become U+FFFD."""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for number, raw in enumerate(handle, start=1):
            text = raw.rstrip("\r\n")
            if text.strip():
                yield number, text


def _parse_sidecar_line(text: str) -> Optional[Dict[str, Any]]:
    """The line as a dict, or ``None`` when it is not one the writer could have written."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if not (isinstance(data.get("dispatch_id"), str) and data["dispatch_id"]):
        return None
    if not (isinstance(data.get("project_id"), str) and data["project_id"]):
        return None
    if not isinstance(data.get("manifest"), dict):
        return None
    return data


def read_sidecar(path: Path) -> Tuple[List[Dict[str, Any]], List[int]]:
    """The readable lines (each with its ``sha256``) and the numbers of the unreadable ones."""
    lines: List[Dict[str, Any]] = []
    unreadable: List[int] = []
    for number, text in _iter_lines(path):
        data = _parse_sidecar_line(text)
        if data is None:
            unreadable.append(number)
            continue
        data["_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        lines.append(data)
    return lines, unreadable


def _receipt_facts(receipt: Any) -> Optional[Tuple[str, str, Optional[str]]]:
    """``(dispatch_id, provider, manifest sha256)`` of a receipt, or ``None`` without a dispatch id."""
    if not isinstance(receipt, dict):
        return None
    dispatch_id = receipt.get("dispatch_id")
    if not (isinstance(dispatch_id, str) and dispatch_id):
        return None
    provider = receipt.get("provider")
    stamp = receipt.get("context_manifest")
    sha = stamp.get("sha256") if isinstance(stamp, dict) else None
    return (
        dispatch_id,
        provider if isinstance(provider, str) and provider else UNKNOWN_PROVIDER,
        sha if isinstance(sha, str) and sha else None,
    )


def read_receipts(path: Path) -> Dict[str, Any]:
    """Providers by dispatch id, the manifest hashes the receipts name, and the unreadable receipt count."""
    providers: Dict[str, str] = {}
    hashes: List[Tuple[str, str]] = []
    unreadable = 0
    if not path.is_file():
        return {"providers": providers, "hashes": hashes, "unreadable": unreadable}
    for _number, text in _iter_lines(path):
        try:
            facts = _receipt_facts(json.loads(text))
        except ValueError:
            unreadable += 1
            continue
        if facts is None:
            continue
        providers.setdefault(facts[0], facts[1])
        if facts[2] is not None:
            hashes.append((facts[0], facts[2]))
    return {"providers": providers, "hashes": hashes, "unreadable": unreadable}


def _recorded(manifest: Dict[str, Any]) -> Dict[str, bool]:
    """What this manifest records: instruction in config home, user skill, hook."""
    instructions = manifest.get("instructions")
    files = instructions.get("files") if isinstance(instructions, dict) else None
    skills = manifest.get("skills")
    initial = skills.get("initial") if isinstance(skills, dict) else None
    user = initial.get("user") if isinstance(initial, dict) else None
    hooks = manifest.get("hooks")
    hook_list = hooks.get("hooks") if isinstance(hooks, dict) else None
    return {
        "in_config_home": any(isinstance(f, dict) and f.get("in_config_home") is True
                              for f in (files if isinstance(files, list) else [])),
        "user_skills": isinstance(user, list) and len(user) > 0,
        "hooks": isinstance(hook_list, list) and len(hook_list) > 0,
    }


def _new_bucket() -> Dict[str, Any]:
    return {"lines": 0, "status": {"ok": 0, "partial": 0},
            "in_config_home": 0, "user_skills": 0, "hooks": 0}


def _share(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def _finish_bucket(bucket: Dict[str, Any]) -> Dict[str, Any]:
    total = bucket["lines"]
    out = {"lines": total, "status": bucket["status"]}
    for key in ("in_config_home", "user_skills", "hooks"):
        out[key] = {"lines": bucket[key], "share": _share(bucket[key], total)}
    return out


def _fold_lines(lines: List[Dict[str, Any]], providers: Dict[str, str], own_project: str) -> Dict[str, Any]:
    buckets: Dict[str, Dict[str, Any]] = {}
    foreign = 0
    for line in lines:
        if line["project_id"] != own_project:
            foreign += 1
        bucket = buckets.setdefault(providers.get(line["dispatch_id"], UNKNOWN_PROVIDER), _new_bucket())
        bucket["lines"] += 1
        status = line["manifest"].get("status")
        key = status if isinstance(status, str) and status else "missing"
        bucket["status"][key] = bucket["status"].get(key, 0) + 1
        for name, present in _recorded(line["manifest"]).items():
            bucket[name] += 1 if present else 0
    return {"buckets": buckets, "foreign": foreign}


def _total_bucket(buckets: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    total = _new_bucket()
    for bucket in buckets.values():
        total["lines"] += bucket["lines"]
        for key in ("in_config_home", "user_skills", "hooks"):
            total[key] += bucket[key]
        for status, count in bucket["status"].items():
            total["status"][status] = total["status"].get(status, 0) + count
    return total


def _empty_report(state_dir: Path, absent: bool) -> Dict[str, Any]:
    return {
        "state_dir": str(state_dir), "sidecar": LEDGER, "sidecar_absent": absent,
        "store_project_id": None, "lines": 0, "status": {"ok": 0, "partial": 0},
        "foreign_project_lines": 0, "unreadable_lines": [], "receipt_hash_without_line": [],
        "receipts_unreadable": 0, "by_provider": {},
    }


def build_report(state_dir: Path, own_project: Optional[str] = None) -> Dict[str, Any]:
    """The report for one store. An absent sidecar is zero lines, not an error."""
    ledger = Path(state_dir) / LEDGER
    if not ledger.is_file():
        return _empty_report(Path(state_dir), absent=True)
    if own_project is None:
        from project_scope import resolve_stamp_project_id
        own_project = resolve_stamp_project_id(db_path=Path(state_dir) / RECEIPTS)
    lines, unreadable = read_sidecar(ledger)
    receipts = read_receipts(Path(state_dir) / RECEIPTS)
    folded = _fold_lines(lines, receipts["providers"], own_project)
    in_sidecar = {line["_sha256"] for line in lines}
    report = _empty_report(Path(state_dir), absent=False)
    total = _total_bucket(folded["buckets"])
    report.update({
        "store_project_id": own_project, "lines": total["lines"], "status": total["status"],
        "foreign_project_lines": folded["foreign"], "unreadable_lines": unreadable,
        "receipt_hash_without_line": sorted(d for d, sha in receipts["hashes"] if sha not in in_sidecar),
        "receipts_unreadable": receipts["unreadable"],
        "by_provider": {name: _finish_bucket(b) for name, b in sorted(folded["buckets"].items())},
    })
    return report


def _format_provider(name: str, bucket: Dict[str, Any]) -> str:
    status = " ".join(f"{k}={v}" for k, v in sorted(bucket["status"].items()))
    shares = " ".join(f"{k}={bucket[k]['lines']} ({bucket[k]['share']:.0%})"
                      for k in ("in_config_home", "user_skills", "hooks"))
    return f"  {name}: lines={bucket['lines']} | {status} | {shares}"


def format_text(report: Dict[str, Any]) -> str:
    """Plain-text rendering of ``build_report``."""
    if report["sidecar_absent"]:
        return f"context manifest sidecar absent: {report['state_dir']}/{LEDGER}\nlines: 0"
    status = " ".join(f"{k}={v}" for k, v in sorted(report["status"].items()))
    out = [f"state dir: {report['state_dir']}", f"store project_id: {report['store_project_id']}",
           f"lines: {report['lines']} ({status})",
           f"foreign_project_lines: {report['foreign_project_lines']}",
           "unreadable lines: " + (", ".join(str(n) for n in report["unreadable_lines"]) or "none"),
           "receipt hashes without sidecar line: "
           + (", ".join(report["receipt_hash_without_line"]) or "none"),
           "per provider:"]
    out.extend(_format_provider(n, b) for n, b in report["by_provider"].items())
    return "\n".join(out)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vnx context-manifest", description=__doc__.split("\n")[0])
    parser.add_argument("--json", action="store_true", help="one JSON object instead of text")
    parser.add_argument("--by", choices=["provider"], default="provider", help="grouping (provider)")
    parser.add_argument("--state-dir", help="state dir to read; default: the resolved project state dir")
    return parser


def _resolve_state_dir(explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    from vnx_paths import resolve_state_dir
    return resolve_state_dir()


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = build_report(_resolve_state_dir(args.state_dir))
    except Exception as exc:  # an unresolved store identity cannot be counted against: say so, exit 1
        print(f"context-manifest: cannot build report: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True) if args.json else format_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
