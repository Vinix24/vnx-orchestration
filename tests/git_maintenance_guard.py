"""Shared guard for the throwaway git repos tests create.

``git commit`` finishes with ``run_auto_maintenance()``, which spawns a
detached ``git maintenance run --auto --quiet --detach`` child unless
``maintenance.auto`` is false (or ``gc.auto`` is 0). That child owns
``.git/objects/maintenance.lock`` for a few milliseconds; a
``TemporaryDirectory`` deleting its repo inside that window failed VNX CI run
39971696706 with ``OSError: [Errno 39] Directory not empty: 'objects'``
(``test_attest_d2.py::TestDiffBinding::test_diff_bind_forged_manifest_fails``,
serial Profile A only), which blocked the D4 xdist cutover rule. Hiding the
cleanup error (``ignore_cleanup_errors=True``, a retry) would hide real
cleanup failures too, so the writer is never spawned instead.

:func:`disable_auto_maintenance` writes both settings into one repo's own
config, so they travel with the repo even when a later git command runs under
a scrubbed environment. :func:`install_auto_maintenance_guard` injects them
into this process's environment as git command-scope config
(``GIT_CONFIG_COUNT`` / ``GIT_CONFIG_KEY_n`` / ``GIT_CONFIG_VALUE_n``); every
git child process the test run starts inherits them, which covers the dozens
of repo-creating helpers in ``tests/`` — including helpers added later —
without an edit per helper that the next helper silently misses.
``tests/conftest.py`` calls it once per session. Neither entry point touches
the operator's ``~/.gitconfig``.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import MutableMapping, Optional

#: The two settings that stop ``git commit`` from spawning the detached child.
AUTO_MAINTENANCE_SETTINGS: tuple[tuple[str, str], ...] = (
    ("maintenance.auto", "false"),
    ("gc.auto", "0"),
)

_ENV_PREFIX = "GIT_CONFIG"


def disable_auto_maintenance(repo: Path) -> None:
    """Write both settings into ``repo``'s local config (idempotent)."""
    for key, value in AUTO_MAINTENANCE_SETTINGS:
        subprocess.run(
            ["git", "config", key, value],
            cwd=str(repo), check=True, capture_output=True,
        )


def auto_maintenance_disabled(repo: Path) -> bool:
    """True ONLY when ``repo``'s own config carries exactly both settings.

    Fail-closed: a key whose read fails (unset), returns nothing, or returns
    any value other than the exact expected one is NOT disabled. The read is
    pinned to the local scope on purpose: a command-scope setting inherited
    from the environment says nothing about the repo itself, and the repo is
    what a scrubbed-env git call or a copied repo has to keep.
    """
    for key, expected in AUTO_MAINTENANCE_SETTINGS:
        result = subprocess.run(
            ["git", "config", "--local", "--get", key],
            cwd=str(repo), capture_output=True, text=True,
        )
        if result.returncode != 0:
            return False
        if result.stdout.strip() != expected:
            return False
    return True


def install_auto_maintenance_guard(
    env: Optional[MutableMapping[str, str]] = None,
) -> None:
    """Add both settings as command-scope config to ``env`` (default: os.environ).

    Idempotent: a pair already present among the existing ``GIT_CONFIG_*``
    command-scope entries is not added twice. A malformed existing
    ``GIT_CONFIG_COUNT`` raises instead of silently discarding the entries it
    describes (that would drop whatever a caller deliberately configured).
    """
    target: MutableMapping[str, str] = os.environ if env is None else env
    count = _config_count(target)
    present = {
        (
            target.get(f"{_ENV_PREFIX}_KEY_{index}"),
            target.get(f"{_ENV_PREFIX}_VALUE_{index}"),
        )
        for index in range(count)
    }
    for key, value in AUTO_MAINTENANCE_SETTINGS:
        if (key, value) in present:
            continue
        target[f"{_ENV_PREFIX}_KEY_{count}"] = key
        target[f"{_ENV_PREFIX}_VALUE_{count}"] = value
        count += 1
    target[f"{_ENV_PREFIX}_COUNT"] = str(count)


def _config_count(env: MutableMapping[str, str]) -> int:
    raw = env.get(f"{_ENV_PREFIX}_COUNT")
    if raw is None or raw == "":
        return 0
    try:
        count = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"GIT_CONFIG_COUNT={raw!r} is not an integer; refusing to append "
            "the auto-maintenance guard to an ambiguous command-scope config"
        ) from exc
    if count < 0:
        raise ValueError(f"GIT_CONFIG_COUNT={raw!r} is negative")
    return count
