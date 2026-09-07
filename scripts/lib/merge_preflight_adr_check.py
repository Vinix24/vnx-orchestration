#!/usr/bin/env python3
"""Fail-closed ADR-number preflight for ``vnx pr-merge`` (Golf B, B6).

On 2026-09-06, #1790 and #1792 both landed a new ``ADR-038-*.md`` and
collided: both CI runs read a checkout of ``main`` that still ended at
ADR-037, because the VNX CI workflow runs on the PR's *merge ref* (not the
real, post-merge main) and ``required_status_checks.strict`` is off — so
neither run could see the other PR's number claim. A CI test on the merge-ref
diff can never catch this; only a check that runs at merge time, against the
REAL main, can.

This module answers one question: does an ADR file *added* (or renamed/copied
into a NEW number) by this PR reuse a number that already exists on the real
base branch (``main`` by default)? It never trusts a local ``origin/main``
ref (that is only as fresh as the last ``git fetch`` the door happened to
run, and the door itself never fetches) — both sides are read live via the
GitHub API:

  1. The PR's changed files, via
     ``gh api repos/{owner}/{repo}/pulls/<N>/files --paginate --slurp`` — this
     endpoint carries a ``status`` per file (``added``, ``modified``,
     ``removed``, ``renamed``, ``copied``, ...) plus a ``previous_filename``
     for renames/copies. A rename/copy whose OWN number changes (the ADR
     number embedded in ``previous_filename`` differs from the one in
     ``filename``) is treated the same as ``added`` — it claims a new number
     just as much as a brand-new file does. A rename/copy that keeps the same
     number (a wording fix) is not a collision. ``--paginate`` alone prints
     each page as a SEPARATE JSON document back-to-back (confirmed via
     ``gh api --help``: "Each page is a separate JSON array or object"), which
     is not valid JSON once a PR has more files than one page (30-100
     depending on the endpoint) — ``--slurp`` wraps all pages into a single
     outer JSON array of pages, which this module flattens.
  2. The base branch's ADR directory listing, via
     ``gh api repos/{owner}/{repo}/contents/docs/governance/decisions?ref=<base_ref>``
     — the live tree at the tip of ``base_ref`` (default ``main``, override
     with the PR's actual ``baseRefName`` when known), not a cached/local ref.

ADR-007 (composite ``project_id`` key on the central ``adrs`` DB table) does
NOT apply here: this check never touches that table. Two projects may both
have an "ADR-001" in the central DB by design (composite PK over
``project_id``); this module only ever compares filenames within ONE repo's
``docs/governance/decisions/`` tree, where ADR numbers are meant to be
globally unique regardless of project_id.

Fail-closed: any unreadable/unparseable API response (``gh`` missing, not
authenticated, a non-zero exit, invalid JSON, an unexpected shape) is a NO-GO
with its own message — never a silent pass. There is no override for this
check (unlike the CI/review gates): a colliding ADR number is always wrong,
never a legitimate reason to bypass.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

# Matches an ADR file's repo-relative path, e.g.
# "docs/governance/decisions/ADR-038-receipt-outcome-identity.md".
ADR_PATH_RE = re.compile(r"^docs/governance/decisions/ADR-(\d+)-[^/]+\.md$")

# Matches a bare ADR filename (used against the contents-API directory
# listing, which returns names, not full paths).
ADR_FILENAME_RE = re.compile(r"^ADR-(\d+)-[^/]+\.md$")

ADR_DECISIONS_DIR = "docs/governance/decisions"

GH_PR_FILES_TIMEOUT = 20
GH_CONTENTS_TIMEOUT = 20


def _capture(
    argv: List[str], *, timeout: int, cwd: Optional[str] = None
) -> Tuple[Optional["subprocess.CompletedProcess[str]"], Optional[str]]:
    """Run a command and return (result, error_tag).

    error_tag is one of "missing" (binary not found), "timeout", or None. A
    non-zero exit is NOT an error_tag: the caller inspects ``returncode``.
    """
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return result, None
    except FileNotFoundError:
        return None, "missing"
    except subprocess.TimeoutExpired:
        return None, "timeout"


def _no_go(message: str, **extra: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "verdict": "NO-GO",
        "message": message,
        "colliding_number": None,
        "pr_file": None,
        "main_file": None,
    }
    base.update(extra)
    return base


def _go(message: str, **extra: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "verdict": "GO",
        "message": message,
        "colliding_number": None,
        "pr_file": None,
        "main_file": None,
    }
    base.update(extra)
    return base


STATUSES_THAT_CAN_CLAIM_A_NEW_NUMBER = frozenset({"added", "renamed", "copied"})


def get_pr_added_adr_files(
    pr_number: int, *, gh_bin: str = "gh", project_root: Optional[Path] = None
) -> Tuple[Optional[Dict[str, str]], Optional[Dict[str, Any]]]:
    """Return ({number: path}, None) for ADR files ADDED (or renamed/copied
    into a NEW number) by this PR, or (None, no_go).

    A plain ``"modified"`` status is never a new-number claim and is excluded.
    ``"renamed"``/``"copied"`` are ambiguous on their own: they cover both "I
    reworded ADR-038" (same number before and after — not a claim) and "I
    renamed ADR-031 into ADR-038" (claims 038 exactly as much as a brand-new
    ``ADR-038-*.md`` would). The two are told apart by comparing the ADR
    number in ``filename`` against the one in ``previous_filename`` — only a
    CHANGED number counts.
    """
    result, err = _capture(
        [
            gh_bin,
            "api",
            f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/files",
            "--paginate",
            "--slurp",
        ],
        timeout=GH_PR_FILES_TIMEOUT,
        cwd=str(project_root) if project_root else None,
    )
    if err == "missing":
        return None, _no_go("gh CLI niet beschikbaar: ADR-preflight is niet toetsbaar")
    if err == "timeout":
        return None, _no_go(
            f"gh api pulls/{pr_number}/files liep vast: ADR-preflight is niet toetsbaar"
        )
    if result is None or result.returncode != 0:
        # Fail-closed also catches an old gh binary that does not know
        # --slurp yet (non-zero exit, "unknown flag" on stderr): there is no
        # silent fallback to unpaginated/unslurped output here.
        stderr = (result.stderr if result else "").strip()
        return None, _no_go(
            f"gh api pulls/{pr_number}/files faalde: ADR-preflight is niet toetsbaar"
            + (f" ({stderr[:160]})" if stderr else "")
        )
    try:
        pages = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        # Most likely cause: gh's --paginate prints one JSON document PER
        # PAGE back-to-back without --slurp, which is not valid JSON as a
        # whole once there is more than one page.
        return None, _no_go(
            f"gh-uitvoer voor pulls/{pr_number}/files niet te parsen als geslurpte "
            f"paginering (elke pagina moet een JSON-array binnen de buitenste "
            f"array zijn): ADR-preflight is niet toetsbaar ({exc})"
        )
    if not isinstance(pages, list):
        return None, _no_go(
            f"onverwacht antwoordformaat voor pulls/{pr_number}/files: "
            "ADR-preflight is niet toetsbaar"
        )

    files: List[Any] = []
    for page in pages:
        if not isinstance(page, list):
            return None, _no_go(
                f"onverwacht antwoordformaat voor pulls/{pr_number}/files "
                "(geslurpte pagina is geen array): ADR-preflight is niet toetsbaar"
            )
        files.extend(page)

    added: Dict[str, str] = {}
    for entry in files:
        if not isinstance(entry, dict):
            continue
        status = entry.get("status")
        if status not in STATUSES_THAT_CAN_CLAIM_A_NEW_NUMBER:
            continue
        path = entry.get("filename") or ""
        m = ADR_PATH_RE.match(path)
        if not m:
            continue
        number = str(int(m.group(1)))
        if status in ("renamed", "copied"):
            prev_m = ADR_PATH_RE.match(entry.get("previous_filename") or "")
            if prev_m and str(int(prev_m.group(1))) == number:
                # Same ADR number before and after: a wording/rename fix on
                # the PR's own claim, not a new-number claim.
                continue
        added[number] = path
    return added, None


def get_main_adr_numbers(
    *, gh_bin: str = "gh", project_root: Optional[Path] = None, base_ref: str = "main"
) -> Tuple[Optional[Dict[str, str]], Optional[Dict[str, Any]]]:
    """Return ({number: filename}, None) for ADR files on ``base_ref``, or (None, no_go).

    Reads the live directory listing via the contents API scoped to
    ``ref=<base_ref>`` — never a local ``origin/<base_ref>`` ref, which the
    door never freshens (see module docstring). Defaults to ``"main"``, the
    base every VNX PR targets today; pass the PR's actual ``baseRefName`` when
    it is known so a PR targeting something other than main is compared
    against its real base instead of silently assuming main.
    """
    encoded_base_ref = quote(base_ref, safe="")
    result, err = _capture(
        [
            gh_bin,
            "api",
            f"repos/{{owner}}/{{repo}}/contents/{ADR_DECISIONS_DIR}?ref={encoded_base_ref}",
        ],
        timeout=GH_CONTENTS_TIMEOUT,
        cwd=str(project_root) if project_root else None,
    )
    if err == "missing":
        return None, _no_go(
            "gh CLI niet beschikbaar: ADR-preflight is niet toetsbaar (base-ref-listing)"
        )
    if err == "timeout":
        return None, _no_go(
            "gh api contents-listing liep vast: ADR-preflight is niet toetsbaar (base-ref-listing)"
        )
    if result is None or result.returncode != 0:
        stderr = (result.stderr if result else "").strip()
        return None, _no_go(
            f"gh api contents-listing van {ADR_DECISIONS_DIR} op {base_ref} faalde: "
            "ADR-preflight is niet toetsbaar" + (f" ({stderr[:160]})" if stderr else "")
        )
    try:
        entries = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return None, _no_go(
            f"gh-uitvoer voor de main-listing niet te parsen: "
            f"ADR-preflight is niet toetsbaar ({exc})"
        )
    if not isinstance(entries, list):
        return None, _no_go(
            "onverwacht antwoordformaat voor de main-listing: ADR-preflight is niet toetsbaar"
        )

    numbers: Dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "file":
            continue
        name = entry.get("name") or ""
        m = ADR_FILENAME_RE.match(name)
        if m:
            numbers[str(int(m.group(1)))] = name
    return numbers, None


def check_adr_numbers_for_pr(
    pr_number: int,
    *,
    gh_bin: str = "gh",
    project_root: Optional[Path] = None,
    base_ref: str = "main",
) -> Dict[str, Any]:
    """Fail-closed: an ADR file added by this PR must not reuse a number
    already present on the real base branch (``main`` by default).

    Returns ``{"verdict": "GO"|"NO-GO", "message": str, ...}``. No new ADR
    files added by the PR is a GO (nothing to check). There is no override —
    a colliding number is always refused. Pass the PR's actual ``baseRefName``
    as ``base_ref`` when known; a PR targeting something other than main is
    otherwise silently compared against main's numbers instead of its own base.
    """
    if shutil.which(gh_bin) is None:
        return _no_go("gh CLI niet beschikbaar: ADR-preflight is niet toetsbaar")

    pr_adrs, err = get_pr_added_adr_files(pr_number, gh_bin=gh_bin, project_root=project_root)
    if err is not None:
        return err
    assert pr_adrs is not None  # narrows for type-checkers: err is None -> value set

    if not pr_adrs:
        return _go("Geen nieuwe ADR-bestanden in deze PR: geen ADR-nummerbotsing mogelijk")

    main_numbers, err = get_main_adr_numbers(
        gh_bin=gh_bin, project_root=project_root, base_ref=base_ref
    )
    if err is not None:
        return err
    assert main_numbers is not None

    for number in sorted(pr_adrs, key=int):
        if number in main_numbers:
            pr_file = pr_adrs[number]
            main_file = main_numbers[number]
            padded = f"{int(number):03d}"
            return _no_go(
                f"ADR-{padded} botst: deze PR voegt '{pr_file}' toe, maar ADR-{padded} "
                f"staat al op {base_ref} als '{main_file}'. Kies een vrij ADR-nummer.",
                colliding_number=number,
                pr_file=pr_file,
                main_file=main_file,
            )

    return _go(
        f"Geen ADR-nummerbotsing: {len(pr_adrs)} nieuw(e) ADR-bestand(en) getoetst tegen {base_ref}"
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="merge_preflight_adr_check",
        description="Fail-closed check: does an added ADR file collide with a number already on main?",
    )
    parser.add_argument("--pr", type=int, required=True, help="GitHub PR number")
    parser.add_argument("--project-root", default=".", help="Directory whose PR is being checked")
    parser.add_argument("--gh-bin", default="gh", help="Path/name of the gh binary")
    parser.add_argument("--json", action="store_true", help="Emit the full result as JSON")
    args = parser.parse_args(argv)

    result = check_adr_numbers_for_pr(
        args.pr, gh_bin=args.gh_bin, project_root=Path(args.project_root)
    )

    if args.json:
        print(json.dumps(result))
    else:
        print(result["message"])
    return 0 if result["verdict"] == "GO" else 1


if __name__ == "__main__":
    sys.exit(main())
