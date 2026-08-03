#!/usr/bin/env python3
"""path_parity.py — PATH/interpreter parity check: foreground vs background.

OI-852: a Homebrew relink left the PATH-resolved ``python3`` that launchd /
cron / nohup jobs see broken (or different) while the interactive foreground
shell — with its aliases and brewed PATH — kept working. Every diagnosis made
from the foreground was contaminated: "works here" proved nothing about the
jobs that were actually failing. OI-852 is closed, but the relink that caused
it can happen again, so the check stays.

The check probes ``python3`` twice:
  foreground — the ambient environment this process inherited;
  background — a scrubbed environment with the launchd/cron default PATH
               (``/usr/bin:/bin:/usr/sbin:/sbin``), no aliases, no profile.

That bare probe is diagnostic only (``raw_probe``, always ``info``): a
minimal PATH on a macOS host without Xcode Command Line Tools legitimately
resolves Xcode's bundled ``/usr/bin/python3`` (3.9.x on this fleet), which no
real background job ever runs on — every scheduled consumer pins its own
interpreter (see ``discover_launchd_consumers``/``discover_crontab_consumers``
below). Comparing the bare probes can therefore never reach parity on this
kind of machine, which is a property of macOS, not a defect anyone can fix —
so it never drives ``parity``.

``parity`` is instead driven by a **consumer scan**: it enumerates the
launchd agents (``~/Library/LaunchAgents/com.vnx.*.plist``) and crontab
entries that actually run on this machine, statically resolves (no
execution unless the version cannot be inferred from the path) what
interpreter each one would use, and fails only when a real consumer's
interpreter falls outside this project's ``requires-python`` range
(``pyproject.toml``). Consumers that invoke a script outside this repo, or a
non-Python program, are recorded but never flagged — this project's
``requires-python`` bound is not their bound to police.

The library is pure/injectable for tests; the CLI is used by
scripts/hooks/path_parity_check.sh at SessionStart.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import plistlib
import re
import shlex
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from xml.parsers.expat import ExpatError

_LOG = logging.getLogger(__name__)

# launchd/cron default PATH on macOS — what a background job actually gets.
BACKGROUND_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

_PROBE = (
    "import json,sys;"
    "print(json.dumps({"
    "'executable':sys.executable,"
    "'version':sys.version.split()[0],"
    "'prefix':sys.prefix}))"
)


def probe_interpreter(runner: Any = None, *, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Probe what ``python3`` resolves to under ``env`` (None = ambient).

    Returns {ok, executable, version, prefix, error}. Never raises — an
    unstartable interpreter is exactly what this check exists to report.
    """
    run = runner or subprocess.run
    try:
        proc = run(
            ["python3", "-c", _PROBE],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "executable": None, "version": None, "prefix": None, "error": str(exc)}
    if proc.returncode != 0:
        return {
            "ok": False,
            "executable": None,
            "version": None,
            "prefix": None,
            "error": (proc.stderr or proc.stdout or "").strip()[:500] or f"exit {proc.returncode}",
        }
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        return {
            "ok": False,
            "executable": None,
            "version": None,
            "prefix": None,
            "error": f"unparseable probe output: {exc}",
        }
    return {
        "ok": True,
        "executable": data.get("executable"),
        "version": data.get("version"),
        "prefix": data.get("prefix"),
        "error": None,
    }


def background_env() -> Dict[str, str]:
    """A scrubbed environment mimicking launchd/cron: default PATH, no profile."""
    return {
        "PATH": BACKGROUND_PATH,
        "HOME": os.environ.get("HOME", "/"),
        "USER": os.environ.get("USER", ""),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
    }


def _major_minor(version: Optional[str]) -> Optional[str]:
    if not version:
        return None
    parts = version.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else version


def compare_parity(foreground: Dict[str, Any], background: Dict[str, Any]) -> Dict[str, Any]:
    """Decide raw parity from two probe results. Pure — no I/O, fully testable.

    This is the bare foreground/background comparison. It is diagnostic only
    (see module docstring) — callers surface its output as ``info``, never as
    something that drives the check's overall ``parity`` verdict.
    """
    mismatches = []
    info = []

    if not foreground.get("ok"):
        # The foreground being broken is reported but cannot be compared
        # against — treat as a parity failure of its own kind.
        mismatches.append({"kind": "foreground_interpreter_broken", "error": foreground.get("error")})
    if not background.get("ok"):
        mismatches.append({"kind": "background_interpreter_broken", "error": background.get("error")})

    fg_mm = _major_minor(foreground.get("version"))
    bg_mm = _major_minor(background.get("version"))
    if foreground.get("ok") and background.get("ok") and fg_mm != bg_mm:
        mismatches.append(
            {
                "kind": "version_mismatch",
                "foreground_version": foreground.get("version"),
                "background_version": background.get("version"),
            }
        )

    if (
        foreground.get("ok")
        and background.get("ok")
        and foreground.get("executable") != background.get("executable")
    ):
        info.append(
            {
                "kind": "executable_differs",
                "foreground": foreground.get("executable"),
                "background": background.get("executable"),
            }
        )

    return {
        "parity": not mismatches,
        "mismatches": mismatches,
        "info": info,
    }


# ─────────────────────────────────────────────────────────────────────────
# Consumer scan — what real launchd/cron jobs would actually resolve.
# ─────────────────────────────────────────────────────────────────────────

DEFAULT_LAUNCHAGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
_LAUNCHD_LABEL_GLOB = "com.vnx.*.plist"

_VERSION_IN_PATH_RE = re.compile(r"python(?:3)?[@.]?(\d+)\.(\d+)")
_PINNED_PATH_RE = re.compile(r"(?:\"|'|\s|^)(/[\w./@-]*python3(?:\.\d+)?)\b")
_REQUIRES_PYTHON_RE = re.compile(r'(?m)^requires-python\s*=\s*"([^"]+)"')
_CLAUSE_RE = re.compile(r"(>=|<=|==|!=|>|<)\s*(\d+)\.(\d+)")


def parse_requires_python(pyproject_path: Path) -> Optional[str]:
    """Read the raw ``requires-python`` specifier string from pyproject.toml.

    Returns None if the file is unreadable or the key is absent — callers
    must treat that as "cannot judge range", not as "everything is fine".
    """
    try:
        text = Path(pyproject_path).read_text(encoding="utf-8")
    except OSError:
        return None
    match = _REQUIRES_PYTHON_RE.search(text)
    return match.group(1) if match else None


def _version_tuple(version: str) -> tuple:
    parts = version.split(".")
    return (int(parts[0]), int(parts[1]))


def version_in_range(version: str, requires_python: str) -> Optional[bool]:
    """Check a ``major.minor`` version string against a requires-python specifier.

    Supports the comma-joined ``>=``/``<=``/``==``/``!=``/``>``/``<`` clauses
    that ``requires-python`` values actually use (PEP 440 in full is not
    needed here). Returns None — "cannot judge" — when either input is
    unparseable, so callers never mistake "couldn't check" for "in range".
    """
    if not version or not requires_python:
        return None
    try:
        v = _version_tuple(version)
    except (ValueError, IndexError):
        return None
    clauses = [c.strip() for c in requires_python.split(",") if c.strip()]
    if not clauses:
        return None
    for clause in clauses:
        m = _CLAUSE_RE.match(clause)
        if not m:
            continue
        op, major, minor = m.group(1), int(m.group(2)), int(m.group(3))
        bound = (major, minor)
        if op == ">=" and not v >= bound:
            return False
        if op == "<=" and not v <= bound:
            return False
        if op == "==" and not v == bound:
            return False
        if op == "!=" and not v != bound:
            return False
        if op == ">" and not v > bound:
            return False
        if op == "<" and not v < bound:
            return False
    return True


def _version_from_path(path: str) -> Optional[str]:
    """Infer major.minor from an interpreter path without executing it.

    Covers both ``.../python@3.12/bin/python3.12`` and ``.../python3.12``
    shapes. Returns None when the path doesn't encode a version (e.g. a bare
    ``.venv/bin/python`` symlink) — the caller must then run ``--version``.
    """
    match = _VERSION_IN_PATH_RE.search(path)
    if not match:
        return None
    return f"{match.group(1)}.{match.group(2)}"


def resolve_interpreter_version(
    path: str,
    runner: Any = None,
    cache: Optional[Dict[str, Optional[str]]] = None,
) -> Optional[str]:
    """Resolve an interpreter's major.minor version.

    Infers it from the path when possible (no execution). Otherwise runs
    ``--version`` once and caches the result by path — a fleet with several
    consumers sharing one interpreter (e.g. the pinned python@3.12) never
    re-executes it per consumer.
    """
    inferred = _version_from_path(path)
    if inferred:
        return inferred

    cache = cache if cache is not None else {}
    if path in cache:
        return cache[path]

    run = runner or subprocess.run
    version: Optional[str] = None
    try:
        proc = run(
            [path, "-c", "import sys;print(sys.version.split()[0])"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if proc.returncode == 0:
            raw = proc.stdout.strip().splitlines()[-1]
            version = _major_minor(raw)
    except (OSError, subprocess.TimeoutExpired, IndexError):
        version = None
    cache[path] = version
    return version


def discover_launchd_consumers(agents_dir: Path) -> List[Dict[str, Any]]:
    """Enumerate ``com.vnx.*.plist`` launchd agents as ``{label, argv, source}``.

    Uses stdlib ``plistlib`` (reads both XML and binary plists, on any
    platform) instead of shelling out to ``plutil``, which does not exist on
    Linux CI. Never raises — an unreadable or malformed plist is skipped, not
    fatal to the scan.
    """
    agents_dir = Path(agents_dir)
    consumers: List[Dict[str, Any]] = []
    if not agents_dir.is_dir():
        return consumers
    for plist in sorted(agents_dir.glob(_LAUNCHD_LABEL_GLOB)):
        try:
            with open(plist, "rb") as fh:
                data = plistlib.load(fh)
        except (plistlib.InvalidFileException, ExpatError, OSError, ValueError):
            # vnx-silent-except: unreadable/malformed plist is skipped, scan is fail-soft
            continue
        argv = data.get("ProgramArguments")
        if not argv:
            continue
        consumers.append(
            {
                "label": data.get("Label") or plist.stem,
                "argv": list(argv),
                "source": str(plist),
                "search_path": BACKGROUND_PATH,
            }
        )
    return consumers


_CRON_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def read_crontab(runner: Any = None) -> str:
    """Return the current user's ``crontab -l`` output, or "" if none/absent."""
    run = runner or subprocess.run
    try:
        proc = run(["crontab", "-l"], capture_output=True, text=True, check=False, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout or ""


def discover_crontab_consumers(crontab_text: str) -> List[Dict[str, Any]]:
    """Parse ``crontab -l`` output into ``{label, argv, source, search_path}`` entries.

    Honors a leading ``PATH=`` assignment as the search path for any bare
    (unqualified) command those cron lines invoke — cron does not inherit the
    interactive shell's PATH, so this is the PATH a bare ``python3`` in a cron
    line would actually resolve against.
    """
    search_path = BACKGROUND_PATH
    consumers: List[Dict[str, Any]] = []
    idx = 0
    for line in crontab_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _CRON_ENV_ASSIGN_RE.match(stripped):
            key, _, value = stripped.partition("=")
            if key.strip() == "PATH":
                search_path = value.strip()
            continue
        try:
            tokens = shlex.split(stripped)
        except ValueError:
            continue
        if not tokens:
            continue
        if tokens[0].startswith("@"):
            argv = tokens[1:]
        else:
            argv = tokens[5:]
        if not argv:
            continue
        consumers.append(
            {
                "label": f"crontab#{idx}",
                "argv": argv,
                "source": "crontab",
                "search_path": search_path,
            }
        )
        idx += 1
    return consumers


_SHELL_BASENAMES = {"bash", "sh", "zsh"}


def _extract_pinned_python_path(text: str) -> Optional[str]:
    for match in _PINNED_PATH_RE.finditer(text):
        candidate = match.group(1)
        if Path(candidate).exists():
            return candidate
    return None


def resolve_consumer_interpreter(
    argv: List[str],
    repo_root: Path,
    search_path: str = BACKGROUND_PATH,
) -> Dict[str, Any]:
    """Statically determine the python interpreter a consumer's argv resolves to.

    Never executes the target script. Returns
    ``{interpreter, relevant, reason}`` — ``relevant`` is False for anything
    this project's ``requires-python`` bound has no business judging: a
    non-Python program, or a script outside ``repo_root`` (a different
    project with its own, possibly different, supported-version range).
    """
    repo_root = Path(repo_root).resolve()
    if not argv:
        return {"interpreter": None, "relevant": False, "reason": "empty argv"}

    exe = argv[0]
    basename = Path(exe).name

    if basename.startswith("python3") or "python@3" in exe:
        return {"interpreter": exe, "relevant": True, "reason": "direct interpreter invocation"}

    if basename == "vnx":
        return {
            "interpreter": None,
            "relevant": False,
            "reason": "vnx binary resolves its own interpreter (VNX_PYTHON, #1247)",
        }

    if basename not in _SHELL_BASENAMES:
        return {"interpreter": None, "relevant": False, "reason": f"non-python consumer ({basename})"}

    # Shell wrapper: either `bash -c "<inline text>"` or `bash /path/to/script`.
    if len(argv) >= 3 and argv[1] == "-c":
        text = argv[2]
        script_desc = "inline -c script"
        in_repo = str(repo_root) in text
    else:
        script_path = next((Path(a) for a in argv[1:] if a.endswith((".sh", ".py"))), None)
        if script_path is None:
            return {"interpreter": None, "relevant": False, "reason": "shell wrapper, no script argument"}
        if not script_path.is_absolute():
            script_path = (repo_root / script_path).resolve()
        script_desc = script_path.name
        try:
            in_repo = script_path.resolve().is_relative_to(repo_root)
        except AttributeError:  # pragma: no cover - Python <3.9 fallback, unreachable on this fleet
            try:
                script_path.resolve().relative_to(repo_root)
                in_repo = True
            except ValueError:
                in_repo = False
        if not in_repo:
            return {
                "interpreter": None,
                "relevant": False,
                "reason": f"{script_desc}: outside {repo_root.name} — not this project's requires-python to judge",
            }
        try:
            text = script_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {"interpreter": None, "relevant": False, "reason": f"{script_desc}: unreadable"}

    if not in_repo:
        return {
            "interpreter": None,
            "relevant": False,
            "reason": f"{script_desc}: outside {repo_root.name} — not this project's requires-python to judge",
        }

    venv_python = repo_root / ".venv" / "bin" / "python"
    if ".venv/bin/python" in text and venv_python.exists():
        return {"interpreter": str(venv_python), "relevant": True, "reason": f"{script_desc}: repo venv"}

    pinned = _extract_pinned_python_path(text)
    if pinned:
        return {"interpreter": pinned, "relevant": True, "reason": f"{script_desc}: pinned interpreter path"}

    if re.search(r"(?<![./\w])python3\b", text):
        found = shutil.which("python3", path=search_path)
        return {
            "interpreter": found,
            "relevant": found is not None,
            "reason": f"{script_desc}: bare python3 via {'declared PATH' if search_path != BACKGROUND_PATH else 'background PATH'}",
        }

    return {"interpreter": None, "relevant": False, "reason": f"{script_desc}: no python invocation found"}


def scan_consumers(
    consumers: List[Dict[str, Any]],
    repo_root: Path,
    requires_python: Optional[str],
    runner: Any = None,
) -> Dict[str, Any]:
    """Resolve every consumer's interpreter and check it against requires_python.

    Pure aside from the interpreter-version execution inside
    ``resolve_interpreter_version`` (injectable via ``runner``, cached by
    path). ``parity`` here is what drives the check's overall verdict.
    """
    version_cache: Dict[str, Optional[str]] = {}
    resolved: List[Dict[str, Any]] = []
    mismatches: List[Dict[str, Any]] = []

    for consumer in consumers:
        outcome = resolve_consumer_interpreter(
            consumer["argv"], repo_root, consumer.get("search_path", BACKGROUND_PATH)
        )
        entry: Dict[str, Any] = {
            "label": consumer.get("label"),
            "source": consumer.get("source"),
            **outcome,
        }
        if outcome["relevant"] and outcome["interpreter"]:
            version = resolve_interpreter_version(outcome["interpreter"], runner=runner, cache=version_cache)
            entry["version"] = version
            in_range = version_in_range(version, requires_python) if version else None
            entry["in_range"] = in_range
            if in_range is False:
                mismatches.append(
                    {
                        "kind": "consumer_interpreter_out_of_range",
                        "consumer": consumer.get("label"),
                        "source": consumer.get("source"),
                        "interpreter": outcome["interpreter"],
                        "version": version,
                        "requires_python": requires_python,
                    }
                )
        resolved.append(entry)

    return {
        "parity": not mismatches,
        "requires_python": requires_python,
        "consumers": resolved,
        "mismatches": mismatches,
    }


def _default_repo_root() -> Path:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], stderr=subprocess.DEVNULL, text=True, timeout=5
        ).strip()
        if out:
            return Path(out)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError, subprocess.TimeoutExpired):
        pass
    return Path.cwd()


def check_parity(runner: Any = None, repo_root: Optional[Path] = None) -> Dict[str, Any]:
    """Probe foreground/background (diagnostic) and scan real consumers (authoritative)."""
    repo_root = Path(repo_root) if repo_root is not None else _default_repo_root()

    foreground = probe_interpreter(runner=runner)
    background = probe_interpreter(runner=runner, env=background_env())
    raw = compare_parity(foreground, background)

    requires_python = parse_requires_python(repo_root / "pyproject.toml")
    consumers = discover_launchd_consumers(DEFAULT_LAUNCHAGENTS_DIR)
    consumers += discover_crontab_consumers(read_crontab(runner=runner))
    consumer_scan = scan_consumers(consumers, repo_root, requires_python, runner=runner)

    return {
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "parity": consumer_scan["parity"],
        "mismatches": consumer_scan["mismatches"],
        "consumer_scan": consumer_scan,
        "raw_probe": {
            "level": "info",
            "foreground": foreground,
            "background": background,
            "background_path": BACKGROUND_PATH,
            "mismatches": raw["mismatches"],
            "info": raw["info"],
        },
    }


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="PATH/interpreter parity check: real consumers vs requires-python")
    parser.add_argument("--write", default=None, help="Write the result JSON to this path")
    parser.add_argument("--no-fail", action="store_true", help="Always exit 0 (hook mode)")
    parser.add_argument("--repo-root", default=None, help="Project root to scope the consumer scan to")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root) if args.repo_root else None
    result = check_parity(repo_root=repo_root)

    if args.write:
        try:
            out = Path(args.write)
            # Test-isolation backstop (w19c class guard): a no-op in production
            # (pytest is never on sys.modules there), but catches a test that
            # lost its isolation and is about to write into the real central
            # store instead of a tmp_path sandbox — see the function's own
            # docstring in this same concern's precedent, vnx_paths.py.
            try:
                from vnx_paths import refuse_real_central_store_write_under_pytest

                refuse_real_central_store_write_under_pytest(out.parent)
            except ImportError:
                pass
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        except OSError as exc:
            _LOG.warning("path_parity: could not write %s: %s", args.write, exc)

    print(json.dumps(result))
    if result["parity"] or args.no_fail:
        return 0
    return 11


if __name__ == "__main__":
    raise SystemExit(main())
