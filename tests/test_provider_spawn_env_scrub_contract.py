"""tests/test_provider_spawn_env_scrub_contract.py — a call-site guard over
``scripts/lib/provider_spawns/`` for OI-1619, mirroring
tests/test_spawn_scrub_env_keys_contract.py's AST-scan pattern (itself mirroring
tests/test_phantom_guard_context_contract.py's, PR #1748/OI-1614) rather than
inventing a second mechanism.

Defect this closes: kimi_spawn.py, codex_spawn.py, and gemini_spawn.py each Popen a
real worker CLI directly — they have no SubprocessAdapter to route through, so they
are NOT covered by test_spawn_scrub_env_keys_contract.py's guard (which only owns
``spawn_claude(...)`` and ``<SubprocessAdapter-instance>.deliver(...)`` call sites).
All three built their child env as ``{**os.environ, **(extra_env or {})}`` with ZERO
scrub call of any kind — not even litellm_spawn.py's own narrower
``_scrubbed_env()``. Measured 2026-09-04 (mirrors the claude-lane finding of
2026-09-03): a secret present in the parent process (``VNX_SMTP_PASS``) would cross
straight into the kimi/codex/gemini subprocess.

The fix (env_scrub_patterns.py) adds ``scrub_env()`` — a small fnmatch-based free
function mirroring SubprocessAdapter.deliver()'s inline scrub loop, for a caller that
Popens directly and has no adapter to route the scrub through — and calls it with
``DEFAULT_SCRUB_KEY_PATTERNS`` at the one place each of kimi_spawn.py, codex_spawn.py,
and gemini_spawn.py builds its Popen env.

This test is the STATIC backstop: an AST scan over every git-tracked, non-test .py
file under ``scripts/lib/provider_spawns/`` (the directory every provider spawn lane
lives in — extraction convention shared by every file there, see each module's own
"Extracted in Wave 4.6" header) for a ``subprocess.Popen(...)`` / ``subprocess.run(...)``
call carrying an ``env=`` kwarg whose value does not resolve to ``scrub_env(...)``,
directly or through exactly one named local helper (e.g. litellm_spawn.py's own
``_scrubbed_env()``, which delegates to ``scrub_env()`` with its own, narrower
pattern set — see its own CREDENTIAL SAFETY docstring). A brand-new provider_spawns
file added later that Popens a worker CLI with a raw ``{**os.environ, ...}`` env and
no scrub call is caught by this scan automatically, the same "catch the lane that
doesn't exist yet" property the phantom-guard and claude-scrub call-site guards both
have.

Round 1 of this guard trusted ANY function named ``scrub_env`` or ``_scrubbed_env`` by
NAME alone — a real gap, found in review: litellm_spawn.py's ``_scrubbed_env()``
originally hand-rolled its own two-key strip (``ANTHROPIC_API_KEY`` +
``CLAUDE_CODE_OAUTH_TOKEN``, exact-key only) with no call to the shared
``scrub_env()`` at all, and the guard called it safe purely because of its name — it
would have said the same about ANY function named ``_scrubbed_env``, including one
that scrubbed nothing. This round replaces the name allowlist with a PROPERTY check:
``_call_resolves_to_scrub_env()`` requires the call target to be ``scrub_env`` itself,
OR a locally-defined function whose OWN body (walked via ``ast.walk``, any depth
within that one function) contains a real call to ``scrub_env(...)``. One hop of
named-helper indirection is resolved; a helper that calls a second, differently-named
helper that itself calls ``scrub_env`` is NOT resolved and fails closed as unsafe
(over-flag rather than under-flag — the safe failure direction for a security guard).
litellm_spawn.py's real ``_scrubbed_env()`` (a single function whose body directly
calls ``scrub_env(env, _LITELLM_SCRUB_PATTERNS)``) resolves cleanly under this rule.

A ``subprocess.Popen(...)``/``subprocess.run(...)`` call with NO ``env=`` kwarg at all
(e.g. kimi_spawn.py's internal ``git status``/``git diff`` calls, which inherit the
ambient env implicitly like any ordinary trusted-tool shell-out) is out of this guard's
scope — the defect class it exists for is a WORKER-CLI subprocess receiving a
merged/inherited env explicitly, not every subprocess call in the directory.

claude_spawn.py, glm_harness_spawn.py, and deepseek_harness_spawn.py never call
subprocess.Popen/run directly (they route through SubprocessAdapter.deliver(), already
covered by test_spawn_scrub_env_keys_contract.py) so they never match this guard's
Popen/run+env= pattern — no special-casing needed for them.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

REPO = Path(__file__).resolve().parent.parent
TARGET_DIR = REPO / "scripts" / "lib" / "provider_spawns"

_POPEN_METHODS = frozenset({"Popen", "run"})
_SCRUB_ENV_NAME = "scrub_env"
_ENV_KWARG = "env"


def _git_tracked_py_files(root: Path, subdir: Path) -> list[Path]:
    """Absolute paths to every git-tracked ``.py`` file under *subdir* (relative to
    *root*). Tracked-ness, not a directory namelist, keeps a build artifact / nested
    worktree / CI rollout copy out of the scan. Fails CLOSED — raises
    ``RuntimeError`` — when git is unavailable (OI-1597 precedent), rather than
    silently falling back to an incomplete namelist.
    """
    rel_subdir = subdir.relative_to(root)
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", f"{rel_subdir}/*.py"],
            cwd=str(root), capture_output=True, timeout=10,
        )
    except OSError as exc:
        raise RuntimeError(
            f"git is not available to determine tracked .py files under {subdir}"
        ) from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"`git ls-files` failed under {root} (not a git work tree?): {stderr}")
    stdout = result.stdout.decode("utf-8")
    return [root / rel for rel in stdout.split("\0") if rel]


def _call_target_name(node: ast.AST) -> Optional[str]:
    """The bare callee name of an ast.Call — 'foo' for both foo(...) and mod.foo(...)."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_subprocess_popen_or_run(node: ast.Call) -> bool:
    """True for ``subprocess.Popen(...)`` / ``subprocess.run(...)`` specifically —
    not any unrelated object's same-named ``.Popen``/``.run`` method."""
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in _POPEN_METHODS
        and isinstance(func.value, ast.Name)
        and func.value.id == "subprocess"
    )


def _function_defs_by_name(tree: ast.Module) -> "dict[str, ast.AST]":
    """Map of function name -> its FunctionDef/AsyncFunctionDef node, module-wide
    (last definition wins on a name collision — a theoretical, not real, edge case
    in this repo's small provider_spawns modules)."""
    defs: "dict[str, ast.AST]" = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs[node.name] = node
    return defs


def _calls_scrub_env(node: ast.AST) -> bool:
    """True iff a direct call to ``scrub_env(...)`` appears anywhere in *node*'s
    subtree (any statement, any nesting depth within that one function body)."""
    return any(
        isinstance(sub, ast.Call) and _call_target_name(sub) == _SCRUB_ENV_NAME
        for sub in ast.walk(node)
    )


def _call_resolves_to_scrub_env(call_name: str, funcs: "dict[str, ast.AST]") -> bool:
    """PROPERTY check, not a name allowlist: *call_name* is safe when it IS
    ``scrub_env`` itself, or when it names a function defined in this module whose
    OWN body actually calls ``scrub_env(...)`` — e.g. litellm_spawn.py's
    ``_scrubbed_env()``, which delegates to ``scrub_env()`` with its own pattern set.
    A function with any other name, or one that does not itself call ``scrub_env``
    (however it's named), resolves to False. Only one hop of named-helper
    indirection is resolved — a helper that calls a second, differently-named helper
    which itself calls ``scrub_env`` is NOT resolved and fails closed as unsafe."""
    if call_name == _SCRUB_ENV_NAME:
        return True
    func = funcs.get(call_name)
    if func is None:
        return False
    return _calls_scrub_env(func)


def _collect_safe_assigned_names(tree: ast.Module, funcs: "dict[str, ast.AST]") -> set[str]:
    """Names of local variables whose assigned expression contains (anywhere in its
    subtree) a call that resolves to ``scrub_env`` under ``_call_resolves_to_scrub_env``.
    Deliberately module-wide and scope-blind — same "good enough, no false negatives
    in this repo today" tradeoff documented in
    test_spawn_scrub_env_keys_contract.py's ``_subprocess_adapter_var_names``.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        contains_safe_call = any(
            isinstance(sub, ast.Call)
            and _call_resolves_to_scrub_env(_call_target_name(sub) or "", funcs)
            for sub in ast.walk(node.value)
        )
        if not contains_safe_call:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _env_kwarg_value(call: ast.Call) -> "tuple[bool, Optional[ast.AST]]":
    """(has_env_kwarg, value_node) for the ``env=`` keyword on *call*, if any."""
    for kw in call.keywords:
        if kw.arg == _ENV_KWARG:
            return True, kw.value
    return False, None


def _env_value_is_safe(value: ast.AST, safe_names: set[str], funcs: "dict[str, ast.AST]") -> bool:
    if isinstance(value, ast.Call):
        name = _call_target_name(value)
        if name and _call_resolves_to_scrub_env(name, funcs):
            return True
    if isinstance(value, ast.Name):
        return value.id in safe_names
    return False


def _find_popen_env_sites(
    root: Path, target_dir: Path,
) -> dict[str, list[tuple[int, bool]]]:
    """Map of relative-path -> list of (lineno, is_safe) for every
    ``subprocess.Popen(...)``/``subprocess.run(...)`` call THAT CARRIES AN ``env=``
    kwarg, over the git-tracked .py files rooted at *target_dir*. A call with no
    ``env=`` kwarg at all is not a site (out of this guard's scope — see module
    docstring).

    An unparseable or non-UTF-8 file raises a clean AssertionError naming the file,
    instead of letting the raw SyntaxError/UnicodeDecodeError traceback escape.
    """
    found: dict[str, list[tuple[int, bool]]] = {}
    for full_path in _git_tracked_py_files(root, target_dir):
        rel = full_path.relative_to(root)
        try:
            source = full_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(full_path))
        except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
            raise AssertionError(
                f"cannot parse {rel} while scanning for Popen/run env= call sites: {exc}"
            ) from exc

        funcs = _function_defs_by_name(tree)
        safe_names = _collect_safe_assigned_names(tree, funcs)
        sites: list[tuple[int, bool]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_subprocess_popen_or_run(node):
                continue
            has_env, value = _env_kwarg_value(node)
            if not has_env:
                continue
            is_safe = _env_value_is_safe(value, safe_names, funcs)
            sites.append((node.lineno, is_safe))
        if sites:
            found[str(rel)] = sites
    return found


_KNOWN_CALL_SITE_FILES = frozenset({
    "scripts/lib/provider_spawns/kimi_spawn.py",
    "scripts/lib/provider_spawns/codex_spawn.py",
    "scripts/lib/provider_spawns/gemini_spawn.py",
    "scripts/lib/provider_spawns/litellm_spawn.py",
})


class TestEveryProviderSpawnPopenSupplessScrubbedEnv:
    """The wachter: every real, git-tracked subprocess.Popen()/subprocess.run() call
    under scripts/lib/provider_spawns/ that carries an env= kwarg must resolve that
    kwarg to scrub_env(...), directly or through one named local helper whose own
    body actually calls it (see _call_resolves_to_scrub_env — a property check, not
    a name allowlist). This does not enumerate a fixed baseline of files (unlike a
    growth-tripwire test) — a brand-new provider spawn lane is picked up
    automatically by the git-tracked-.py scan and held to the same bar, without this
    test needing to be edited."""

    def test_known_call_sites_are_found(self):
        sites = _find_popen_env_sites(REPO, TARGET_DIR)
        missing = _KNOWN_CALL_SITE_FILES - set(sites)
        assert not missing, f"expected call site(s) not found by the scan: {missing}"

    def test_every_popen_env_kwarg_resolves_to_a_scrub_call(self):
        sites = _find_popen_env_sites(REPO, TARGET_DIR)
        assert sites, "expected at least the four known call-site files"
        problems = []
        for rel, calls in sites.items():
            for lineno, is_safe in calls:
                if not is_safe:
                    problems.append(f"{rel}:{lineno}")
        assert not problems, (
            "subprocess.Popen()/subprocess.run() call(s) under provider_spawns/ pass "
            "an env= kwarg that does not resolve (directly, or through one named "
            "local helper that itself calls it) to scrub_env(...) — the worker "
            "subprocess would inherit the full ambient environment, secrets "
            "included:\n" + "\n".join(problems)
        )


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)


def _git_add(root: Path, *relative_paths: str) -> None:
    subprocess.run(["git", "add", "--", *relative_paths], cwd=str(root), check=True)


_GOOD_DIRECT_CALL_SITE = (
    "import subprocess\n"
    "from env_scrub_patterns import DEFAULT_SCRUB_KEY_PATTERNS, scrub_env\n"
    "proc = subprocess.Popen(\n"
    "    cmd, env=scrub_env({**__import__('os').environ}, DEFAULT_SCRUB_KEY_PATTERNS),\n"
    ")\n"
)

_GOOD_VAR_CALL_SITE = (
    "import os\n"
    "import subprocess\n"
    "from env_scrub_patterns import DEFAULT_SCRUB_KEY_PATTERNS, scrub_env\n"
    "env = scrub_env({**os.environ}, DEFAULT_SCRUB_KEY_PATTERNS)\n"
    "proc = subprocess.Popen(cmd, env=env)\n"
)

_GOOD_INDIRECT_HELPER_SITE = (
    "import os\n"
    "import subprocess\n"
    "from env_scrub_patterns import scrub_env\n"
    "def _my_arbitrarily_named_helper(extra_env):\n"
    "    env = {**os.environ, **(extra_env or {})}\n"
    "    return scrub_env(env, frozenset({'*_PASS'}))\n"
    "env = _my_arbitrarily_named_helper(None)\n"
    "proc = subprocess.Popen(cmd, env=env)\n"
)

_BAD_SCRUBBED_ENV_NAMED_HELPER_THAT_NEVER_CALLS_SCRUB_ENV = (
    "import os\n"
    "import subprocess\n"
    "def _scrubbed_env(extra_env):\n"
    "    env = {**os.environ, **(extra_env or {})}\n"
    "    env.pop('ANTHROPIC_API_KEY', None)\n"
    "    return env\n"
    "env = _scrubbed_env(None)\n"
    "proc = subprocess.Popen(cmd, env=env)\n"
)


class TestCallSiteGuardCanActuallyFail:
    """Een wachter die je niet hebt zien falen, bewaakt niets. ``_find_popen_env_sites``
    and ``_env_value_is_safe`` are exercised directly against isolated fixture trees
    (never the real repo) so this guard's MECHANISM is proven, not just today's three
    call sites."""

    def test_a_compliant_direct_scrub_call_site_passes(self, tmp_path):
        _git_init(tmp_path)
        (tmp_path / "provider_spawns").mkdir()
        (tmp_path / "provider_spawns" / "good.py").write_text(_GOOD_DIRECT_CALL_SITE, encoding="utf-8")
        _git_add(tmp_path, "provider_spawns/good.py")

        sites = _find_popen_env_sites(tmp_path, tmp_path / "provider_spawns")
        assert sites, "expected the scan to find the call site"
        for calls in sites.values():
            for _lineno, is_safe in calls:
                assert is_safe

    def test_a_compliant_var_scrub_call_site_passes(self, tmp_path):
        _git_init(tmp_path)
        (tmp_path / "provider_spawns").mkdir()
        (tmp_path / "provider_spawns" / "good.py").write_text(_GOOD_VAR_CALL_SITE, encoding="utf-8")
        _git_add(tmp_path, "provider_spawns/good.py")

        sites = _find_popen_env_sites(tmp_path, tmp_path / "provider_spawns")
        assert sites
        for calls in sites.values():
            for _lineno, is_safe in calls:
                assert is_safe

    def test_a_compliant_indirect_helper_with_an_arbitrary_name_passes(self, tmp_path):
        """PROPERTY, not name: a locally-defined helper whose own body actually calls
        scrub_env(...) is trusted regardless of what it's called — this is what makes
        litellm_spawn.py's real _scrubbed_env() (a different name, same property)
        resolve, without hardcoding that one name into the guard."""
        _git_init(tmp_path)
        (tmp_path / "provider_spawns").mkdir()
        (tmp_path / "provider_spawns" / "good.py").write_text(_GOOD_INDIRECT_HELPER_SITE, encoding="utf-8")
        _git_add(tmp_path, "provider_spawns/good.py")

        sites = _find_popen_env_sites(tmp_path, tmp_path / "provider_spawns")
        assert sites
        for calls in sites.values():
            for _lineno, is_safe in calls:
                assert is_safe

    def test_a_helper_merely_named_scrubbed_env_that_never_calls_scrub_env_turns_the_assertion_red(self, tmp_path):
        """The literal loophole Round 1 of this guard had: it trusted ANY function
        named scrub_env or _scrubbed_env by NAME alone. litellm_spawn.py's real
        _scrubbed_env() happened to be safe, but the guard could not tell that apart
        from a same-named function that scrubs nothing at all — this fixture IS that
        function. The property check must reject it precisely because its body never
        calls scrub_env(...), regardless of its name."""
        _git_init(tmp_path)
        (tmp_path / "provider_spawns").mkdir()
        (tmp_path / "provider_spawns" / "bad.py").write_text(
            _BAD_SCRUBBED_ENV_NAMED_HELPER_THAT_NEVER_CALLS_SCRUB_ENV, encoding="utf-8",
        )
        _git_add(tmp_path, "provider_spawns/bad.py")

        sites = _find_popen_env_sites(tmp_path, tmp_path / "provider_spawns")
        problems = [
            (rel, lineno)
            for rel, calls in sites.items()
            for lineno, is_safe in calls
            if not is_safe
        ]
        assert problems == [("provider_spawns/bad.py", 8)]

    def test_a_raw_os_environ_merge_with_no_scrub_call_turns_the_assertion_red(self, tmp_path):
        """The literal historic defect: kimi/codex/gemini built
        env = {**os.environ, **(extra_env or {})} and Popen'd it directly, with no
        scrub call anywhere in the module."""
        _git_init(tmp_path)
        (tmp_path / "provider_spawns").mkdir()
        (tmp_path / "provider_spawns" / "bad.py").write_text(
            "import os\n"
            "import subprocess\n"
            "env = {**os.environ, **(extra_env or {})}\n"
            "proc = subprocess.Popen(cmd, env=env)\n",
            encoding="utf-8",
        )
        _git_add(tmp_path, "provider_spawns/bad.py")

        sites = _find_popen_env_sites(tmp_path, tmp_path / "provider_spawns")
        problems = [
            (rel, lineno)
            for rel, calls in sites.items()
            for lineno, is_safe in calls
            if not is_safe
        ]
        assert problems == [("provider_spawns/bad.py", 4)]

    def test_a_direct_unscrubbed_env_kwarg_turns_the_assertion_red(self, tmp_path):
        _git_init(tmp_path)
        (tmp_path / "provider_spawns").mkdir()
        (tmp_path / "provider_spawns" / "bad.py").write_text(
            "import os\n"
            "import subprocess\n"
            "proc = subprocess.Popen(cmd, env={**os.environ})\n",
            encoding="utf-8",
        )
        _git_add(tmp_path, "provider_spawns/bad.py")

        sites = _find_popen_env_sites(tmp_path, tmp_path / "provider_spawns")
        problems = [
            (rel, lineno)
            for rel, calls in sites.items()
            for lineno, is_safe in calls
            if not is_safe
        ]
        assert problems == [("provider_spawns/bad.py", 3)]

    def test_a_popen_call_with_no_env_kwarg_is_ignored(self, tmp_path):
        """kimi_spawn.py's internal `git status`/`git diff` calls carry no env=
        kwarg at all — inheriting the ambient env implicitly for a trusted internal
        tool is a different concern than the worker-CLI defect this guard owns."""
        _git_init(tmp_path)
        (tmp_path / "provider_spawns").mkdir()
        (tmp_path / "provider_spawns" / "unrelated.py").write_text(
            "import subprocess\n"
            "proc = subprocess.run(['git', 'status'], capture_output=True)\n",
            encoding="utf-8",
        )
        _git_add(tmp_path, "provider_spawns/unrelated.py")

        sites = _find_popen_env_sites(tmp_path, tmp_path / "provider_spawns")
        assert sites == {}

    def test_a_non_subprocess_popen_call_is_ignored(self, tmp_path):
        """A same-named .Popen()/.run() method on an unrelated object must not be
        flagged — this guard owns exactly `subprocess.Popen`/`subprocess.run`."""
        _git_init(tmp_path)
        (tmp_path / "provider_spawns").mkdir()
        (tmp_path / "provider_spawns" / "unrelated.py").write_text(
            "class FakeRunner:\n"
            "    def run(self, cmd, env=None):\n"
            "        pass\n"
            "FakeRunner().run(cmd, env={'UNSAFE': '1'})\n",
            encoding="utf-8",
        )
        _git_add(tmp_path, "provider_spawns/unrelated.py")

        sites = _find_popen_env_sites(tmp_path, tmp_path / "provider_spawns")
        assert sites == {}

    def test_a_docstring_mention_does_not_trip_it(self, tmp_path):
        _git_init(tmp_path)
        (tmp_path / "provider_spawns").mkdir()
        (tmp_path / "provider_spawns" / "mentions_only.py").write_text(
            '"""Eventually calls subprocess.Popen(cmd, env=scrub_env(...))."""\n',
            encoding="utf-8",
        )
        _git_add(tmp_path, "provider_spawns/mentions_only.py")

        sites = _find_popen_env_sites(tmp_path, tmp_path / "provider_spawns")
        assert sites == {}

    def test_an_untracked_file_does_not_trip_it(self, tmp_path):
        """A fourth-lane call site that exists on disk but was never `git add`-ed (a
        build artifact, a nested worktree, a scratch file) is invisible to the scan —
        same property the beacon/phantom-guard/claude-scrub guards all rely on
        (OI-1597)."""
        _git_init(tmp_path)
        (tmp_path / "provider_spawns").mkdir()
        (tmp_path / "provider_spawns" / "existing.py").write_text(_GOOD_DIRECT_CALL_SITE, encoding="utf-8")
        _git_add(tmp_path, "provider_spawns/existing.py")
        baseline_files = set(_find_popen_env_sites(tmp_path, tmp_path / "provider_spawns"))

        (tmp_path / "provider_spawns" / "untracked_bad.py").write_text(
            "import os\n"
            "import subprocess\n"
            "proc = subprocess.Popen(cmd, env={**os.environ})\n",
            encoding="utf-8",
        )
        # Deliberately never git add-ed.
        matched_files = set(_find_popen_env_sites(tmp_path, tmp_path / "provider_spawns"))
        assert matched_files == baseline_files

    def test_an_unparseable_file_gives_a_clean_assertion_not_a_traceback(self, tmp_path):
        _git_init(tmp_path)
        (tmp_path / "provider_spawns").mkdir()
        (tmp_path / "provider_spawns" / "broken.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
        _git_add(tmp_path, "provider_spawns/broken.py")

        with pytest.raises(AssertionError, match="broken.py"):
            _find_popen_env_sites(tmp_path, tmp_path / "provider_spawns")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
