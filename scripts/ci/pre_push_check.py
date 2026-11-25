#!/usr/bin/env python3
"""Pre-push check: run what CI would run on the files this branch touched.

Usage: pre_push_check.py [--base origin/main] [--repo <dir>]

Touched files are `git diff --name-only --diff-filter=d <base>...HEAD`
(committed work only, deleted files left out). Steps, in order, each with a
one-line result:

  compile        python3.11 -m py_compile on every touched .py (CI compiles on 3.11)
  lint patterns  the added lines of touched scripts/ and dashboard/ .py files, piped
                 to scripts/ci_lint_patterns.py --scan-stdin (the CI Lint Patterns job)
  ratchet        scripts/ci/check_function_size_ratchet.py --base <base> --head HEAD
  tests          pytest on the selected test files (xdist -n 4 when importable).
                 Selection reasons: a touched test file; b neighbour by name
                 (tests/test_<stem>.py, tests/test_<stem>_*.py); c a test that imports
                 the module; d a test that names the file as a string, builds its
                 directory from path segments, or globs/walks a directory holding it.

A step whose script is not in the repo is skipped with a printed line.

Plug-in (ADR-079): `pre_push_command: "<shell command>"` in `.vnx/config.yml`
(flat `key: value`). When set, that command runs from the repo root instead of
the default steps and its exit code decides. With neither the default scripts
nor a command the check prints `no command configured` and exits 0.

Exit codes: 0 green, 1 red, 2 bad input (not a git repo, unknown base, unreadable config).
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

RED_TAIL = "pre-push check: RED, do not push. Fix, commit, run it again."
NO_COMMAND = "pre-push check: no command configured (.vnx/config.yml pre_push_command)"
NO_PY311 = "python3.11 not found: CI compiles on 3.11"
LINT_SCRIPT = "scripts/ci_lint_patterns.py"
RATCHET_SCRIPT = "scripts/ci/check_function_size_ratchet.py"
CODE_DIRS = ("scripts/", "dashboard/", "bin/", "hooks/", "vnx_cli/")
GENERIC_NAMES = {"__init__.py", "conftest.py"}


class BadInput(Exception):
    """The check could not run: bad repo, base or config (exit 2)."""


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise BadInput(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def touched_files(repo: Path, base: str) -> list[str]:
    out = git(repo, "diff", "--name-only", "--diff-filter=d", f"{base}...HEAD")
    return [line for line in out.splitlines() if line]


def read_config_command(repo: Path) -> str | None:
    cfg = repo / ".vnx" / "config.yml"
    if not cfg.is_file():
        return None
    try:
        text = cfg.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise BadInput(f"cannot read {cfg}: {exc}") from exc
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() == "pre_push_command":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value or None
    return None


# --- test selection ---------------------------------------------------------------------------


def _const_runs(items: list) -> list[str]:
    runs, cur = [], []
    for item in items + [None]:
        if isinstance(item, str):
            cur.extend(p for p in item.split("/") if p)
        else:
            if cur:
                runs.append("/".join(cur))
            cur = []
    return runs


def _flatten_div(node: ast.AST) -> list:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _flatten_div(node.left) + _flatten_div(node.right)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    return [None]


def _is_sys_path_call(node: ast.AST) -> bool:
    func = getattr(node, "func", None)
    return (isinstance(node, ast.Call) and isinstance(func, ast.Attribute) and func.attr in {"insert", "append"}
            and isinstance(func.value, ast.Attribute) and func.value.attr == "path")


def _receiver_chains(node: ast.AST, names: dict[str, list[str]]) -> list[str]:
    """Path chains a glob/walk/copytree receiver resolves to (a name, a chain, str(x), Path(x))."""
    if isinstance(node, ast.Name):
        return names.get(node.id, [])
    if isinstance(node, ast.BinOp):
        return _const_runs(_flatten_div(node))
    if isinstance(node, ast.Call):
        return [c for a in node.args for c in _receiver_chains(a, names)]
    return []


def _globbed_chains(tree: ast.AST) -> list[str]:
    names: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.BinOp):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names[target.id] = _const_runs(_flatten_div(node.value))
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attr = node.func.attr
        if attr in {"rglob", "glob", "iterdir"}:
            out.extend(_receiver_chains(node.func.value, names))
        elif attr in {"walk", "copytree"} and node.args:
            out.extend(_receiver_chains(node.args[0], names))
    return out


def test_facts(text: str) -> dict:
    """Static facts about one test file: literals, imports, path chains, glob use."""
    facts = {"literals": [], "imports": set(), "chains": [], "path_chains": [], "globbed": []}
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        facts["literals"] = [text]  # unparseable: fall back to plain text, never skip it
        return facts
    facts["globbed"] = _globbed_chains(tree)
    inner = {id(n.left) for n in ast.walk(tree) if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div)}
    # A chain only put on sys.path is an import root, not a read: kept apart, used with a glob.
    on_path = {id(sub) for n in ast.walk(tree) if _is_sys_path_call(n) for a in n.args for sub in ast.walk(a)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            facts["literals"].append(node.value)
        elif isinstance(node, ast.Import):
            facts["imports"].update(a.name.split(".")[-1] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            facts["imports"].update([(node.module or "").split(".")[-1]] + [a.name for a in node.names])
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) and id(node) not in inner:
            runs = _const_runs(_flatten_div(node))
            facts["path_chains" if id(node) in on_path else "chains"].extend(runs)
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "join":
            args = [a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else None for a in node.args]
            facts["chains"].extend(_const_runs(args))
    return facts


def _name_in(literal: str, name: str) -> bool:
    return re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w])", literal) is not None


def _reason_d(facts: dict, path: str) -> str | None:
    base = path.rsplit("/", 1)[-1]
    named = "." in base and base not in GENERIC_NAMES  # a bare name like `vnx` is too common a word
    for lit in facts["literals"]:
        if path in lit or (named and _name_in(lit, base)):
            return f"d: names {base}"
    # `git ls-files` scans the whole repo: every chain the test builds counts as scanned.
    scans_repo = "ls-files" in "".join(facts["literals"])
    chains = facts["chains"] + (facts["path_chains"] if scans_repo else [])
    for chain in chains:
        if path == chain or path.startswith(chain + "/"):
            # A code dir is on sys.path or imported all the time: it only counts when the
            # test globs, walks or copies that very dir. A data dir (schemas/migrations) counts alone.
            if scans_repo or chain in facts["globbed"] or ("/" in chain and not (chain + "/").startswith(CODE_DIRS)):
                return f"d: reads {chain}/"
    return None


def reasons_for(path: str, test: str, facts: dict) -> list[str]:
    out = []
    stem = Path(path).stem
    if path == test:
        out.append("a: touched test")
    name = Path(test).name
    if path.endswith(".py") and not path.startswith("tests/"):
        if name == f"test_{stem}.py" or (name.startswith(f"test_{stem}_") and name.endswith(".py")):
            out.append("b: neighbour by name")
        if stem in facts["imports"] or any(f"{stem}" == lit for lit in facts["literals"]):
            out.append("c: imports module")
    d = _reason_d(facts, path)
    if d:
        out.append(d)
    return out


def select_tests(repo: Path, touched: list[str]) -> dict[str, list[str]]:
    selected: dict[str, list[str]] = {}
    tests = sorted(p.relative_to(repo).as_posix() for p in (repo / "tests").rglob("test_*.py"))
    for test in tests:
        try:
            facts = test_facts((repo / test).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            selected[test] = ["d: unreadable, selected to stay blocking"]
            continue
        for path in touched:
            for reason in reasons_for(path, test, facts):
                selected.setdefault(test, []).append(f"{reason} [{path}]")
    return selected


# --- steps ------------------------------------------------------------------------------------


def _run(cmd: list[str], repo: Path, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=repo, input=stdin, capture_output=True, text=True)


def step_compile(repo: Path, touched: list[str]) -> bool:
    files = [f for f in touched if f.endswith(".py") and (repo / f).is_file()]
    if not files:
        print("[ok] compile: no .py files touched")
        return True
    py311 = shutil.which("python3.11")
    if not py311:
        print(f"[RED] compile: {NO_PY311}")
        return False
    proc = _run([py311, "-m", "py_compile", *files], repo)
    if proc.returncode != 0:
        print("[RED] compile: py_compile failed on python3.11")
        print((proc.stderr + proc.stdout).rstrip())
        return False
    print(f"[ok] compile: {len(files)} file(s) on python3.11")
    return True


def step_lint(repo: Path, base: str, touched: list[str]) -> bool:
    if not (repo / LINT_SCRIPT).is_file():
        print(f"skipped: lint patterns ({LINT_SCRIPT} not in this repo)")
        return True
    files = [f for f in touched if re.match(r"^(scripts|dashboard)/.*\.py$", f)]
    if not files:
        print("[ok] lint patterns: no scripts/ or dashboard/ .py touched")
        return True
    diff = _run(["git", "diff", "--unified=0", f"{base}...HEAD", "--", *files], repo)
    if diff.returncode != 0:
        raise BadInput(f"git diff failed: {diff.stderr.strip()}")
    added = [ln[1:] for ln in diff.stdout.splitlines() if re.match(r"^\+[^+]", ln)]
    if not added:
        print("[ok] lint patterns: no added lines in target files")
        return True
    proc = _run([sys.executable, LINT_SCRIPT, "--scan-stdin"], repo, "\n".join(added) + "\n")
    if proc.returncode != 0:
        print("[RED] lint patterns")
        print((proc.stdout + proc.stderr).rstrip())
        return False
    print(f"[ok] lint patterns: {len(added)} added line(s) scanned")
    return True


def step_ratchet(repo: Path, base: str) -> bool:
    if not (repo / RATCHET_SCRIPT).is_file():
        print(f"skipped: ratchet ({RATCHET_SCRIPT} not in this repo)")
        return True
    proc = _run([sys.executable, RATCHET_SCRIPT, "--base", base, "--head", "HEAD"], repo)
    if proc.returncode != 0:
        print("[RED] ratchet")
        print((proc.stdout + proc.stderr).rstrip())
        return False
    print("[ok] ratchet: " + (proc.stdout.strip().splitlines() or ["clean"])[-1])
    return True


def step_tests(repo: Path, touched: list[str]) -> bool:
    selected = select_tests(repo, touched)
    for test, reasons in selected.items():
        print(f"  selected {test}: {'; '.join(reasons[:3])}")
    for path in touched:
        covered = any(f"[{path}]" in r for rs in selected.values() for r in rs)
        if path.startswith("scripts/") and path.endswith(".py") and not covered:
            print(f"  no test selects {path}")
    if not selected:
        print("[ok] tests: nothing selected")
        return True
    cmd = [sys.executable, "-m", "pytest", "-q", *selected]
    if importlib.util.find_spec("xdist") is not None:
        cmd += ["-p", "xdist", "-n", "4"]
    proc = _run(cmd, repo)
    if proc.returncode != 0:
        print(f"[RED] tests: pytest exit {proc.returncode}")
        print((proc.stdout + proc.stderr).rstrip()[-6000:])
        return False
    print(f"[ok] tests: {len(selected)} file(s) green")
    return True


def run_default(repo: Path, base: str, touched: list[str]) -> bool:
    results = [
        step_compile(repo, touched),
        step_lint(repo, base, touched),
        step_ratchet(repo, base),
        step_tests(repo, touched),
    ]
    return all(results)


def run_check(repo: Path, base: str) -> int:
    command = read_config_command(repo)
    if command:
        print(f"pre-push check: running pre_push_command: {command}")
        code = subprocess.run(command, shell=True, cwd=repo).returncode
        if code != 0:
            print(f"pre-push command exited {code}")
            print(RED_TAIL)
            return 1
        print("pre-push check: GREEN")
        return 0
    if not any((repo / s).is_file() for s in (LINT_SCRIPT, RATCHET_SCRIPT)):
        print(NO_COMMAND)
        return 0
    touched = touched_files(repo, base)
    print(f"pre-push check: {len(touched)} touched file(s) against {base}")
    if run_default(repo, base, touched):
        print("pre-push check: GREEN")
        return 0
    print(RED_TAIL)
    return 1


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        self.exit(2, f"{self.prog}: error: {message}\n")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description="Run what CI would run on the touched files.")
    parser.add_argument("--base", default="origin/main", help="base ref (default origin/main)")
    parser.add_argument("--repo", default=".", help="repository directory (default .)")
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    try:
        if not repo.is_dir():
            raise BadInput(f"not a directory: {repo}")
        repo = Path(git(repo, "rev-parse", "--show-toplevel").strip())
        git(repo, "rev-parse", "--verify", "--quiet", f"{args.base}^{{commit}}")
        return run_check(repo, args.base)
    except BadInput as exc:
        print(f"pre-push check: bad input: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
