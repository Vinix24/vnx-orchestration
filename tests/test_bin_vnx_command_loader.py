"""Guard bin/vnx command branches against loader filename drift.

The main dispatcher loads external commands from scripts/commands/<name>.sh or
the hyphen-to-underscore variant before entering the case branch. This test
derives the command branches from bin/vnx itself and verifies that each branch
has an invocable implementation target. It fails for the old worktree-release
filename drift, where the branch called cmd_worktree_release but the loader
could only look for worktree-release.sh or worktree_release.sh.
"""

from __future__ import annotations

import re
import shlex
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
