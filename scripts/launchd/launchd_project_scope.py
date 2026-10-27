#!/usr/bin/env python3
"""launchd_project_scope.py — per-project launchd label guard (OI-1509/OI-1510).

Measured live on this machine while building this module (2026-09-04,
``launchctl list | grep vnx``): ``com.vnx.gate-obligation-runner`` (bare, no
project suffix — mission-control's job) and
``com.vnx.gate-obligation-runner.vnx-dev`` (a hand-installed workaround, never
produced by any script in this repo) were BOTH loaded at once. That bare
label is the collision-enabling condition OI-1509/OI-1510 describe: launchd
itself refuses to run two DIFFERENT jobs under one Label at the same time, so
a genuine collision never shows up as two simultaneous entries in
``launchctl list`` — it shows up as the SECOND install's ``launchctl unload``
silently tearing down the FIRST project's job before overwriting the shared
destination file. The bare label is what makes that possible; a per-project
suffix is what makes it impossible. This module checks two properties, one
static and one live:

  1. ``check_template_contract`` — every daemon family this repo requires to
     be per-project (``REQUIRED_PER_PROJECT_FAMILIES`` below) must ship a
     template under ``scripts/launchd/`` whose ``Label`` carries the
     ``${VNX_PROJECT_ID}`` placeholder. Deterministic, no host dependency.

  2. ``check_installed_state`` — given a ``launchctl list`` snapshot (always
     injected by the caller, never read directly by the functions below) and
     this project's id, every required family must have a loaded instance
     named ``<family>.<project_id>``, and no loaded label for that family may
     be bare or carry a malformed (non-project-id-shaped) suffix.

  3. ``check_pin_alignment`` — every INSTALLED plist of a registered, pinned
     project must run that project's pin. Measured 2026-10-25: 0 of the 6 such
     jobs followed their pin (three on ``current``, three on
     ``versions/v1.7.1``), and ``check_installed_state`` reported ``ok``
     because it only looks at Labels. A job follows the pin only when its
     ``VNX_HOME`` exists and RESOLVES to the installed pinned release
     directory: a matching basename elsewhere, a missing path and a
     same-named symlink into another release are all reported. The
     ``VNX_HOME`` a job runs with is the one its ``ProgramArguments`` assign
     (the assignment overrides ``EnvironmentVariables`` for the exec'd
     process); an assignment that cannot be read statically is reported
     ``pin_unmeasurable`` instead of trusting the environment.

``main()`` is the live, read-only operator/CI entry point: it resolves this
project's id (``vnx_paths.resolve_project_id()``), shells out to the real
``launchctl list`` (never ``load``/``unload``/``bootstrap`` — this module
installs nothing), judges the installed plists against the project pins, and
reports violations with a non-zero exit code. It never writes to
``~/Library/LaunchAgents`` or any state store.
"""
from __future__ import annotations

import argparse
import json
import plistlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from xml.parsers.expat import ExpatError

_SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "lib"
if str(_SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_LIB))

# The engine root carries ``scripts/`` and, in a dev checkout, ``vnx_cli/``; a
# packaged install ships the engine trees under ``vnx_orchestration/`` next to
# ``vnx_cli/``, one level higher. Same bootstrap as ``scripts/lib/pin_drift.py``
# (the module whose pin reader this one imports): whichever sibling holds
# ``_reexec.py`` goes on ``sys.path``, and the file check keeps an unrelated
# ``vnx_cli`` off it.
_ENGINE_ROOT = Path(__file__).resolve().parent.parents[1]
for _root in (_ENGINE_ROOT, _ENGINE_ROOT.parent):
    if (_root / "vnx_cli" / "_reexec.py").is_file():
        if str(_root) not in sys.path:
            sys.path.insert(0, str(_root))
        _ENGINE_ROOT = _root
        break

from job_exit_capture import _parse_launchctl_list  # noqa: E402
import pin_drift  # noqa: E402
import vnx_paths  # noqa: E402
from vnx_cli._reexec import _read_pin, _versions_dir  # noqa: E402

# Mirrors PROJECT_ID_RE / _vnx_valid_project_id (scripts/lib/vnx_paths.py,
# scripts/lib/vnx_paths.sh) — kept as a small, deliberate duplicate here,
# same lockstep-mirror pattern vnx_paths.sh itself documents for its own
# py/sh duplication.
PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")

# The daemon families that must be per-project. OI-1509/OI-1510 named the first
# two; OI-1629 deel c adds the reviewed-worktree cleaner, which must run per
# project (each project's .vnx-data/worktrees/ is its own) and was measured
# hard-targeted at a single repo via its old mission-control launchd job. Not
# derived dynamically from whatever templates happen to exist under
# scripts/launchd/: most templates in that directory (conversation-analyzer,
# dashboard-adjacent jobs, etc.) are deliberately single-instance-per-machine,
# not per-project, and must NOT be flagged just for lacking a
# ${VNX_PROJECT_ID} placeholder.
REQUIRED_PER_PROJECT_FAMILIES: tuple = (
    "com.vnx.gate-obligation-runner",
    "com.vnx.receipt-processor",
    "com.vnx.cleanup-reviewed-worktrees",
    "com.vnx.ledger-health",
)

# Templates that use ${VNX_PROJECT_ID} in their body but deliberately keep a bare
# Label: one job per machine by documented decision, never installed by `vnx init`.
# Every other template that mentions the placeholder must carry it in its Label
# (OI-1942: ledger-health used it in its environment with a bare Label, so every
# project's install replaced the previous project's job). Each entry names its reason.
MACHINE_WIDE_TEMPLATES: Dict[str, str] = {
    "com.vnx.subsystem-probe": "one probe per machine; it reads the project id only to locate the store it reports on",
    "com.vnx.fleet-role-drift": "one drift check per machine across the registered fleet",
    "com.vnx.fleet-pin-audit": "one pin audit per machine across the scanned development tree",
}

_PLACEHOLDER = "${VNX_PROJECT_ID}"


def _read_template_label(plist_path: Path) -> Optional[str]:
    """Read a template's raw (pre-substitution) Label. None on any read/parse
    failure — the caller turns that into its own, more specific violation."""
    try:
        with open(plist_path, "rb") as fh:
            data = plistlib.load(fh)
    except (plistlib.InvalidFileException, ExpatError, OSError, ValueError):
        # vnx-silent-except: unreadable/malformed template surfaces as a
        # template_unreadable violation in check_template_contract, not a crash
        return None
    label = data.get("Label")
    return label if isinstance(label, str) else None


def _check_family_template(
    templates_dir: Path, family: str
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Return ``(violations, checked entry)`` for one required family's template."""
    template_path = templates_dir / f"{family}.plist"
    if not template_path.is_file():
        violation = {
            "family": family,
            "kind": "template_missing",
            "detail": f"no template at {template_path}",
        }
        return [violation], {"family": family, "template": str(template_path), "label": None}

    label = _read_template_label(template_path)
    entry = {"family": family, "template": str(template_path), "label": label}

    if label is None:
        violation = {
            "family": family,
            "kind": "template_unreadable",
            "detail": f"could not read a Label out of {template_path}",
        }
        return [violation], entry

    if _PLACEHOLDER not in label:
        violation = {
            "family": family,
            "kind": "label_not_project_scoped",
            "detail": (
                f"{template_path.name} Label={label!r} does not contain "
                f"{_PLACEHOLDER}; two projects installing this template "
                "would resolve to the SAME launchd Label and collide "
                "(OI-1509/OI-1510)"
            ),
        }
        return [violation], entry
    return [], entry


def _scan_unlisted_templates(
    templates_dir: Path, families: Sequence[str]
) -> List[Dict[str, Any]]:
    """Return violations for templates outside ``families`` and ``MACHINE_WIDE_TEMPLATES``
    that use the placeholder without carrying it in their Label.
    """
    violations: List[Dict[str, Any]] = []
    for template_path in sorted(templates_dir.glob("*.plist")):
        family = template_path.stem
        if family in families or family in MACHINE_WIDE_TEMPLATES:
            continue
        try:
            raw = template_path.read_text(encoding="utf-8")
        except OSError:
            # vnx-silent-except: an unreadable non-family template is reported as unreadable below
            violations.append(
                {
                    "family": family,
                    "kind": "template_unreadable",
                    "detail": f"could not read {template_path}",
                }
            )
            continue
        if _PLACEHOLDER not in raw:
            continue
        label = _read_template_label(template_path)
        if label is None or _PLACEHOLDER not in label:
            violations.append(
                {
                    "family": family,
                    "kind": "label_not_project_scoped",
                    "detail": (
                        f"{template_path.name} uses {_PLACEHOLDER} but its Label={label!r} does not "
                        "carry it; every install of this template would replace the previous "
                        "project's job (OI-1942). Put the placeholder in the Label, or list the "
                        "template in MACHINE_WIDE_TEMPLATES with a reason"
                    ),
                }
            )
    return violations


def check_template_contract(
    templates_dir: Path,
    families: Sequence[str] = REQUIRED_PER_PROJECT_FAMILIES,
) -> Dict[str, Any]:
    """Static contract check: no host, no launchctl, no environment — every
    required family's template must exist under ``templates_dir`` and its
    ``Label`` must contain the ``${VNX_PROJECT_ID}`` placeholder.
    """
    templates_dir = Path(templates_dir)
    violations: List[Dict[str, Any]] = []
    checked: List[Dict[str, Any]] = []

    for family in families:
        family_violations, entry = _check_family_template(templates_dir, family)
        violations.extend(family_violations)
        checked.append(entry)

    violations.extend(_scan_unlisted_templates(templates_dir, families))

    return {"ok": not violations, "violations": violations, "checked": checked}


def check_installed_state(
    launchctl_output: str,
    project_id: str,
    families: Sequence[str] = REQUIRED_PER_PROJECT_FAMILIES,
) -> Dict[str, Any]:
    """Behavioral check against an injected ``launchctl list`` snapshot.

    A live collision (two projects fighting over one Label) cannot be
    observed as two simultaneous entries — launchd itself prevents that, the
    second ``load`` unloads the first. What IS observable, and is exactly the
    precondition that makes a future collision possible, is a bare
    (non-project-scoped) or malformed label still loaded for a family this
    project requires to be project-scoped. That, plus this project's own
    instance being absent, are the two failure modes checked here.
    """
    if not PROJECT_ID_RE.match(project_id):
        return {
            "ok": False,
            "violations": [
                {
                    "family": None,
                    "kind": "invalid_project_id",
                    "detail": f"project_id {project_id!r} does not match {PROJECT_ID_RE.pattern}",
                }
            ],
            "loaded_labels": [],
        }

    jobs = _parse_launchctl_list(launchctl_output)
    loaded_labels = [job["label"] for job in jobs]
    loaded_set = set(loaded_labels)

    violations: List[Dict[str, Any]] = []
    for family in families:
        expected_label = f"{family}.{project_id}"
        if expected_label not in loaded_set:
            violations.append(
                {
                    "family": family,
                    "kind": "missing_instance",
                    "detail": (
                        f"no loaded launchd job named {expected_label!r} — "
                        f"project {project_id!r} has no instance of this daemon"
                    ),
                }
            )

        for label in loaded_labels:
            if label == family:
                violations.append(
                    {
                        "family": family,
                        "kind": "non_per_project_label",
                        "detail": (
                            f"{label!r} is loaded bare (no project suffix) — "
                            f"collision risk for {family}"
                        ),
                    }
                )
                continue
            if label.startswith(family + "."):
                suffix = label[len(family) + 1 :]
                if not PROJECT_ID_RE.match(suffix):
                    violations.append(
                        {
                            "family": family,
                            "kind": "malformed_label",
                            "detail": f"{label!r} suffix {suffix!r} is not a valid project id",
                        }
                    )

    return {"ok": not violations, "violations": violations, "loaded_labels": loaded_labels}


def check_project_scope(
    templates_dir: Path,
    project_id: str,
    launchctl_output: str,
    families: Sequence[str] = REQUIRED_PER_PROJECT_FAMILIES,
) -> Dict[str, Any]:
    """Combine the static template contract and the live installed-state
    checks. Both an injected ``launchctl_output`` and an explicit
    ``project_id`` are required — this function never reads the host's real
    launchd state or environment itself (that boundary lives in ``main()``
    only), so it is fully deterministic under test.
    """
    template_check = check_template_contract(templates_dir, families)
    installed_check = check_installed_state(launchctl_output, project_id, families)
    violations = template_check["violations"] + installed_check["violations"]
    return {
        "ok": template_check["ok"] and installed_check["ok"],
        "violations": violations,
        "template_check": template_check,
        "installed_check": installed_check,
    }


# ---------------------------------------------------------------------------
# Pin alignment -- does an installed job run the release its project pins?
# Read-only: pins come from ``vnx_cli._reexec._read_pin`` (the reader
# ``fleet_pin_audit`` uses, imported not copied) and the pinned dir from
# ``pin_drift.classify_pin``. Nothing is installed, loaded, unloaded or written.
# ---------------------------------------------------------------------------

PIN_FOLLOWED = "pin_followed"
PIN_IGNORED_CURRENT = "pin_ignored_current"
PIN_IGNORED_OTHER_VERSION = "pin_ignored_other_version"
PIN_DEAD = "pin_dead"
# The job assigns VNX_HOME on its own command line in a way that cannot be read
# without running the shell (another variable, a command substitution, an empty
# value, two conflicting literals). Its release is unmeasured -- a violation,
# never ``pin_followed`` (OI-2415).
PIN_UNMEASURABLE = "pin_unmeasurable"

# ``VNX_HOME=<value>`` inside a ProgramArguments string: the bare
# gate-obligation-runner renders exactly this instead of an env block. Such an
# assignment is ALSO the highest-precedence source: in
# ``bash -c 'VNX_HOME=/store/current; exec python3 runner.py'`` the assignment
# overrides the plist's EnvironmentVariables for the process that execs the
# runner, so it is read FIRST and only then the environment, never the other
# way round (OI-2415). ``export``/``env VNX_HOME=...``/``&&``/``||``/subshell
# forms all match because the separator class covers whitespace and shell
# punctuation.
_VNX_HOME_ASSIGN_RE = re.compile(r"(?:^|[\s;&|()])VNX_HOME=")

# The only expansions a statically-read assignment may carry: the bare
# obligation-runner template writes ``VNX_HOME="$HOME/.vnx-system/current"``
# and ``_expand_plist_path`` resolves exactly these two tokens. Anything else
# (another variable, ``$()``, backticks) leaves the release unknowable without
# running the shell, so the job is reported unmeasurable rather than judged.
_HOME_EXPANSION_TOKENS = ("${HOME}", "$HOME")


def _unreadable_assignment_reason(raw: str) -> Optional[str]:
    """Why a command-line ``VNX_HOME=`` value cannot be read with certainty, or
    None when it is a literal path (``$HOME``/``${HOME}`` excepted)."""
    remainder = raw
    for token in _HOME_EXPANSION_TOKENS:
        remainder = remainder.replace(token, "")
    if "`" in remainder or "$(" in remainder:
        return "a command substitution"
    if "${" in remainder or re.search(r"\$[A-Za-z_0-9@*#?]", remainder):
        return "an unresolved variable expansion"
    return None


def _assignment_value(text: str, start: int) -> Tuple[str, Optional[str], int]:
    """Read the value of a ``VNX_HOME=`` assignment whose value begins at
    ``start`` in ``text``.

    Returns ``(value, unreadable_reason, end)``. ``value`` is the literal text
    as written (quotes stripped); ``end`` is where the value stops, so the
    caller keeps scanning the rest of the same argument. A value that is a
    command substitution, an unterminated quote or an empty token cannot be
    read statically -- the caller turns that into ``pin_unmeasurable``.
    """
    tail = text[start:]
    if tail.startswith("$("):
        return "", "a command substitution", start + 2
    if tail.startswith("`"):
        return "", "a command substitution", start + 1
    for quote in ('"', "'"):
        if tail.startswith(quote):
            close = text.find(quote, start + 1)
            if close == -1:
                return "", f"an unterminated {quote} quote", len(text)
            return text[start + 1:close], None, close + 1
    token = re.match(r"[^\s;&|()]*", tail).group(0)
    if token.endswith("$") and text.startswith("(", start + len(token)):
        return "", "a command substitution", start + len(token) + 1
    return token, None, start + len(token)


def _command_vnx_home(payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """``(value, unreadable_reason)`` for a ``VNX_HOME=`` assignment made by the
    job's own command line (``ProgramArguments``).

    ``(None, None)`` when the command assigns VNX_HOME nowhere; ``(None, reason)``
    when it assigns a value that cannot be read statically. Several DIFFERENT
    literals are unmeasurable too: which one the runner's own command inherits
    depends on the command sequence, which this reader does not model. Repeats
    of the same literal are harmless and still measured.
    """
    argv = payload.get("ProgramArguments")
    literals: List[str] = []
    unreadable: Optional[str] = None
    for argument in argv if isinstance(argv, list) else ():
        if not isinstance(argument, str):
            continue
        position = 0
        while True:
            match = _VNX_HOME_ASSIGN_RE.search(argument, position)
            if match is None:
                break
            raw, reason, end = _assignment_value(argument, match.end())
            position = max(end, match.end() + 1)  # empty value: still move on
            if reason is not None:
                unreadable = unreadable or reason
                continue
            raw = raw.strip()
            if not raw:
                unreadable = unreadable or "an empty value"
                continue
            reason = _unreadable_assignment_reason(raw)
            if reason is not None:
                unreadable = unreadable or f"{reason} ({raw!r})"
            elif raw not in literals:
                literals.append(raw)
    if unreadable is not None:
        return None, unreadable
    if len(literals) > 1:
        return None, "conflicting assignments ({})".format(", ".join(repr(v) for v in literals))
    if literals:
        return literals[0], None
    return None, None


def _expand_plist_path(value: Optional[str]) -> Optional[Path]:
    """VNX_HOME as a path WITHOUT resolving symlinks -- ``current`` must survive
    as the final component. None when empty, unrendered or unusable."""
    text = (value or "").strip()
    if not text or _PLACEHOLDER in text:
        return None
    for token in ("${HOME}", "$HOME"):
        text = text.replace(token, str(Path.home()))
    try:
        return Path(text).expanduser()
    except (OSError, RuntimeError):
        return None


def _plist_vnx_home(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The VNX_HOME the job actually runs with: ``value``, where it came from
    (``source`` = ``"command"``/``"environment"``/None) and -- when the command
    line assigns it unreadably -- the ``unreadable`` reason.

    A ``VNX_HOME=`` assignment in ``ProgramArguments`` WINS over
    ``EnvironmentVariables``: the assignment is what the shell that execs the
    runner exports, so a job whose environment names the pinned release but
    whose command says ``VNX_HOME=/store/current`` runs ``current``. Only when
    the command assigns none at all does the environment's value count. A
    command assignment that cannot be read statically is never replaced by the
    environment value either: that would report the pin the job may well not
    run (OI-2415).
    """
    env = payload.get("EnvironmentVariables")
    environment = env.get("VNX_HOME") if isinstance(env, dict) else None
    environment = environment.strip() if isinstance(environment, str) and environment.strip() else None
    command_value, unreadable = _command_vnx_home(payload)
    if unreadable is not None:
        return {"value": None, "source": "command", "unreadable": unreadable,
                "environment": environment}
    if command_value is not None:
        return {"value": command_value, "source": "command", "unreadable": None,
                "environment": environment}
    return {"value": environment, "source": "environment" if environment else None,
            "unreadable": None, "environment": environment}


def _attributed_project(
    label: str, payload: Optional[Dict[str, Any]], projects: Mapping[str, Path]
) -> Optional[str]:
    """The registered project a plist serves, or None. ``VNX_PROJECT_ID`` wins
    (the bare obligation-runner has no project suffix), else the Label's last
    component -- which is also the filename, so an unreadable plist still counts."""
    env = payload.get("EnvironmentVariables") if payload is not None else None
    declared = env.get("VNX_PROJECT_ID") if isinstance(env, dict) else None
    if isinstance(declared, str) and declared.strip() in projects:
        return declared.strip()
    suffix = label.rsplit(".", 1)[-1] if "." in label else label
    return suffix if suffix in projects else None


def _classify_home(vnx_home: Optional[str], pin: str, pin_record: Dict[str, Any]) -> Tuple[str, str]:
    """Classify one installed job's VNX_HOME against its project's pin.

    ``pin_followed`` requires the job's directory to BE the installed release
    the pin names: the home must exist and ``resolve()`` to
    ``pin_record['release_dir']`` (the resolved installed pin dir, computed by
    ``check_pin_alignment``). A name-only match is a false positive -- a
    ``VNX_HOME`` of ``/nonexistent/v1.7.5`` or a ``v1.7.5`` symlink into
    ``v1.7.4`` passes such a comparison while the job runs no such install.
    Order: ``pin_dead``, the unusable home, the explicit ``current`` rejection
    (decided on the name AS WRITTEN, before resolving, so a ``current`` link
    that happens to point at the pinned release is still reported), then the
    directory comparison. Everything unmeasured stays a violation.
    """
    if pin_record["verdict"] in (pin_drift.VERDICT_DEAD, pin_drift.VERDICT_NON_RELEASE):
        return PIN_DEAD, (
            f"pin {pin!r} of {pin_record['root']} names no installed release dir "
            f"(store verdict {pin_record['verdict']!r}); this job cannot follow it"
        )
    home_path = _expand_plist_path(vnx_home)
    if home_path is None:
        return PIN_IGNORED_OTHER_VERSION, (
            f"VNX_HOME {vnx_home!r} names no usable release directory, so this job "
            f"cannot run the pinned release {pin!r}"
        )
    if home_path.name == "current":
        return PIN_IGNORED_CURRENT, (
            f"VNX_HOME {vnx_home!r} follows the rolling 'current' link while the project "
            f"pins {pin!r}; a later flip silently changes the version this job runs"
        )
    pinned_dir = pin_record.get("release_dir")
    if not pinned_dir:
        # No measured installed dir for a pin the store calls usable: that is
        # an unmeasured job, never a followed pin (fail closed).
        return PIN_IGNORED_OTHER_VERSION, (
            f"no installed release directory was measured for pin {pin!r}, so VNX_HOME "
            f"{vnx_home!r} cannot be shown to run it"
        )
    try:
        present = home_path.is_dir()
    except OSError:
        present = False
    if not present:
        return PIN_IGNORED_OTHER_VERSION, (
            f"VNX_HOME {str(home_path)!r} names no existing directory, so this job cannot "
            f"run the pinned release {pin!r} installed at {str(pinned_dir)!r}"
        )
    try:
        resolved = home_path.resolve()
    except (OSError, RuntimeError) as exc:
        return PIN_IGNORED_OTHER_VERSION, (
            f"VNX_HOME {vnx_home!r} cannot be resolved ({type(exc).__name__}: {exc}), so this "
            f"job cannot be measured against the pinned release {pin!r}"
        )
    if resolved == Path(str(pinned_dir)):
        return PIN_FOLLOWED, (
            f"VNX_HOME {vnx_home!r} resolves to the pinned release {pin!r} at {str(resolved)!r}"
        )
    return PIN_IGNORED_OTHER_VERSION, (
        f"VNX_HOME {vnx_home!r} resolves to {str(resolved)!r}, not the pinned release "
        f"{pin!r} installed at {str(pinned_dir)!r}"
    )


def check_pin_alignment(
    plist_dir: Path,
    projects: Mapping[str, Path],
    versions_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Judge every installed VNX plist against the pin of the project it serves.

    ``plist_dir`` holds INSTALLED plists (``~/Library/LaunchAgents`` live, a
    fixture under test); ``projects`` maps project id to project root, whose
    ``.vnx-version`` is read; ``versions_dir`` is the store's ``versions/`` (the
    engine's own by default, injectable for callers/tests).

    Each job is ``pin_followed`` (its ``VNX_HOME`` exists and resolves to the
    installed release dir the pin names), ``pin_ignored_current`` (it follows
    the rolling ``current`` link while the project pins a version),
    ``pin_ignored_other_version`` (another release, a missing directory, or no
    release at all), ``pin_dead`` (the pinned release dir does not exist) or
    ``pin_unmeasurable`` (the job's own command line assigns ``VNX_HOME``
    through a variable, a command substitution or conflicting literals, so no
    release can be measured -- the command overrides the environment, which is
    therefore never consulted to fill that gap).
    Everything but ``pin_followed`` is a violation -- an unreadable or
    unmeasured job is never reported as following -- except a plist of a
    project without a pin (a dev checkout), which is listed under
    ``unmeasured``. Pure read.
    """
    plist_dir = Path(plist_dir)
    versions_dir = Path(versions_dir) if versions_dir is not None else _versions_dir(_ENGINE_ROOT)
    roots = {project_id: Path(root).expanduser() for project_id, root in projects.items()}
    entries: List[Dict[str, Any]] = []
    violations: List[Dict[str, Any]] = []
    unmeasured: List[Dict[str, Any]] = []
    pins: Dict[str, Dict[str, Any]] = {}

    for plist_path in sorted(plist_dir.glob("*.plist")):
        if not plist_path.name.startswith("com.vnx."):
            continue  # not a VNX-installed agent
        try:
            with open(plist_path, "rb") as handle:
                payload = plistlib.load(handle)
            if not isinstance(payload, dict):
                raise ValueError(f"top-level value is {type(payload).__name__}")
            read_error = None
        except (plistlib.InvalidFileException, ExpatError, OSError, ValueError) as exc:
            payload, read_error = None, f"{type(exc).__name__}: {exc}"
        raw_label = payload.get("Label") if payload is not None else None
        label = raw_label if isinstance(raw_label, str) and raw_label else plist_path.stem
        project_id = _attributed_project(label, payload, roots)
        if project_id is None:
            unmeasured.append({"plist": str(plist_path), "label": label, "project_id": None,
                               "reason": read_error or "label names no registered project"})
            continue

        if project_id not in pins:
            pin = _read_pin(roots[project_id])[0]
            # ``release_dir`` is the RESOLVED installed dir the pin names (None
            # when the store has none) -- the value _classify_home compares a
            # job's resolved VNX_HOME against, so a same-named path elsewhere
            # never counts as following.
            pinned_dir = pin_drift.pinned_release_dir(versions_dir, pin) if pin else None
            pins[project_id] = {"root": str(roots[project_id]), "pin": pin,
                                "release_dir": str(pinned_dir) if pinned_dir is not None else None,
                                "verdict": pin_drift.classify_pin(pin, versions_dir.parent).verdict if pin else None}
        pin_record, pin = pins[project_id], pins[project_id]["pin"]

        if pin is None:
            unmeasured.append({"plist": str(plist_path), "label": label, "project_id": project_id,
                               "reason": read_error or f"no .vnx-version pin under {pin_record['root']}"})
            continue
        if read_error is not None:
            violations.append({
                "kind": "pin_unreadable", "label": label, "project_id": project_id,
                "plist": str(plist_path),
                "detail": (f"cannot read {plist_path.name} ({read_error}) while project "
                           f"{project_id!r} pins {pin!r}; the job's version is unmeasured"),
            })
            continue

        home = _plist_vnx_home(payload)
        if home["unreadable"] is not None:
            environment_note = (
                f" while EnvironmentVariables names {home['environment']!r}"
                if home["environment"] is not None else ""
            )
            # Fail closed: the command assignment overrides the environment, so
            # neither value can be shown to be the release this job runs.
            classification = PIN_UNMEASURABLE
            detail = (
                f"the job's command line assigns VNX_HOME through {home['unreadable']}"
                f"{environment_note}, so the release it runs cannot be measured against "
                f"the pinned {pin!r}"
            )
        else:
            classification, detail = _classify_home(home["value"], pin, pin_record)
        entry = {"plist": str(plist_path), "label": label, "project_id": project_id,
                 "classification": classification, "pin": pin, "vnx_home": home["value"],
                 "vnx_home_source": home["source"], "detail": detail}
        entries.append(entry)
        if classification != PIN_FOLLOWED:
            violations.append(dict(entry, kind=classification))

    return {"ok": not violations, "entries": entries, "violations": violations,
            "unmeasured": unmeasured, "versions_dir": str(versions_dir)}


class LaunchctlListFailedError(RuntimeError):
    """``launchctl list`` ran but exited non-zero. Distinct from
    ``OSError``/``subprocess.SubprocessError`` (binary missing, timeout):
    this means launchctl itself refused or errored on this specific
    invocation. Caught separately in ``main()`` so a non-zero exit is never
    silently treated as "queried successfully, nothing loaded" -- an
    absence must be MEASURED, never inferred from a failed measurement
    attempt (the exact class of bug the coordinator flagged in review: a
    prior version of this function returned ``result.stdout`` unconditionally,
    discarding ``result.returncode`` entirely)."""


def _run_real_launchctl_list() -> str:
    result = subprocess.run(
        ["launchctl", "list"], capture_output=True, text=True, timeout=10, check=False
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise LaunchctlListFailedError(
            f"launchctl list exited {result.returncode}" + (f": {stderr}" if stderr else "")
        )
    return result.stdout or ""


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fail-closed guard: exits non-zero when a required per-project "
            "launchd daemon family is missing this project's instance, or when "
            "a bare/malformed label is loaded for it (OI-1509/OI-1510, "
            "OI-1629). Read-only: never installs, loads, or unloads a launchd "
            "job."
        )
    )
    parser.add_argument(
        "--project-id",
        default=None,
        help="Override the resolved project id (default: vnx_paths.resolve_project_id())",
    )
    parser.add_argument(
        "--templates-dir",
        default=str(Path(__file__).resolve().parent),
        help="Directory containing the *.plist templates (default: this file's directory)",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args(argv)

    if sys.platform != "darwin":
        print(f"launchd_project_scope: skipped — platform {sys.platform!r} is not darwin (launchd is macOS-only)")
        return 0

    project_id = args.project_id or vnx_paths.resolve_project_id()
    if not project_id:
        print(
            "launchd_project_scope: cannot resolve a project id (no --project-id, "
            "no VNX_PROJECT_ID, no .vnx-project-id marker) — refusing to guess",
            file=sys.stderr,
        )
        return 2

    try:
        launchctl_output = _run_real_launchctl_list()
    except (OSError, subprocess.SubprocessError, LaunchctlListFailedError) as exc:
        print(f"launchd_project_scope: launchctl unavailable ({exc}) — cannot verify installed state", file=sys.stderr)
        return 2

    # An absent registry is an empty fleet; one that exists but cannot be read
    # or carries a malformed entry is unmeasurable -- exit 2, never a quiet pass.
    try:
        registry = json.loads((Path.home() / ".vnx" / "projects.json").read_text(encoding="utf-8"))
        projects = {
            str(entry["project_id"]): Path(entry["path"]).expanduser()
            for entry in registry["projects"]
        }
    except FileNotFoundError:
        projects = {}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"launchd_project_scope: project registry unusable ({exc})", file=sys.stderr)
        return 2
    pin_alignment = check_pin_alignment(Path.home() / "Library" / "LaunchAgents", projects)

    result = check_project_scope(Path(args.templates_dir), project_id, launchctl_output)
    result["pin_alignment"] = pin_alignment
    result["violations"] = result["violations"] + pin_alignment["violations"]
    result["ok"] = result["ok"] and pin_alignment["ok"]

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        status = "OK" if result["ok"] else "VIOLATIONS"
        print(f"launchd_project_scope[{project_id}]: {status}")
        for violation in result["violations"]:
            owner = (
                violation.get("family")
                or violation.get("label")
                or violation.get("project_id")
            )
            print(f"  - [{violation['kind']}] {owner}: {violation['detail']}")

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
