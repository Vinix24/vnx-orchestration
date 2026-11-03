"""Guard bin/vnx command branches against loader filename drift.

The main dispatcher loads external commands from scripts/commands/<name>.sh or
the hyphen-to-underscore variant before entering the case branch. This test
derives the command branches from bin/vnx itself and verifies that each branch
has an invocable implementation target. It fails for the old worktree-release
filename drift, where the branch called cmd_worktree_release but the loader
could only look for worktree-release.sh or worktree_release.sh.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_VNX = REPO_ROOT / "bin" / "vnx"
COMMANDS_DIR = REPO_ROOT / "scripts" / "commands"


@dataclass(frozen=True)
class CaseBranch:
    patterns: tuple[str, ...]
    body: str
    line: int


def _main_command_case_lines(text: str) -> tuple[int, list[str]]:
    lines = text.splitlines()
    main_start = next(
        i for i, line in enumerate(lines)
        if re.match(r"^main\(\)\s*\{\s*$", line)
    )
    start = next(
        i for i, line in enumerate(lines)
        if i > main_start and re.match(r'^\s*case "\$cmd" in\s*$', line)
    )

    depth = 1
    body: list[str] = []
    for offset, line in enumerate(lines[start + 1:], start + 2):
        if re.match(r"^\s*case\b", line):
            depth += 1
        elif re.match(r"^\s*esac\b", line):
            depth -= 1
            if depth == 0:
                return start + 2, body
        body.append(line)

    raise AssertionError("Could not find closing esac for main command case")


def _extract_case_branches(text: str) -> list[CaseBranch]:
    start_line, lines = _main_command_case_lines(text)
    branches: list[CaseBranch] = []
    current_patterns: tuple[str, ...] | None = None
    current_body: list[str] = []
    current_line = 0

    branch_re = re.compile(r"^    ([A-Za-z0-9_*|-]+)\)\s*$")
    for index, line in enumerate(lines, start_line):
        match = branch_re.match(line)
        if match:
            if current_patterns is not None:
                branches.append(
                    CaseBranch(current_patterns, "\n".join(current_body), current_line)
                )
            current_patterns = tuple(match.group(1).split("|"))
            current_body = []
            current_line = index
        elif current_patterns is not None:
            current_body.append(line)

    if current_patterns is not None:
        branches.append(CaseBranch(current_patterns, "\n".join(current_body), current_line))

    return branches


def _inline_functions(text: str) -> set[str]:
    return set(re.findall(r"^\s*(cmd_[A-Za-z0-9_]+)\(\)\s*\{", text, re.MULTILINE))


def _loadable_command_file(command: str) -> Path | None:
    direct = COMMANDS_DIR / f"{command}.sh"
    if direct.exists():
        return direct

    underscore = command.replace("-", "_")
    if underscore != command:
        alternate = COMMANDS_DIR / f"{underscore}.sh"
        if alternate.exists():
            return alternate

    return None


def _branch_commands(branch: CaseBranch) -> tuple[str, ...]:
    return tuple(
        pattern for pattern in branch.patterns
        if pattern != "*" and not pattern.startswith("-")
    )


def _called_cmd_functions(body: str) -> set[str]:
    calls = set()
    for line in body.splitlines():
        match = re.match(r"^\s*(cmd_[A-Za-z0-9_]+)\b", line)
        if match:
            calls.add(match.group(1))
    return calls


def _vnx_home_paths(body: str) -> set[Path]:
    paths: set[Path] = set()
    for match in re.finditer(r'"\$VNX_HOME/([^":]+)"', body):
        relative = match.group(1)
        if relative.startswith("scripts/"):
            paths.add(REPO_ROOT / relative)
    return paths


def _python_module_paths(body: str) -> set[Path]:
    paths: set[Path] = set()
    for match in re.finditer(r"\s-m\s+([A-Za-z_][A-Za-z0-9_.]*)\b", body):
        module = match.group(1)
        module_path = REPO_ROOT / Path(*module.split("."))
        if module_path.with_suffix(".py").exists():
            paths.add(module_path.with_suffix(".py"))
        elif (module_path / "__init__.py").exists():
            paths.add(module_path / "__init__.py")
        else:
            paths.add(module_path.with_suffix(".py"))
    return paths


def _external_file_defines_function(command_file: Path, function: str) -> bool:
    script = (
        "set -e\n"
        f"VNX_HOME={shlex.quote(str(REPO_ROOT))}\n"
        f"VNX_COMMANDS_DIR={shlex.quote(str(COMMANDS_DIR))}\n"
        f"source {shlex.quote(str(command_file))}\n"
        f"declare -F {shlex.quote(function)} >/dev/null\n"
    )
    result = subprocess.run(
        ["bash"],
        input=script,
        text=True,
        capture_output=True,
        timeout=10,
    )
    return result.returncode == 0


def test_every_bin_vnx_case_branch_has_invocable_implementation():
    text = BIN_VNX.read_text()
    inline = _inline_functions(text)
    branches = _extract_case_branches(text)

    failures: list[str] = []
    for branch in branches:
        if "*" in branch.patterns:
            continue

        command_names = _branch_commands(branch)
        cmd_calls = _called_cmd_functions(branch.body)
        script_paths = _vnx_home_paths(branch.body)
        module_paths = _python_module_paths(branch.body)

        if cmd_calls:
            for function in sorted(cmd_calls):
                if function in inline:
                    continue

                command_results = []
                for command in command_names:
                    command_file = _loadable_command_file(command)
                    if command_file is None:
                        command_results.append(f"{command}: no loader file")
                        continue
                    if _external_file_defines_function(command_file, function):
                        break
                    command_results.append(
                        f"{command}: {command_file.relative_to(REPO_ROOT)} "
                        f"does not define {function}"
                    )
                else:
                    failures.append(
                        f"line {branch.line} {branch.patterns}: {function} is not "
                        f"inline and is not defined by a loadable command file "
                        f"({'; '.join(command_results)})"
                    )
            continue

        missing_paths = sorted(path for path in script_paths | module_paths if not path.exists())
        if missing_paths:
            failures.append(
                f"line {branch.line} {branch.patterns}: missing direct target(s): "
                + ", ".join(str(path.relative_to(REPO_ROOT)) for path in missing_paths)
            )
            continue

        if script_paths or module_paths or re.search(r"^\s*usage\b", branch.body, re.MULTILINE):
            continue

        failures.append(
            f"line {branch.line} {branch.patterns}: no cmd_* call, script path, "
            "python module, or usage handler found"
        )

    assert not failures, "Broken bin/vnx command branches:\n" + "\n".join(failures)


# ---------------------------------------------------------------------------
# `preflight` delegation to the pip CLI (OI-2456)
# ---------------------------------------------------------------------------

def _temporary_vnx_home(tmp_path: Path) -> Path:
    """A throwaway VNX_HOME carrying this tree's shim, mode gate and pip CLI.

    ``bin/vnx`` derives VNX_HOME from its own location, so the shim is copied
    into the temporary tree instead of being run from the checkout: a real
    copy (never a symlink back into the repo) proves the delegation loads
    ``vnx_cli`` from ``$VNX_HOME`` and not from whatever the test process
    happened to import. Only what the ``preflight --help`` path needs is
    copied: the shim, the two scripts/lib modules the mode gate imports
    (``vnx_paths.sh`` for path resolution, ``vnx_mode.py`` for the tier
    check) and the whole ``vnx_cli`` package.
    """
    home = tmp_path / "vnx-home"
    (home / "bin").mkdir(parents=True)
    (home / "scripts" / "lib").mkdir(parents=True)
    shutil.copy2(BIN_VNX, home / "bin" / "vnx")
    for name in ("vnx_paths.sh", "vnx_mode.py"):
        shutil.copy2(REPO_ROOT / "scripts" / "lib" / name, home / "scripts" / "lib" / name)
    shutil.copytree(
        REPO_ROOT / "vnx_cli",
        home / "vnx_cli",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copy2(REPO_ROOT / "VERSION", home / "VERSION")
    return home


def test_bin_vnx_preflight_delegates_to_vnx_cli(tmp_path):
    """`bin/vnx preflight --help` must reach the pip CLI's preflight parser.

    On origin/main the name is in no mode tier, so the shim's own gate
    refuses it with `Error: 'preflight' requires a different mode` before any
    case branch (OI-2456). With `preflight` in the shared tier and a
    delegating case branch, the same invocation prints the parser's usage and
    exits 0 — from a temporary VNX_HOME, so the tree under test cannot be
    confused with the operator's install.
    """
    home = _temporary_vnx_home(tmp_path)
    data_dir = tmp_path / "project" / ".vnx-data"
    data_dir.mkdir(parents=True)
    (data_dir / "mode.json").write_text(
        json.dumps({"mode": "starter", "schema_version": 1}) + "\n", encoding="utf-8"
    )
    (tmp_path / "home").mkdir()

    env = {key: value for key, value in os.environ.items() if not key.startswith("VNX_")}
    env.pop("PYTHONPATH", None)
    env["HOME"] = str(tmp_path / "home")
    # VNX_DATA_DIR_EXPLICIT keeps vnx_mode reading this scratch store instead
    # of resolving the operator's central ~/.vnx-data.
    env["VNX_DATA_DIR"] = str(data_dir)
    env["VNX_DATA_DIR_EXPLICIT"] = "1"

    result = subprocess.run(
        [str(home / "bin" / "vnx"), "preflight", "--help"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    combined = result.stdout + result.stderr
    assert "requires a different mode" not in combined, combined
    assert result.returncode == 0, combined
    assert "usage: vnx preflight" in result.stdout, combined
