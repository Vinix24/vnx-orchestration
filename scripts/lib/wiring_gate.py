"""wiring_gate.py — Dead-code detection gate for PR diffs.

AST-parses new public definitions (functions, classes) added in a PR diff,
then greps the codebase for callers. Definitions with zero callers outside
their own file (excluding tests) are flagged as unwired dead code.

Skip-list: ${VNX_STATE_DIR}/wiring_skip.yaml supports library-exports,
decorator-registry entries, __all__ re-exports, and CLI dispatch-dicts.

Env: VNX_WIRING_GATE_REQUIRED (default "0" = shadow/advisory, "1" = blocking).
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import Dict, List, Optional, Tuple

import yaml


class WiringGateError(Exception):
    pass


@dataclass(frozen=True)
class UnwiredSymbol:
    name: str
    file: str
    line: int
    kind: str  # "function" or "class"


@dataclass
class WiringGateResult:
    status: str  # "pass", "fail", "advisory", "error"
    unwired: List[UnwiredSymbol] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    total_checked: int = 0
    test_file_skipped: int = 0  # definitions left out because they live in a test file
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "unwired": [
                {"name": s.name, "file": s.file, "line": s.line, "kind": s.kind}
                for s in self.unwired
            ],
            "skipped": self.skipped,
            "total_checked": self.total_checked,
            "test_file_skipped": self.test_file_skipped,
            "summary": self.summary,
        }


def _load_skip_list(state_dir: Optional[Path] = None) -> set:
    if state_dir is None:
        env = os.environ.get("VNX_STATE_DIR")
        if env:
            state_dir = Path(env)
        else:
            # Canonical resolver (VNX_HOME + project-marker aware) instead of a
            # bare CWD-relative default, so central-mode resolves the project store.
            from vnx_paths import resolve_state_dir as _canonical_state_dir
            state_dir = _canonical_state_dir()
    skip_path = state_dir / "wiring_skip.yaml"
    if not skip_path.exists():
        return set()
    try:
        data = yaml.safe_load(skip_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return set()
    if not isinstance(data, dict):
        return set()
    symbols = set()
    for key in ("library_exports", "decorator_registry", "all_reexports", "cli_dispatch"):
        entries = data.get(key, [])
        if isinstance(entries, list):
            symbols.update(str(e) for e in entries)
    return symbols


def _get_pr_diff(pr_number: int) -> str:
    try:
        proc = subprocess.run(
            ["gh", "pr", "diff", str(pr_number)],
            capture_output=True, text=True, timeout=30, check=True,
        )
    except subprocess.TimeoutExpired as e:
        raise WiringGateError(f"gh pr diff timed out for PR #{pr_number}") from e
    except (subprocess.CalledProcessError, OSError) as e:
        raise WiringGateError(f"gh pr diff failed for PR #{pr_number}: {e}") from e
    return proc.stdout


_ADDED_DEF_RE = re.compile(r"^\+(?:async\s+)?(?:def|class)\s+([A-Za-z]\w*)")


def _extract_added_source_per_file(diff_text: str) -> dict[str, tuple[str, dict[int, int]]]:
    """Extract full added source per .py file from a unified diff.

    Returns {filepath: (source_text, {source_line: diff_line})} where source_text
    is the reconstructed new-file content from added lines, and the mapping allows
    translating AST lineno back to the actual file line in the PR.
    """
    files: dict[str, tuple[list[str], dict[int, int]]] = {}
    current_file: Optional[str] = None
    line_in_new = 0
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            match = re.search(r" b/(.+)$", line)
            current_file = match.group(1) if match else None
            line_in_new = 0
            continue
        if line.startswith("@@"):
            m = hunk_re.match(line)
            if m:
                line_in_new = int(m.group(1)) - 1
            continue
        if current_file and current_file.endswith(".py"):
            if line.startswith("+") and not line.startswith("+++"):
                line_in_new += 1
                if current_file not in files:
                    files[current_file] = ([], {})
                src_lines, line_map = files[current_file]
                src_lines.append(line[1:])  # strip leading '+'
                line_map[len(src_lines)] = line_in_new
            elif line.startswith("-"):
                pass
            else:
                line_in_new += 1

    return {fp: ("\n".join(lines), lmap) for fp, (lines, lmap) in files.items()}


def _extract_defs_via_ast(source: str, filepath: str, line_map: dict[int, int]) -> List[dict]:
    """Parse source with AST and extract public top-level function/class defs."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return _public_defs_of_tree(tree, filepath, line_map)


def _public_defs_of_tree(tree: ast.AST, filepath: str, line_map: dict[int, int]) -> List[dict]:
    """The public top-level function/class defs of a parsed module (shared walk)."""
    defs = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                diff_line = line_map.get(node.lineno, node.lineno)
                defs.append({
                    "name": node.name,
                    "file": filepath,
                    "line": diff_line,
                    "kind": "function",
                })
        elif isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                diff_line = line_map.get(node.lineno, node.lineno)
                defs.append({
                    "name": node.name,
                    "file": filepath,
                    "line": diff_line,
                    "kind": "class",
                })
    return defs


def _extract_defs_via_regex(diff_text: str) -> List[dict]:
    """Fallback regex-based extraction when AST parse fails."""
    defs = []
    current_file = None
    line_in_new = 0
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            match = re.search(r" b/(.+)$", line)
            current_file = match.group(1) if match else None
            line_in_new = 0
            continue
        if line.startswith("@@"):
            m = hunk_re.match(line)
            if m:
                line_in_new = int(m.group(1)) - 1
            continue
        if current_file and current_file.endswith(".py"):
            if line.startswith("+") and not line.startswith("+++"):
                line_in_new += 1
                m = _ADDED_DEF_RE.match(line)
                if m:
                    name = m.group(1)
                    if not name.startswith("_"):
                        kind = "class" if line[1:].strip().startswith("class") else "function"
                        defs.append({
                            "name": name,
                            "file": current_file,
                            "line": line_in_new,
                            "kind": kind,
                        })
            elif line.startswith("-"):
                pass
            else:
                line_in_new += 1
    return defs


def _is_test_file(filepath: str) -> bool:
    """A pytest file: a ``tests``/``test`` path component, or a test basename.

    Whole components and basenames only — ``testing_tools/`` and ``attest.py``
    are production files, not tests.
    """
    parts = Path(filepath).parts
    name = parts[-1] if parts else filepath
    return ("tests" in parts or "test" in parts or name.startswith("test_")
            or name.endswith("_test.py") or name == "conftest.py")


def _extract_new_public_defs(diff_text: str) -> Tuple[List[dict], int]:
    """Extract new public definitions from diff. Uses AST as primary, regex as fallback.

    Returns ``(definitions, test_file_skipped)``. A definition inside a test file
    has no caller by definition, so it is not an unwired candidate (OI-1411); the
    predicate runs on the merged list, so AST and regex fallback filter alike.
    """
    file_sources = _extract_added_source_per_file(diff_text)

    all_defs: List[dict] = []
    ast_failed_files: set[str] = set()

    for filepath, (source, line_map) in file_sources.items():
        ast_defs = _extract_defs_via_ast(source, filepath, line_map)
        if ast_defs:
            all_defs.extend(ast_defs)
        else:
            ast_failed_files.add(filepath)

    if ast_failed_files:
        regex_defs = _extract_defs_via_regex(diff_text)
        for d in regex_defs:
            if d["file"] in ast_failed_files:
                all_defs.append(d)

    production_defs = [d for d in all_defs if not _is_test_file(d["file"])]
    return production_defs, len(all_defs) - len(production_defs)


def _grep_callers(symbol_name: str, def_file: str, *, root: Optional[str] = None) -> Optional[int]:
    """Count non-test callers of symbol outside its definition file.

    ``root`` is the checkout to search; ``None`` keeps the historical "the
    process's own working directory" (``.``), which is what ``check_pr_wiring``
    has always done. A caller that HAS a checkout passes it explicitly, and
    every hit is then reported relative to it, so ``def_file`` (a path from the
    diff, always repo-relative) compares the same either way.

    Returns None on grep failure (unknown state) — callers must treat
    None as blocking rather than assuming the symbol is wired.
    """
    try:
        proc = subprocess.run(
            [
                "grep", "-r", "--include=*.py",
                "-l", symbol_name, str(root) if root is not None else ".",
            ],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None

    if proc.returncode not in (0, 1):
        return None

    callers = 0
    for hit_file in proc.stdout.splitlines():
        hit_file = hit_file.strip()
        hit_file = os.path.relpath(hit_file, root) if root is not None else hit_file.lstrip("./")
        if _counts_as_caller(hit_file, def_file):
            callers += 1
    return callers


def _counts_as_caller(hit_file: str, def_file: str) -> bool:
    """The caller rule: outside the defining file, and not a test file."""
    if hit_file == def_file:
        return False
    return not (hit_file.startswith("tests/") or "/tests/" in hit_file
                or hit_file.startswith("test_"))


def _classify_symbols(
    new_defs: List[dict], skip_list: set, root: Optional[str],
) -> Tuple[List[UnwiredSymbol], List[str], List[str]]:
    """Split new public defs into (unwired, skipped, grep_failures).

    A symbol whose caller grep could not run lands in BOTH ``unwired`` and
    ``grep_failures``: unknown is blocking, never "wired".
    """
    unwired: List[UnwiredSymbol] = []
    skipped: List[str] = []
    grep_failures: List[str] = []
    for d in new_defs:
        if d["name"] in skip_list:
            skipped.append(d["name"])
            continue
        caller_count = _grep_callers(d["name"], d["file"], root=root)
        if caller_count is None:
            grep_failures.append(d["name"])
        elif caller_count > 0:
            continue
        unwired.append(UnwiredSymbol(
            name=d["name"],
            file=d["file"],
            line=d["line"],
            kind=d["kind"],
        ))
    return unwired, skipped, grep_failures


def _grep_failure_result(
    unwired: List[UnwiredSymbol], skipped: List[str], total_checked: int,
    grep_failures: List[str], test_file_skipped: int,
) -> WiringGateResult:
    """The blocking result of a run whose caller analysis could not be done."""
    names = ", ".join(grep_failures[:5])
    tail = f" (+{len(grep_failures) - 5} more)" if len(grep_failures) > 5 else ""
    return WiringGateResult(
        status="fail",
        unwired=unwired,
        skipped=skipped,
        total_checked=total_checked,
        test_file_skipped=test_file_skipped,
        summary=f"grep failed for {len(grep_failures)} symbol(s) (treated as blocking): {names}{tail}",
    )


def analyse_diff(
    diff_text: str,
    *,
    state_dir: Optional[Path] = None,
    root: Optional[str] = None,
    required: Optional[bool] = None,
) -> WiringGateResult:
    """The gate's LOCAL half: analyse a diff and grep a checkout for its callers.

    No ``gh``, no network — ``deterministic_gate`` runs exactly this under the
    ``deterministic`` adapter kind (GE-6/ADR-046 §4). ``root`` is the checkout
    the caller analysis greps (``None`` keeps the historical working directory).
    ``required`` is the gate's blocking posture; ``None`` reads it from
    :data:`VNX_WIRING_GATE_REQUIRED` as the gate always has.

    Returns WiringGateResult with status:
      - "pass": all new public defs have callers
      - "fail": unwired defs found AND the gate is required
      - "advisory": unwired defs found but gate is in shadow mode
    """
    skip_list = _load_skip_list(state_dir)
    if required is None:
        import config_runtime
        required = config_runtime.get_bool("VNX_WIRING_GATE_REQUIRED")

    if not diff_text:
        return WiringGateResult(
            status="pass", total_checked=0,
            summary="Empty PR diff (no changes)",
        )

    new_defs, test_file_skipped = _extract_new_public_defs(diff_text)
    if not new_defs:
        left_out = f" ({test_file_skipped} test-file definition(s) skipped)" if test_file_skipped else ""
        return WiringGateResult(
            status="pass", total_checked=0, test_file_skipped=test_file_skipped,
            summary="No new public definitions in PR diff" + left_out,
        )

    unwired, skipped, grep_failures = _classify_symbols(new_defs, skip_list, root)
    total_checked = len(new_defs) - len(skipped)

    if grep_failures:
        return _grep_failure_result(
            unwired, skipped, total_checked, grep_failures, test_file_skipped,
        )

    if not unwired:
        return WiringGateResult(
            status="pass",
            unwired=[],
            skipped=skipped,
            total_checked=total_checked,
            test_file_skipped=test_file_skipped,
            summary=f"All {total_checked} new public definitions are wired",
        )

    status = "fail" if required else "advisory"
    names = ", ".join(s.name for s in unwired[:5])
    tail = f" (+{len(unwired) - 5} more)" if len(unwired) > 5 else ""
    summary = f"{len(unwired)} unwired symbol(s): {names}{tail}"

    return WiringGateResult(
        status=status,
        unwired=unwired,
        skipped=skipped,
        total_checked=total_checked,
        test_file_skipped=test_file_skipped,
        summary=summary,
    )


def check_pr_wiring(pr_number: int, *, state_dir: Optional[Path] = None) -> WiringGateResult:
    """Check a PR for unwired (dead) public definitions.

    The gate's GitHub half is exactly one call — ``gh pr diff`` — and it stays
    here; everything after it is :func:`analyse_diff`, the local half the
    ``deterministic`` adapter kind runs on its own (GE-6).

    Raises WiringGateError if the diff cannot be fetched — the gate runner
    must catch this and mark the gate outcome as FAILED, never PASS with 0 checks.
    """
    return analyse_diff(_get_pr_diff(pr_number), state_dir=state_dir)


# ---------------------------------------------------------------------------
# Tree mode: unused public definitions in the whole tree at a git ref
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"\w+")


def _git_out(repo: Optional[Path], *args: str, stdin: Optional[bytes] = None) -> bytes:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(repo) if repo else None, input=stdin,
            capture_output=True, timeout=300, check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        raise WiringGateError(f"git {' '.join(args[:2])} failed: {e}") from e
    return proc.stdout


def _read_py_at_ref(repo: Optional[Path], ref: str) -> Tuple[str, Dict[str, Optional[str]]]:
    """``(sha, {path: source})`` for every .py file at ``ref``; undecodable -> None."""
    sha = _git_out(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
    names = _git_out(repo, "ls-tree", "-r", "-z", "--name-only", sha).decode(
        "utf-8", "surrogateescape").split("\0")
    paths = [n for n in names if n.endswith(".py")]
    sources: Dict[str, Optional[str]] = {}
    for path in paths:
        blob = _git_out(repo, "show", f"{sha}:{path}")
        try:
            sources[path] = blob.decode("utf-8")
        except UnicodeDecodeError:
            sources[path] = None
    return sha, sources


# Tree-mode counting rule, version 4. Decorators that prove a definition is
# registered (the framework or registry calls it); every other decorator proves
# nothing and does not count. One entry per decorator, with the reason.
TREE_REGISTRATION_DECORATORS: Dict[str, str] = {
    "fastapi_route": (
        "@<app>.get/post/put/delete/patch(...) on a name bound to FastAPI(...) in the "
        "same module: the framework dispatches the request to the function"),
    "register_probe": (
        "@register_probe(...): the probe registry stores the class and the runner "
        "instantiates it by name"),
}
_FASTAPI_METHODS = frozenset({"get", "post", "put", "delete", "patch"})
TREE_DEFAULT_EXCLUDED_PREFIXES: Tuple[str, ...] = ("scripts/benchmark/field-tests/",)
TREE_SKIPS_PATH = "scripts/ci/wiring_tree_skips.yaml"
_SHELL_PY_HEREDOC_RE = re.compile(
    r"\bpython3?(?:\.\d+)?\b[^\n]*<<-?\s*(['\"]?)(\w+)\1")
_SHELL_PY_DASH_C_RE = re.compile(
    r"\bpython3?(?:\.\d+)?\s+(?:-\w+\s+)*-c\s+(\"(?:[^\"\\]|\\.)*\"|'[^']*')", re.DOTALL)


def _read_blob(repo: Optional[Path], sha: str, path: str) -> Optional[str]:
    blob = _git_out(repo, "show", f"{sha}:{path}")
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _shell_python_segments(text: str) -> str:
    """The Python inside a shell script: heredocs fed to python and ``python -c``."""
    parts: List[str] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        m = _SHELL_PY_HEREDOC_RE.search(lines[i])
        i += 1
        if not m:
            continue
        while i < len(lines) and lines[i].strip() != m.group(2):
            parts.append(lines[i])
            i += 1
    parts.extend(m.group(1) for m in _SHELL_PY_DASH_C_RE.finditer(text))
    return "\n".join(parts)


def _non_python_tokens(repo: Optional[Path], sha: str) -> set:
    """Identifier tokens in the Python of ``bin/vnx`` and every ``*.sh`` at ``sha``."""
    names = _git_out(repo, "ls-tree", "-r", "-z", "--name-only", sha).decode(
        "utf-8", "surrogateescape").split("\0")
    tokens: set = set()
    for path in names:
        if path != "bin/vnx" and not path.endswith(".sh"):
            continue
        text = _read_blob(repo, sha, path)
        if text is not None:
            tokens.update(_WORD_RE.findall(_shell_python_segments(text)))
    return tokens


def _name_refs(node: ast.AST) -> set:
    refs = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            refs.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            refs.add(sub.attr)
    return refs


def _fastapi_app_names(tree: ast.AST) -> set:
    names = set()
    for node in ast.iter_child_nodes(tree):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            continue
        func = node.value.func
        if (getattr(func, "id", None) or getattr(func, "attr", None)) == "FastAPI":
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _is_registration_decorator(dec: ast.AST, app_names: set) -> bool:
    if not isinstance(dec, ast.Call):
        return False
    func = dec.func
    if isinstance(func, ast.Name):
        return func.id == "register_probe"
    if isinstance(func, ast.Attribute):
        if func.attr == "register_probe":
            return True
        return (func.attr in _FASTAPI_METHODS and isinstance(func.value, ast.Name)
                and func.value.id in app_names)
    return False


def _wired_without_other_files(tree: ast.AST) -> set:
    """Top-level names used elsewhere in their own file, or registered by a decorator."""
    app_names = _fastapi_app_names(tree)
    nodes = list(ast.iter_child_nodes(tree))
    refs = [_name_refs(n) for n in nodes]
    wired = set()
    for i, node in enumerate(nodes):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if any(_is_registration_decorator(d, app_names) for d in node.decorator_list):
            wired.add(node.name)
        elif any(node.name in refs[j] for j in range(len(nodes)) if j != i):
            wired.add(node.name)
    return wired


def _tree_definitions(
    sources: Dict[str, Optional[str]], prefix: str, excluded: Tuple[str, ...] = (),
) -> Tuple[List[dict], List[str], int]:
    """Public defs under ``prefix`` (tests, excluded prefixes left out), the unparsable
    files. Each def carries ``self_wired`` (own-file use or a registration decorator)."""
    defs: List[dict] = []
    unparsed: List[str] = []
    files = 0
    for path in sorted(sources):
        if not path.startswith(prefix) or _is_test_file(path) or path.startswith(excluded):
            continue
        files += 1
        try:
            if sources[path] is None:
                raise ValueError("not utf-8")
            tree = ast.parse(sources[path])
        except (SyntaxError, ValueError, RecursionError):
            unparsed.append(path)
            continue
        wired = _wired_without_other_files(tree)
        for d in _public_defs_of_tree(tree, path, {}):
            d["self_wired"] = d["name"] in wired
            defs.append(d)
    return defs, unparsed, files


def _token_index(sources: Dict[str, Optional[str]]) -> Dict[str, set]:
    """{identifier token: files holding it}; an unreadable file contributes nothing."""
    index: Dict[str, set] = {}
    for path, text in sources.items():
        if text is None:
            continue
        for token in set(_WORD_RE.findall(text)):
            index.setdefault(token, set()).add(path)
    return index


def _has_caller(name: str, def_file: str, index: Dict[str, set]) -> bool:
    """grep -l semantics (substring) over the index, with :func:`_counts_as_caller`."""
    for token, hit_files in index.items():
        if name in token and any(_counts_as_caller(f, def_file) for f in hit_files):
            return True
    return False


def _load_tree_skips(repo: Optional[Path], sha: str) -> set:
    """``{(name, file)}`` from the tracked skip file at ``sha``; an entry without a
    reason, or a malformed file, raises WiringGateError (exit 2). Absent file = none."""
    try:
        text = _read_blob(repo, sha, TREE_SKIPS_PATH)
    except WiringGateError:
        return set()  # the file does not exist at this ref
    try:
        data = yaml.safe_load(text) if text is not None else ValueError("not utf-8")
    except yaml.YAMLError as e:
        raise WiringGateError(f"{TREE_SKIPS_PATH} is not valid YAML: {e}") from e
    if isinstance(data, ValueError) or not isinstance(data, list):
        raise WiringGateError(f"{TREE_SKIPS_PATH} must be a list of {{name, file, reason}}")
    skips = set()
    for i, entry in enumerate(data):
        fields = [entry.get(k) if isinstance(entry, dict) else None
                  for k in ("name", "file", "reason")]
        if not all(isinstance(v, str) and v.strip() for v in fields):
            raise WiringGateError(
                f"{TREE_SKIPS_PATH} entry {i} needs a non-empty name, file and reason")
        skips.add((fields[0], fields[1]))
    return skips


def check_tree_wiring(
    ref: str = "HEAD", *, prefix: str = "scripts/",
    state_dir: Optional[Path] = None, repo: Optional[Path] = None,
    exclude_prefixes: Tuple[str, ...] = (),
) -> dict:
    """Unused public definitions under ``prefix`` in the tree at ``ref`` (rule v4).

    Reads blobs with ``git show`` (never the working tree). A definition is used when
    another non-test file names it, when its own file uses it outside its own body, when
    ``bin/vnx`` or a ``*.sh`` Python segment names it, or when a proven registration
    decorator wires it. Skips: the runtime ``wiring_skip.yaml`` plus the tracked
    ``scripts/ci/wiring_tree_skips.yaml`` at ``ref``. A file that cannot be parsed is
    listed under ``unparsed``, never counted as zero.
    Raises WiringGateError when the ref cannot be read or the tracked skip file is bad.
    """
    skip_list = _load_skip_list(state_dir)
    sha, sources = _read_py_at_ref(repo, ref)
    tree_skips = _load_tree_skips(repo, sha)
    excluded = tuple(dict.fromkeys(TREE_DEFAULT_EXCLUDED_PREFIXES + tuple(exclude_prefixes)))
    defs, unparsed, n_files = _tree_definitions(sources, prefix, excluded)
    index = _token_index(sources)
    shell_tokens = _non_python_tokens(repo, sha)
    files: Dict[str, List[dict]] = {}
    skipped = 0
    unused = 0
    for d in defs:
        if d["name"] in skip_list or (d["name"], d["file"]) in tree_skips:
            skipped += 1
            continue
        if d["self_wired"] or d["name"] in shell_tokens or _has_caller(
                d["name"], d["file"], index):
            continue
        unused += 1
        files.setdefault(d["file"], []).append(
            {"name": d["name"], "line": d["line"], "kind": d["kind"]})
    return {
        "ref": ref, "sha": sha, "prefix": prefix, "files": files, "unparsed": unparsed,
        "excluded_prefixes": list(excluded),
        "totals": {"files": n_files, "definitions": len(defs), "skipped": skipped,
                   "unused": unused, "unparsed": len(unparsed)},
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Wiring gate tree mode")
    ap.add_argument("--tree", action="store_true", required=True)
    ap.add_argument("--ref", default="HEAD")
    ap.add_argument("--prefix", default="scripts/")
    ap.add_argument("--exclude-prefix", action="append", default=[],
                    help="path prefix left out of the tree mode (repeatable)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        result = check_tree_wiring(args.ref, prefix=args.prefix,
                                   exclude_prefixes=tuple(args.exclude_prefix))
    except WiringGateError as e:
        print(f"wiring_gate: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        t = result["totals"]
        print(f"{result['ref']} {result['sha']}: {t['files']} files, {t['definitions']} "
              f"definitions, {t['unused']} unused, {t['unparsed']} unparsed "
              f"(excluded: {', '.join(result['excluded_prefixes']) or 'none'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
