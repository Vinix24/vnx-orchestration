"""tests/test_spawn_scrub_env_keys_contract.py — a call-site guard for the
``scrub_env_keys`` parameter on ``spawn_claude()`` / ``SubprocessAdapter.deliver()``,
mirroring tests/test_phantom_guard_context_contract.py's AST-scan pattern (PR #1748,
OI-1614) rather than inventing a new one.

Defect this closes: ``scrub_env_keys`` was accepted by both ``spawn_claude()``
(provider_spawns/claude_spawn.py) and the lower-level ``SubprocessAdapter.deliver()``
(subprocess_adapter.py) with a default of ``None`` (no scrubbing). Only the DeepSeek-
and GLM-harness lanes ever passed it (their own account-safety-scoped
``_HARNESS_SCRUB_KEYS``). The two DEFAULT production claude lanes — headless
(envelope_adapters_claude.py, the default for `claude`/`claude_headless`) and
terminal-pinned subprocess (subprocess_dispatch_internals/delivery.py,
``VNX_ADAPTER_T{n}=subprocess``) — plus the benchmark path (provider_dispatch.py) and
the direct-``deliver()`` stream_events() path (adapters/claude_adapter.py) all silently
forwarded the FULL ambient environment to the worker subprocess. Measured 2026-09-03:
``VNX_SMTP_PASS`` (present in-process, 19 chars) would cross that boundary ungated.

The fix (provider_spawns/claude_spawn.py) makes ``scrub_env_keys`` a required
keyword-only parameter on ``spawn_claude()`` — no default, so a caller that forgets it
gets a ``TypeError`` at call time, not a silently-unscrubbed subprocess. That protects
every ``spawn_claude()`` caller, but ``SubprocessAdapter.deliver()`` itself stays
optional (many existing tests call it directly for reasons unrelated to env-scrubbing,
and it is a lower-level primitive — same two-tier shape as ``phantom_guard()`` staying
optional while ``record_phantom_if_any()``'s ``context`` became mandatory). A caller
that reaches ``SubprocessAdapter.deliver()`` directly, bypassing ``spawn_claude()``
entirely (adapters/claude_adapter.py's ``stream_events()``), is NOT covered by that
runtime TypeError. This test is the STATIC backstop for both shapes: an AST scan over
every git-tracked, non-test call site of ``spawn_claude(...)`` OR
``<SubprocessAdapter-instance>.deliver(...)``, checking that ``scrub_env_keys=`` is
present and not the literal ``None`` — so a new lane that drops it is caught in CI
before it ever spawns a subprocess, the same "catch the lane that doesn't exist yet"
property the phantom-guard test's call-site guard has.

Fixture-tree test classes prove the mechanism can actually fail, mirroring
TestCallSiteGuardCanActuallyFail in the phantom-guard test: a missing keyword, an
explicit ``scrub_env_keys=None``, and a receiver that is provably NOT a
SubprocessAdapter instance (so a same-named ``.deliver()`` method on an unrelated class
— TmuxAdapter, RuntimeFacade, HeadlessTransportAdapter, LocalSessionAdapter all define
one — is correctly ignored rather than flagged as a false positive).
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "scripts" / "lib"
sys.path.insert(0, str(LIB))

_SPAWN_CLAUDE_TARGET = "spawn_claude"
_DELIVER_TARGET = "deliver"
_ADAPTER_CTOR = "SubprocessAdapter"
_SCRUB_KWARG = "scrub_env_keys"
_EXCLUDE_DIRS = frozenset({"tests"})


def _git_tracked_py_files(root: Path) -> list[Path]:
    """Absolute paths to every git-tracked ``.py`` file under ``root``.

    Tracked-ness, not a directory namelist, keeps a build artifact / nested worktree /
    CI rollout copy out of the scan regardless of what it's named or where it lives.
    Fails CLOSED — raises ``RuntimeError`` — when git is unavailable, rather than
    silently falling back to an incomplete namelist (OI-1597 precedent).
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", "*.py"],
            cwd=str(root), capture_output=True, timeout=10,
        )
    except OSError as exc:
        raise RuntimeError(
            f"git is not available to determine tracked .py files under {root}"
        ) from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"`git ls-files` failed under {root} (not a git work tree?): {stderr}")
    stdout = result.stdout.decode("utf-8")
    return [root / rel for rel in stdout.split("\0") if rel]


def _call_target_name(node: ast.Call) -> Optional[str]:
    """The bare callee name of an ast.Call — 'foo' for both foo(...) and mod.foo(...)."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _subprocess_adapter_var_names(tree: ast.Module) -> set[str]:
    """Names of local variables assigned directly from a ``SubprocessAdapter(...)``
    call anywhere in the module (e.g. ``adapter = SubprocessAdapter()``,
    ``adapter = _sd.SubprocessAdapter()``). Deliberately module-wide rather than
    scope-precise — same "good enough, no false negatives" tradeoff as the phantom
    guard's flat AST walk; a variable name reused for something else in an unrelated
    function is a theoretical false positive this repo does not currently have.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        if _call_target_name(node.value) != _ADAPTER_CTOR:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _find_scrub_call_sites(
    root: Path, exclude_dirs: frozenset[str] = _EXCLUDE_DIRS,
) -> dict[str, list[tuple[str, ast.Call]]]:
    """Map of relative-path -> list of (category, ast.Call) for every real call to
    ``spawn_claude(...)`` or ``<SubprocessAdapter instance>.deliver(...)``, over the
    git-tracked .py files rooted at ``root``. A ``.deliver(...)`` call on a receiver
    that is NOT provably a SubprocessAdapter instance (TmuxAdapter, RuntimeFacade,
    HeadlessTransportAdapter, LocalSessionAdapter, a Mock in a test) is not a call
    site — this guard only owns the one ``deliver()`` implementation that actually
    builds a Popen env from the ambient environment.

    An unparseable or non-UTF-8 file raises a clean AssertionError naming the file,
    instead of letting the raw SyntaxError/UnicodeDecodeError traceback escape.
    """
    found: dict[str, list[tuple[str, ast.Call]]] = {}
    for full_path in _git_tracked_py_files(root):
        rel = full_path.relative_to(root)
        if any(part in exclude_dirs for part in rel.parts[:-1]):
            continue
        try:
            source = full_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(full_path))
        except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
            raise AssertionError(
                f"cannot parse {rel} while scanning for scrub_env_keys call sites: {exc}"
            ) from exc

        adapter_vars = _subprocess_adapter_var_names(tree)
        sites: list[tuple[str, ast.Call]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_target_name(node)
            if name == _SPAWN_CLAUDE_TARGET:
                sites.append(("spawn_claude", node))
            elif name == _DELIVER_TARGET and isinstance(node.func, ast.Attribute):
                receiver = node.func.value
                is_adapter = (
                    (isinstance(receiver, ast.Name) and receiver.id in adapter_vars)
                    or (
                        isinstance(receiver, ast.Call)
                        and _call_target_name(receiver) == _ADAPTER_CTOR
                    )
                )
                if is_adapter:
                    sites.append(("SubprocessAdapter.deliver", node))
        if sites:
            found[str(rel)] = sites
    return found


def _scrub_env_keys_kwarg_present(call: ast.Call) -> bool:
    """True iff ``call`` carries a ``scrub_env_keys=`` keyword whose value is not the
    literal ``None`` — an omitted keyword and an explicit ``None`` are the same defect
    (silently-unscrubbed subprocess env), so both are rejected identically."""
    for kw in call.keywords:
        if kw.arg == _SCRUB_KWARG:
            value = kw.value
            return not (isinstance(value, ast.Constant) and value.value is None)
    return False


_KNOWN_CALL_SITES = frozenset({
    "scripts/lib/provider_spawns/glm_harness_spawn.py",
    "scripts/lib/provider_spawns/deepseek_harness_spawn.py",
    "scripts/lib/subprocess_dispatch_internals/delivery.py",
    "scripts/lib/envelope_adapters_claude.py",
    "scripts/lib/provider_dispatch.py",
    "scripts/lib/provider_spawns/claude_spawn.py",
    "scripts/lib/adapters/claude_adapter.py",
})


class TestEveryCallSiteSuppliesScrubEnvKeys:
    """The wachter: every real, git-tracked call site of spawn_claude(...) or
    SubprocessAdapter(...).deliver(...) must pass a non-None scrub_env_keys=. This
    does not enumerate a fixed baseline of files (unlike a growth-tripwire test) — a
    brand-new lane is picked up automatically by the git-tracked-.py scan and held to
    the same bar, without this test needing to be edited."""

    def test_known_call_sites_are_found(self):
        sites = _find_scrub_call_sites(REPO)
        missing = _KNOWN_CALL_SITES - set(sites)
        assert not missing, f"expected call site(s) not found by the scan: {missing}"

    def test_every_call_site_supplies_non_none_scrub_env_keys(self):
        sites = _find_scrub_call_sites(REPO)
        assert sites, "expected at least the seven known call sites"
        problems = []
        for rel, calls in sites.items():
            for category, call in calls:
                if not _scrub_env_keys_kwarg_present(call):
                    problems.append(f"{rel}:{call.lineno} ({category}) missing or None scrub_env_keys=")
        assert not problems, (
            "call site(s) spawn a claude subprocess without an explicit, non-None "
            "scrub_env_keys= — the worker would inherit the full ambient environment, "
            "secrets included:\n" + "\n".join(problems)
        )


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)


def _git_add(root: Path, *relative_paths: str) -> None:
    subprocess.run(["git", "add", "--", *relative_paths], cwd=str(root), check=True)


_GOOD_SPAWN_CLAUDE_SITE = (
    "from provider_spawns.claude_spawn import spawn_claude\n"
    "from env_scrub_patterns import DEFAULT_SCRUB_KEY_PATTERNS\n"
    "spawn_claude(\n"
    "    prompt=p, model=m, dispatch_id=d, terminal_id=t,\n"
    "    scrub_env_keys=DEFAULT_SCRUB_KEY_PATTERNS,\n"
    ")\n"
)

_GOOD_DELIVER_SITE_VAR = (
    "from subprocess_adapter import SubprocessAdapter\n"
    "adapter = SubprocessAdapter()\n"
    "adapter.deliver(t, d, scrub_env_keys=DEFAULT_SCRUB_KEY_PATTERNS)\n"
)

_GOOD_DELIVER_SITE_INLINE = (
    "from subprocess_adapter import SubprocessAdapter\n"
    "SubprocessAdapter().deliver(t, d, scrub_env_keys=DEFAULT_SCRUB_KEY_PATTERNS)\n"
)


class TestCallSiteGuardCanActuallyFail:
    """Een wachter die je niet hebt zien falen, bewaakt niets. ``_find_scrub_call_sites``
    and ``_scrub_env_keys_kwarg_present`` are exercised directly against isolated
    fixture trees (never the real repo) so this guard's MECHANISM is proven, not just
    today's seven call sites."""

    def test_a_compliant_spawn_claude_site_passes(self, tmp_path):
        _git_init(tmp_path)
        (tmp_path / "good.py").write_text(_GOOD_SPAWN_CLAUDE_SITE, encoding="utf-8")
        _git_add(tmp_path, "good.py")

        sites = _find_scrub_call_sites(tmp_path)
        for rel, calls in sites.items():
            for _category, call in calls:
                assert _scrub_env_keys_kwarg_present(call), f"{rel}:{call.lineno}"

    def test_a_compliant_deliver_site_passes_both_var_and_inline_receiver(self, tmp_path):
        _git_init(tmp_path)
        (tmp_path / "good_var.py").write_text(_GOOD_DELIVER_SITE_VAR, encoding="utf-8")
        (tmp_path / "good_inline.py").write_text(_GOOD_DELIVER_SITE_INLINE, encoding="utf-8")
        _git_add(tmp_path, "good_var.py", "good_inline.py")

        sites = _find_scrub_call_sites(tmp_path)
        assert set(sites) == {"good_var.py", "good_inline.py"}
        for rel, calls in sites.items():
            for _category, call in calls:
                assert _scrub_env_keys_kwarg_present(call), f"{rel}:{call.lineno}"

    def test_a_missing_scrub_env_keys_keyword_turns_the_assertion_red(self, tmp_path):
        """The literal historic defect: the default-lane call sites omitted the
        keyword entirely (envelope_adapters_claude.py, delivery.py, provider_dispatch.py,
        claude_adapter.py before this fix)."""
        _git_init(tmp_path)
        (tmp_path / "bad.py").write_text(
            "from provider_spawns.claude_spawn import spawn_claude\n"
            "spawn_claude(prompt=p, model=m, dispatch_id=d, terminal_id=t)\n",
            encoding="utf-8",
        )
        _git_add(tmp_path, "bad.py")

        sites = _find_scrub_call_sites(tmp_path)
        problems = [
            (rel, call.lineno)
            for rel, calls in sites.items()
            for _category, call in calls
            if not _scrub_env_keys_kwarg_present(call)
        ]
        assert problems == [("bad.py", 2)]

    def test_an_explicit_none_turns_the_assertion_red(self, tmp_path):
        """Not just an omitted keyword — writing scrub_env_keys=None explicitly is the
        exact same defect (a caller that thought about it and got it wrong is no safer
        than one that never thought about it)."""
        _git_init(tmp_path)
        (tmp_path / "bad.py").write_text(
            "from subprocess_adapter import SubprocessAdapter\n"
            "adapter = SubprocessAdapter()\n"
            "adapter.deliver(t, d, scrub_env_keys=None)\n",
            encoding="utf-8",
        )
        _git_add(tmp_path, "bad.py")

        sites = _find_scrub_call_sites(tmp_path)
        problems = [
            (rel, call.lineno)
            for rel, calls in sites.items()
            for _category, call in calls
            if not _scrub_env_keys_kwarg_present(call)
        ]
        assert problems == [("bad.py", 3)]

    def test_a_deliver_call_on_a_non_subprocessadapter_receiver_is_ignored(self, tmp_path):
        """TmuxAdapter, RuntimeFacade, HeadlessTransportAdapter and LocalSessionAdapter
        all define their own .deliver() method with the same name. A call on any of
        them must NOT be flagged — this guard owns exactly one deliver() implementation,
        the one that actually builds a Popen env from the ambient environment."""
        _git_init(tmp_path)
        (tmp_path / "unrelated.py").write_text(
            "from tmux_adapter import TmuxAdapter\n"
            "adapter = TmuxAdapter(state_dir)\n"
            "adapter.deliver(t, d)\n",  # no scrub_env_keys — must not be flagged
            encoding="utf-8",
        )
        _git_add(tmp_path, "unrelated.py")

        sites = _find_scrub_call_sites(tmp_path)
        assert sites == {}

    def test_a_docstring_mention_does_not_trip_it(self, tmp_path):
        _git_init(tmp_path)
        (tmp_path / "mentions_only.py").write_text(
            '"""Eventually calls spawn_claude(scrub_env_keys=...)."""\n'
            "# see SubprocessAdapter.deliver for details\n",
            encoding="utf-8",
        )
        _git_add(tmp_path, "mentions_only.py")

        sites = _find_scrub_call_sites(tmp_path)
        assert sites == {}

    def test_an_untracked_file_does_not_trip_it(self, tmp_path):
        """A fourth-lane call site that exists on disk but was never `git add`-ed (a
        build artifact, a nested worktree, a scratch file) is invisible to the scan —
        same property the beacon/phantom-guard tests' guards rely on (OI-1597)."""
        _git_init(tmp_path)
        (tmp_path / "existing.py").write_text(_GOOD_SPAWN_CLAUDE_SITE, encoding="utf-8")
        _git_add(tmp_path, "existing.py")
        baseline_files = set(_find_scrub_call_sites(tmp_path))

        (tmp_path / "untracked_bad.py").write_text(
            "from provider_spawns.claude_spawn import spawn_claude\n"
            "spawn_claude(prompt=p, model=m, dispatch_id=d, terminal_id=t)\n",
            encoding="utf-8",
        )
        # Deliberately never git add-ed.
        matched_files = set(_find_scrub_call_sites(tmp_path))
        assert matched_files == baseline_files

    def test_an_unparseable_file_gives_a_clean_assertion_not_a_traceback(self, tmp_path):
        _git_init(tmp_path)
        (tmp_path / "broken.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
        _git_add(tmp_path, "broken.py")

        with pytest.raises(AssertionError, match="broken.py"):
            _find_scrub_call_sites(tmp_path)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
