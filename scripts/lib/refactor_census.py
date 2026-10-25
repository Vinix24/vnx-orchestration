#!/usr/bin/env python3
"""Repeatable census of refactor candidates and AI slop. Reports; never gates.
A commit is read through git objects only (git ls-tree + git show), never the
working tree; without --commit the tracked files of the working tree are read.
  python3 scripts/lib/refactor_census.py --commit <sha> [--paths p1,p2] [--json]
  PYTHONPATH=scripts/lib python3 -m refactor_census --commit <sha> --json
"""

from __future__ import annotations

import argparse, ast, hashlib, io, json, re, subprocess, sys, tokenize
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from function_size_gate import measure_source

EXCLUDED = frozenset({".git", ".vnx-data", "venv", ".venv", "__pycache__"})
RAW_THRESHOLDS = (100, 150, 200, 300, 400)
EXECUTABLE_THRESHOLD, DUPLICATE_MIN_RAW, BANNER_LOOKAHEAD = 70, 30, 3
RESTATING_OVERLAP, MIN_WORD_LENGTH = 0.6, 3
BANNER_RE = re.compile(r"#\s*-+\s*step\s+\S+", re.IGNORECASE)
VERSION_SUFFIX_RE = re.compile(r"(_v[0-9]+|_fixed|_new|_old|_bak|_copy)", re.IGNORECASE)
DEFENSIVE_RE = re.compile(r"just in case|impossible|should never happen|cannot happen", re.IGNORECASE)
SEED_RE = re.compile(r"^scripts/benchmark/field-tests/tasks/.*/seed/")
DEFENSIVE_PREFIXES = ("scripts/", "vnx_cli/", "dashboard/")
CORPUS_SUFFIXES = (".py", ".sh", ".md", ".yml", ".yaml", ".json", ".ts", ".tsx", ".toml")
IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
WORD_RE = re.compile(r"[a-z0-9_]+")


class CensusError(RuntimeError):
    """The census cannot complete; no partial count is returned."""


def _git(repo: Path, *args: str) -> bytes:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    except OSError as exc:
        raise CensusError(f"cannot run git: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise CensusError(f"git {' '.join(args)} failed: {detail or proc.returncode}")
    return proc.stdout


def _source(root_or_commit: Any, repo: Optional[str]) -> tuple[Path, Optional[str]]:
    """(repository root, commit) — commit is None for a working-tree census."""
    path = Path(str(root_or_commit))
    if path.is_dir():
        return path.resolve(), None
    start = Path(repo).resolve() if repo else Path.cwd()
    return Path(_git(start, "rev-parse", "--show-toplevel").decode().strip()), str(root_or_commit)


def _files(root: Path, commit: Optional[str]) -> list[str]:
    raw = _git(root, "ls-tree", "-r", "-z", "--name-only", commit) if commit else _git(root, "ls-files", "-z")
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _text(root: Path, commit: Optional[str], path: str) -> str:
    if commit:
        return _git(root, "show", f"{commit}:{path}").decode("utf-8", "replace")
    return (root / path).read_bytes().decode("utf-8", "replace")


def _selectors(paths: Optional[Any]) -> list[str]:
    if not paths:
        return []
    items = paths.split(",") if isinstance(paths, str) else list(paths)
    return [str(item).strip().strip("/") for item in items if str(item).strip().strip("/")]


def long_functions(trees: dict[str, ast.Module], texts: dict[str, str]) -> dict[str, Any]:
    counts = {threshold: 0 for threshold in RAW_THRESHOLDS}
    items, executable_over, function_count = [], 0, 0
    for path in sorted(trees):
        measured = measure_source(texts[path], filename=path)
        spans = {node.lineno: node.end_lineno - node.lineno + 1 for node in ast.walk(trees[path])
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        function_count += len(measured)
        for function in measured:
            if function.lineno not in spans:
                raise CensusError(f"{path}:{function.lineno}: function missing from the AST walk")
            raw = spans[function.lineno]
            executable_over += int(function.executable_lines > EXECUTABLE_THRESHOLD)
            for threshold in RAW_THRESHOLDS:
                counts[threshold] += int(raw >= threshold)
            if raw >= RAW_THRESHOLDS[0]:
                items.append({"path": path, "line": function.lineno, "qualname": function.qualname,
                              "raw_lines": raw, "executable_lines": function.executable_lines})
    items.sort(key=lambda item: (-item["raw_lines"], item["path"], item["line"]))
    return {"counts": {f"raw_ge_{t}": counts[t] for t in RAW_THRESHOLDS},
            "executable_over_70": executable_over, "function_count": function_count, "items": items}


def _body_hash(node: ast.AST) -> str:
    body = list(node.body)
    if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return hashlib.sha256(ast.dump(ast.Module(body=body, type_ignores=[])).encode("utf-8")).hexdigest()


def exact_duplicates(trees: dict[str, ast.Module]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in sorted(trees):
        for node in ast.walk(trees[path]):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                raw = node.end_lineno - node.lineno + 1
                if raw >= DUPLICATE_MIN_RAW:
                    groups[_body_hash(node)].append(
                        {"path": path, "line": node.lineno, "name": node.name, "raw_lines": raw})
    items = [{"sha256": digest, "members": sorted(members, key=lambda m: (m["path"], m["line"]))}
             for digest, members in groups.items() if len(members) >= 2]
    items.sort(key=lambda group: (-len(group["members"]), group["members"][0]["path"]))
    return {"group_count": len(items), "function_count": sum(len(g["members"]) for g in items), "items": items}


def _comments(source: str) -> list[tuple[int, str]]:
    try:
        return [(token.start[0], token.string) for token in tokenize.generate_tokens(io.StringIO(source).readline)
                if token.type == tokenize.COMMENT]
    except (tokenize.TokenError, IndentationError):
        return [(n, line.strip()) for n, line in enumerate(source.splitlines(), 1)
                if line.strip().startswith("#")]


def doubled_banners(texts: dict[str, str]) -> dict[str, Any]:
    items = []
    for path in sorted(texts):
        lines, comments = texts[path].splitlines(), _comments(texts[path])
        for index, (lineno, text) in enumerate(comments):
            if not lines[lineno - 1].strip().startswith("#") or not BANNER_RE.search(text):
                continue
            for later_line, later_text in comments[index + 1:]:
                if later_line - lineno > BANNER_LOOKAHEAD:
                    break
                if later_text == text:
                    items.append({"path": path, "line": lineno, "text": text.strip()})
                    break
    return {"count": len(items), "items": items}


def version_suffixed_paths(tracked: list[str]) -> dict[str, Any]:
    items = [path for path in tracked if VERSION_SUFFIX_RE.search(path)]
    return {"count": len(items), "items": items}


def defensive_markers(texts: dict[str, str]) -> dict[str, Any]:
    items = [{"path": path, "line": lineno, "text": comment.strip()}
             for path in sorted(texts) if path.startswith(DEFENSIVE_PREFIXES) and not SEED_RE.match(path)
             for lineno, comment in _comments(texts[path]) if DEFENSIVE_RE.search(comment)]
    return {"count": len(items), "items": items}


def _identifier_counts(texts: dict[str, str], tracked: list[str], root: Path, commit: Optional[str]) -> Counter:
    return Counter(
        word for path in tracked if path.endswith(CORPUS_SUFFIXES)
        for word in IDENTIFIER_RE.findall(texts.get(path) or _text(root, commit, path))
    )


def dead_module_functions(trees: dict[str, ast.Module], texts: dict[str, str], counts: Counter) -> dict[str, Any]:
    items = []
    for path in sorted(trees):
        if path.startswith("tests/"):
            continue
        lines = texts[path].splitlines()
        for node in trees[path].body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name.startswith("test_"):
                continue
            own = sum(1 for m in IDENTIFIER_RE.finditer(lines[node.lineno - 1]) if m.group() == node.name)
            if counts[node.name] - own <= 0:
                items.append({"path": path, "line": node.lineno, "name": node.name,
                              "in_seed": bool(SEED_RE.match(path))})
    outside = sum(1 for item in items if not item["in_seed"])
    return {"count": len(items), "outside_seeds": outside, "seeds": len(items) - outside, "items": items}


def _words(text: str) -> set[str]:
    return {word for word in WORD_RE.findall(text.lower()) if len(word) >= MIN_WORD_LENGTH}


def _next_code_line(lines: list[str], lineno: int) -> Optional[int]:
    for index in range(lineno, len(lines)):
        stripped = lines[index].strip()
        if stripped and not stripped.startswith("#"):
            return index
    return None


def restating_comments(texts: dict[str, str]) -> dict[str, Any]:
    items = []
    for path in sorted(texts):
        lines = texts[path].splitlines()
        for lineno, comment in _comments(texts[path]):
            comment_words = _words(comment)
            full_line = 0 < lineno <= len(lines) and lines[lineno - 1].strip().startswith("#")
            next_index = _next_code_line(lines, lineno) if full_line else None
            if not comment_words or next_index is None:
                continue
            overlap = len(comment_words & _words(lines[next_index])) / len(comment_words)
            if overlap >= RESTATING_OVERLAP:
                items.append({"path": path, "line": lineno, "comment": comment.strip(),
                              "next_line": lines[next_index].strip(), "overlap": round(overlap, 3)})
    return {"count": len(items), "file_count": len({item["path"] for item in items}), "items": items}


def census(root_or_commit: Any, paths: Optional[Any] = None, repo: Optional[str] = None) -> dict[str, Any]:
    root, commit = _source(root_or_commit, repo)
    selectors = _selectors(paths)
    tracked = [path for path in _files(root, commit)
               if not any(part in EXCLUDED for part in path.split("/"))
               and (not selectors or any(path == s or path.startswith(s + "/") for s in selectors))]
    python_files = [path for path in tracked if path.endswith(".py")]
    trees, texts, unparsed = {}, {}, []
    for path in python_files:
        text = _text(root, commit, path)
        try:
            trees[path] = ast.parse(text, filename=path)
        except SyntaxError:
            unparsed.append(path)
            continue
        texts[path] = text
    long_result = long_functions(trees, texts)
    counts = _identifier_counts(texts, tracked, root, commit)
    return {
        "source": commit or str(root), "commit": commit, "tracked_files": len(tracked),
        "python_files": len(python_files), "functions": long_result["function_count"],
        "unparsed": sorted(unparsed), "long_functions": long_result,
        "exact_duplicates": exact_duplicates(trees), "doubled_banners": doubled_banners(texts),
        "version_suffixed_paths": version_suffixed_paths(tracked),
        "defensive_markers": defensive_markers(texts), "dead_module_functions": dead_module_functions(trees, texts, counts),
        "restating_comments": restating_comments(texts),
    }


def render_table(result: dict[str, Any]) -> str:
    long_result, dupes, dead = result["long_functions"], result["exact_duplicates"], result["dead_module_functions"]
    count = long_result["counts"]
    rows = [
        ("python files", result["python_files"], f"{result['functions']} functions"),
        ("unparsed", len(result["unparsed"]), ", ".join(result["unparsed"])),
        ("long functions", count["raw_ge_100"], ", ".join(f"raw>={t} {count[f'raw_ge_{t}']}" for t in RAW_THRESHOLDS)
         + f"; executable>{EXECUTABLE_THRESHOLD} {long_result['executable_over_70']}"),
        ("exact duplicates", dupes["group_count"], f"{dupes['function_count']} functions in groups"),
        ("doubled step banners", result["doubled_banners"]["count"], ""),
        ("version-suffixed paths", result["version_suffixed_paths"]["count"], ""),
        ("defensive markers", result["defensive_markers"]["count"], ""),
        ("dead module functions", dead["count"], f"{dead['outside_seeds']} outside seeds, {dead['seeds']} in seeds"),
        ("restating comments", result["restating_comments"]["count"], f"{result['restating_comments']['file_count']} files"),
    ]
    width = max(len(row[0]) for row in rows)
    return "\n".join([f"Refactor census — {result['source']}", ""] +
                     [f"{label:<{width}}  {value:>6}  {text}".rstrip() for label, value, text in rows])


def main(argv: Optional[Any] = None) -> int:
    parser = argparse.ArgumentParser(description="Count refactor candidates and AI slop in a tree or commit.")
    parser.add_argument("--commit", help="read this commit through git objects (default: working tree)")
    parser.add_argument("--paths", help="comma-separated files or directories that narrow the set")
    parser.add_argument("--json", action="store_true", help="machine-readable summary with per-class items")
    parser.add_argument("--repo", help="repository root for --commit (default: discovered from cwd)")
    args = parser.parse_args(argv)
    try:
        result = census(args.commit or args.repo or ".", paths=args.paths, repo=args.repo)
    except CensusError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2) if args.json else render_table(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
