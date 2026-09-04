"""Tests for the PATH/interpreter parity check (scripts/lib/path_parity.py).

RED against origin/main (module does not exist), GREEN on this branch.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import path_parity  # noqa: E402


def _probe(executable: str, version: str, ok: bool = True, error=None) -> dict:
    return {"ok": ok, "executable": executable, "version": version, "prefix": "/x", "error": error}


def test_parity_holds_on_same_version() -> None:
    fg = _probe("/opt/homebrew/bin/python3", "3.12.4")
    bg = _probe("/usr/bin/python3", "3.12.1")
    result = path_parity.compare_parity(fg, bg)
    assert result["parity"] is True
    # A different executable PATH on the same major.minor is informational
    # (macOS jobs legitimately resolve another binary), never a failure.
    assert result["mismatches"] == []
    assert result["info"][0]["kind"] == "executable_differs"


def test_parity_breaks_on_version_mismatch() -> None:
    fg = _probe("/opt/homebrew/bin/python3", "3.12.4")
    bg = _probe("/usr/bin/python3", "3.9.6")
    result = path_parity.compare_parity(fg, bg)
    assert result["parity"] is False
    assert result["mismatches"][0]["kind"] == "version_mismatch"


def test_parity_breaks_when_background_interpreter_broken() -> None:
    """The OI-852 case: foreground healthy, PATH-resolved background python3 dead."""
    fg = _probe("/opt/homebrew/bin/python3", "3.12.4")
    bg = _probe(None, None, ok=False, error="python3: command not found")
    result = path_parity.compare_parity(fg, bg)
    assert result["parity"] is False
    assert result["mismatches"][0]["kind"] == "background_interpreter_broken"


def test_parity_breaks_when_foreground_interpreter_broken() -> None:
    fg = _probe(None, None, ok=False, error="bad interpreter")
    bg = _probe("/usr/bin/python3", "3.12.1")
    result = path_parity.compare_parity(fg, bg)
    assert result["parity"] is False
    assert result["mismatches"][0]["kind"] == "foreground_interpreter_broken"


class _FakeProc:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_probe_interpreter_parses_real_shape() -> None:
    payload = json.dumps({"executable": "/usr/bin/python3", "version": "3.9.6", "prefix": "/usr"})

    def runner(cmd, capture_output=False, text=False, check=False, env=None, timeout=None):
        return _FakeProc(stdout=payload + "\n")

    probe = path_parity.probe_interpreter(runner=runner, env={"PATH": "/usr/bin:/bin"})
    assert probe["ok"] is True
    assert probe["executable"] == "/usr/bin/python3"
    assert probe["version"] == "3.9.6"


def test_probe_interpreter_never_raises_on_broken_python() -> None:
    def runner(cmd, capture_output=False, text=False, check=False, env=None, timeout=None):
        raise OSError("python3 not found")

    probe = path_parity.probe_interpreter(runner=runner)
    assert probe["ok"] is False
    assert "not found" in probe["error"]


def test_probe_interpreter_flags_nonzero_exit() -> None:
    def runner(cmd, capture_output=False, text=False, check=False, env=None, timeout=None):
        return _FakeProc(stdout="", returncode=1, stderr="dyld: Library not loaded")

    probe = path_parity.probe_interpreter(runner=runner)
    assert probe["ok"] is False
    assert "dyld" in probe["error"]


def test_background_env_uses_launchd_default_path() -> None:
    env = path_parity.background_env()
    assert env["PATH"] == path_parity.BACKGROUND_PATH


# ─────────────────────────────────────────────────────────────────────────
# requires-python range parsing/checking
# ─────────────────────────────────────────────────────────────────────────

def test_parse_requires_python_reads_pyproject(tmp_path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\nrequires-python = ">=3.11,<3.14"\n')
    assert path_parity.parse_requires_python(pyproject) == ">=3.11,<3.14"


def test_parse_requires_python_missing_file_returns_none(tmp_path) -> None:
    assert path_parity.parse_requires_python(tmp_path / "nope.toml") is None


def test_version_in_range_within_bounds() -> None:
    assert path_parity.version_in_range("3.12", ">=3.11,<3.14") is True


def test_version_in_range_below_minimum() -> None:
    assert path_parity.version_in_range("3.9", ">=3.11,<3.14") is False


def test_version_in_range_at_or_above_exclusive_max() -> None:
    assert path_parity.version_in_range("3.14", ">=3.11,<3.14") is False


def test_version_in_range_unparseable_returns_none() -> None:
    assert path_parity.version_in_range("", ">=3.11,<3.14") is None
    assert path_parity.version_in_range("3.12", "") is None
    assert path_parity.version_in_range("3.12", None) is None


def test_version_from_path_infers_pinned_version() -> None:
    assert path_parity._version_from_path("/opt/homebrew/opt/python@3.12/bin/python3.12") == "3.12"
    assert path_parity._version_from_path("/opt/homebrew/opt/python@3.9/bin/python3") == "3.9"


def test_version_from_path_none_for_unversioned_path() -> None:
    assert path_parity._version_from_path("/repo/.venv/bin/python") is None


def test_resolve_interpreter_version_infers_from_path() -> None:
    assert path_parity.resolve_interpreter_version("/opt/homebrew/opt/python@3.12/bin/python3.12") == "3.12"


def test_resolve_interpreter_version_executes_and_caches_when_unversioned() -> None:
    calls = []

    def runner(cmd, capture_output=False, text=False, check=False, timeout=None):
        calls.append(cmd)
        return _FakeProc(stdout="3.11.9\n")

    cache: dict = {}
    v1 = path_parity.resolve_interpreter_version("/repo/.venv/bin/python", runner=runner, cache=cache)
    v2 = path_parity.resolve_interpreter_version("/repo/.venv/bin/python", runner=runner, cache=cache)
    assert v1 == "3.11"
    assert v2 == "3.11"
    assert len(calls) == 1  # cached, not re-executed for the same interpreter path


# ─────────────────────────────────────────────────────────────────────────
# Consumer discovery — launchd plists + crontab
# ─────────────────────────────────────────────────────────────────────────

_FAKE_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.vnx.fake</string>
<key>ProgramArguments</key><array>
<string>/opt/homebrew/opt/python@3.12/bin/python3.12</string>
<string>/repo/script.py</string>
</array></dict></plist>
"""


def test_discover_launchd_consumers_parses_plist(tmp_path) -> None:
    agents_dir = tmp_path / "LaunchAgents"
    agents_dir.mkdir()
    (agents_dir / "com.vnx.fake.plist").write_text(_FAKE_PLIST)
    (agents_dir / "com.other.ignored.plist").write_text(_FAKE_PLIST)

    consumers = path_parity.discover_launchd_consumers(agents_dir)
    assert len(consumers) == 1
    assert consumers[0]["label"] == "com.vnx.fake"
    assert consumers[0]["argv"][0] == "/opt/homebrew/opt/python@3.12/bin/python3.12"


def test_discover_launchd_consumers_missing_dir_returns_empty(tmp_path) -> None:
    assert path_parity.discover_launchd_consumers(tmp_path / "nope") == []


def test_discover_launchd_consumers_defaults_search_path_without_declared_path(tmp_path) -> None:
    """No ``EnvironmentVariables`` key at all (``_FAKE_PLIST`` above) must
    still fall back to the launchd/cron default -- the pre-existing
    behavior this OI-1621 fix must not regress."""
    agents_dir = tmp_path / "LaunchAgents"
    agents_dir.mkdir()
    (agents_dir / "com.vnx.fake.plist").write_text(_FAKE_PLIST)

    consumers = path_parity.discover_launchd_consumers(agents_dir)
    assert consumers[0]["search_path"] == path_parity.BACKGROUND_PATH


_FAKE_PLIST_WITH_DECLARED_PATH = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.vnx.fake-declared-path</string>
<key>ProgramArguments</key><array>
<string>/bin/bash</string>
<string>-c</string>
<string>cd /repo &amp;&amp; exec python3 script.py</string>
</array>
<key>EnvironmentVariables</key><dict>
<key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
</dict>
</dict></plist>
"""


def test_discover_launchd_consumers_honors_declared_environment_path(tmp_path) -> None:
    """OI-1621 regression: a launchd agent that declares its own
    ``EnvironmentVariables.PATH`` (exactly the shape of the real
    com.vnx.gate-obligation-runner/com.vnx.ledger-health plists) must have
    THAT path threaded through as ``search_path`` -- not the hardcoded
    ``BACKGROUND_PATH`` -- because that declared PATH is what launchd
    actually hands the job at runtime."""
    agents_dir = tmp_path / "LaunchAgents"
    agents_dir.mkdir()
    (agents_dir / "com.vnx.fake-declared-path.plist").write_text(_FAKE_PLIST_WITH_DECLARED_PATH)

    consumers = path_parity.discover_launchd_consumers(agents_dir)
    assert len(consumers) == 1
    assert consumers[0]["search_path"] == "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    assert consumers[0]["search_path"] != path_parity.BACKGROUND_PATH


def test_gate_obligation_runner_shaped_consumer_resolves_via_declared_path_not_background(
    tmp_path, monkeypatch
) -> None:
    """End-to-end OI-1621 reproduction: a bare-``python3`` launchd job
    declaring a homebrew-first PATH must be judged against THAT PATH's
    interpreter, not the Xcode-CLT stub ``BACKGROUND_PATH`` would resolve.

    Simulates two competing interpreters at two competing search paths (a
    modern one at the declared homebrew-first PATH, an old one at
    BACKGROUND_PATH) via a monkeypatched ``shutil.which`` -- deterministic,
    no dependency on what is actually installed on the machine running this
    test. Before the OI-1621 fix (search_path hardcoded to BACKGROUND_PATH
    in discover_launchd_consumers) this reproduces the false
    consumer_interpreter_out_of_range finding; after the fix it is clean."""
    agents_dir = tmp_path / "LaunchAgents"
    agents_dir.mkdir()
    (agents_dir / "com.vnx.fake-declared-path.plist").write_text(_FAKE_PLIST_WITH_DECLARED_PATH)

    def fake_which(name, path=None):
        assert name == "python3"
        if path == path_parity.BACKGROUND_PATH:
            return "/usr/bin/python3.9"  # the Xcode-CLT stub OI-852/OI-1621 warn about
        return "/opt/homebrew/opt/python@3.12/bin/python3.12"

    monkeypatch.setattr(path_parity.shutil, "which", fake_which)

    consumers = path_parity.discover_launchd_consumers(agents_dir)
    # "/repo" matches the literal text baked into _FAKE_PLIST_WITH_DECLARED_PATH's
    # inline -c script above -- resolve_consumer_interpreter's in-repo check is a
    # literal substring match, not path resolution.
    result = path_parity.scan_consumers(consumers, Path("/repo"), ">=3.11,<3.14")

    assert result["parity"] is True
    assert result["mismatches"] == []
    assert result["consumers"][0]["interpreter"] == "/opt/homebrew/opt/python@3.12/bin/python3.12"
    assert result["consumers"][0]["in_range"] is True


def test_discover_crontab_consumers_parses_schedule_and_declared_path() -> None:
    text = (
        "PATH=/opt/homebrew/bin:/usr/bin:/bin\n"
        "0 3 * * * /opt/homebrew/bin/python3 /repo/scripts/nightly.py\n"
        "# a comment line\n"
        "\n"
        "@daily python3 /repo/scripts/daily.py\n"
    )
    consumers = path_parity.discover_crontab_consumers(text)
    assert len(consumers) == 2
    assert consumers[0]["argv"] == ["/opt/homebrew/bin/python3", "/repo/scripts/nightly.py"]
    assert consumers[0]["search_path"] == "/opt/homebrew/bin:/usr/bin:/bin"
    assert consumers[1]["argv"] == ["python3", "/repo/scripts/daily.py"]


def test_discover_crontab_consumers_empty_text_returns_empty() -> None:
    assert path_parity.discover_crontab_consumers("") == []


# ─────────────────────────────────────────────────────────────────────────
# Static interpreter resolution — never executes the target script
# ─────────────────────────────────────────────────────────────────────────

def test_resolve_consumer_interpreter_direct_python_invocation(tmp_path) -> None:
    outcome = path_parity.resolve_consumer_interpreter(
        ["/opt/homebrew/opt/python@3.12/bin/python3.12", "/repo/script.py"], tmp_path
    )
    assert outcome["relevant"] is True
    assert outcome["interpreter"] == "/opt/homebrew/opt/python@3.12/bin/python3.12"


def test_resolve_consumer_interpreter_vnx_binary_is_irrelevant(tmp_path) -> None:
    outcome = path_parity.resolve_consumer_interpreter(["/opt/homebrew/bin/vnx", "dream", "run"], tmp_path)
    assert outcome["relevant"] is False
    assert "#1247" in outcome["reason"]


def test_resolve_consumer_interpreter_non_python_binary_is_irrelevant(tmp_path) -> None:
    outcome = path_parity.resolve_consumer_interpreter(["/usr/local/bin/node", "app.js"], tmp_path)
    assert outcome["relevant"] is False


def test_resolve_consumer_interpreter_finds_repo_venv_script(tmp_path) -> None:
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.write_text("#!/bin/sh\n")
    script = tmp_path / "scripts" / "nightly.sh"
    script.parent.mkdir(parents=True)
    script.write_text('PY="$(dirname "$0")/../.venv/bin/python"\n"$PY" -c pass\n')

    outcome = path_parity.resolve_consumer_interpreter(["/bin/bash", str(script)], tmp_path)
    assert outcome["relevant"] is True
    assert outcome["interpreter"] == str(venv_python)


def test_resolve_consumer_interpreter_script_outside_repo_is_irrelevant(tmp_path) -> None:
    other_root = tmp_path / "other-project"
    script = other_root / "scripts" / "job.sh"
    script.parent.mkdir(parents=True)
    script.write_text("exec python3 -m something\n")
    repo_root = tmp_path / "vnx-orchestration"
    repo_root.mkdir()

    outcome = path_parity.resolve_consumer_interpreter(["/bin/bash", str(script)], repo_root)
    assert outcome["relevant"] is False
    assert "outside" in outcome["reason"]


def test_resolve_consumer_interpreter_inline_script_outside_repo_is_irrelevant(tmp_path) -> None:
    outcome = path_parity.resolve_consumer_interpreter(
        ["/bin/bash", "-c", 'exec "$SOME_OTHER_REPO/.venv/bin/python3" job.py'], tmp_path
    )
    assert outcome["relevant"] is False


# ─────────────────────────────────────────────────────────────────────────
# Consumer scan — the counter-proof pair (DoD: must be able to fail, not
# just report green because nothing was found).
# ─────────────────────────────────────────────────────────────────────────

def test_consumer_scan_flags_real_out_of_range_consumer(tmp_path) -> None:
    """Negative control: a consumer pinned to a too-old interpreter MUST turn
    the scan red. Without this, a scan that finds zero consumers would also
    report parity=true, and a green result would be indistinguishable from a
    blind scan that checked nothing — the exact failure mode this item exists
    to prevent (a control that can never fail, fails silently)."""
    consumers = [
        {
            "label": "com.vnx.fake-stale-job",
            "argv": ["/opt/homebrew/opt/python@3.9/bin/python3.9", "/some/script.py"],
            "source": "/fake/com.vnx.fake-stale-job.plist",
            "search_path": path_parity.BACKGROUND_PATH,
        }
    ]
    result = path_parity.scan_consumers(consumers, tmp_path, ">=3.11,<3.14")
    assert result["parity"] is False
    assert len(result["mismatches"]) == 1
    mismatch = result["mismatches"][0]
    assert mismatch["kind"] == "consumer_interpreter_out_of_range"
    assert mismatch["consumer"] == "com.vnx.fake-stale-job"
    assert mismatch["version"] == "3.9"
    assert mismatch["interpreter"] == "/opt/homebrew/opt/python@3.9/bin/python3.9"


def test_consumer_scan_stays_green_when_all_pinned_in_range(tmp_path) -> None:
    """Positive control paired with the negative one above: every consumer
    directly pins an in-range interpreter -> parity stays true, and every
    consumer entry actually got resolved+checked (not just absent)."""
    consumers = [
        {
            "label": "com.vnx.dashboard",
            "argv": ["/opt/homebrew/opt/python@3.12/bin/python3.12", "/some/serve.py"],
            "source": "/fake/com.vnx.dashboard.plist",
            "search_path": path_parity.BACKGROUND_PATH,
        },
        {
            "label": "com.vnx.provider-usage",
            "argv": ["/opt/homebrew/opt/python@3.12/bin/python3.12", "/some/collect.py"],
            "source": "/fake/com.vnx.provider-usage.plist",
            "search_path": path_parity.BACKGROUND_PATH,
        },
    ]
    result = path_parity.scan_consumers(consumers, tmp_path, ">=3.11,<3.14")
    assert result["parity"] is True
    assert result["mismatches"] == []
    assert len(result["consumers"]) == 2
    assert all(c["in_range"] for c in result["consumers"])


def test_check_parity_raw_probe_is_informational_only(tmp_path, monkeypatch) -> None:
    """Integration shape check: a raw foreground/background version mismatch
    must land under raw_probe (level=info) and must NOT flip top-level
    parity — only a real consumer out of range may do that (defect B)."""
    (tmp_path / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.11,<3.14"\n')
    monkeypatch.setattr(path_parity, "DEFAULT_LAUNCHAGENTS_DIR", tmp_path / "no-such-dir")
    monkeypatch.setattr(path_parity, "read_crontab", lambda runner=None: "")

    def runner(cmd, capture_output=False, text=False, check=False, env=None, timeout=None):
        version = "3.9.6" if (env or {}).get("PATH") == path_parity.BACKGROUND_PATH else "3.12.4"
        payload = json.dumps({"executable": "/usr/bin/python3", "version": version, "prefix": "/x"})
        return _FakeProc(stdout=payload + "\n")

    result = path_parity.check_parity(runner=runner, repo_root=tmp_path)
    assert result["raw_probe"]["level"] == "info"
    assert result["raw_probe"]["mismatches"][0]["kind"] == "version_mismatch"
    # No real consumers were discoverable in this sandboxed tmp_path -> nothing to flag.
    assert result["parity"] is True
    assert result["mismatches"] == []
    assert result["consumer_scan"]["requires_python"] == ">=3.11,<3.14"
