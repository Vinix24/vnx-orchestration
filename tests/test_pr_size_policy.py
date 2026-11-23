"""pr_size_policy (ADR-088, pr-grootte-afgedwongen PG-1): the measurement, the floor rule, unreadable refuses."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))
import config_registry as cr
import pr_size_policy as psp
import review_risk_class

_REAL_FETCH = review_risk_class.fetch_merge_refs  # for a local ``origin``; conftest stubs it per test (R3b)


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo, files):
    for name, body in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_bytes(body if isinstance(body, bytes) else body.encode())
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for key in psp.KEYS:
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(f"VNX_OVERRIDE_{cr._bare(key)}", raising=False)
    monkeypatch.setenv("VNX_GLOBAL_CONFIG_FILE", str(tmp_path / "no-global" / "config.json"))
    monkeypatch.setattr(cr, "_db_resolver", None)


def _rows(**rows):
    return lambda pid, key: rows.get(key)


_REFUSE = _rows(VNX_PR_SIZE_ENFORCE="refuse")  # the floor: a project_config row


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A clone of an ``origin`` whose PR 7 head is ``files`` on top of main; returns (clone, make)."""
    monkeypatch.setattr(review_risk_class, "fetch_merge_refs", _REAL_FETCH)
    origin, clone = tmp_path / "origin", tmp_path / "clone"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    base = _commit(origin, {"README": "base\n"})
    _git(tmp_path, "clone", "-q", str(origin), str(clone))

    def make(files):
        _git(origin, "checkout", "-q", "-B", "pr", base)
        head = _commit(origin, files)
        _git(origin, "update-ref", "refs/pull/7/head", head)
        _git(origin, "checkout", "-q", "main")
        return head
    return clone, make


def _v(lines=10, paths=("a.py",), resolver=None):
    return psp.verdict(7, "", "main", None, resolver=resolver, grant_reader=lambda *a: ("none", None, ""),
                       measurer=lambda *a: psp.Measurement(lines, tuple(paths), "origin/main", "0" * 40))


def test_measure_counts_added_and_deleted_and_a_binary_file_counts_zero(repo):
    clone, make = repo
    head = make({"src/a.py": "x\n" * 10, "tests/test_a.py": "y\n" * 5, "img.bin": b"\x00\x01\x02" * 50})
    m = psp.measure(7, head, "main", clone)
    assert (m.lines, set(m.paths)) == (15, {"src/a.py", "tests/test_a.py", "img.bin"})


def _case(step, origin, make, monkeypatch):
    head = make({"a.py": "z\n" * 3})
    if step in ("numstat", "name_only"):
        real, flag = review_risk_class._git, "--" + step.replace("_", "-")
        monkeypatch.setattr(review_risk_class, "_git", lambda a, cwd: (None, "boom") if flag in a else real(a, cwd))
    if step in ("merge_base", "empty_diff"):
        _git(origin, "checkout", "-q", "--orphan", "lone")
        head = _commit(origin, {"x": "x\n"}) if step == "merge_base" else _git(origin, "rev-parse", "main")
        _git(origin, "update-ref", "refs/pull/7/head", head)
    return {"base_branch": (7, head, None), "bad_name": (7, head, "bad..name"), "head": (7, "-x", "main"),
            "fetch": (99, head, "main")}.get(step, (7, head, "main"))


@pytest.mark.parametrize("step", ["base_branch", "bad_name", "head", "fetch", "merge_base", "numstat", "name_only",
                                  "empty_diff"])
def test_each_measurement_error_names_its_step_no_go_under_refuse_warn_under_warn(repo, monkeypatch, tmp_path, step):
    clone, make = repo
    pr, head, base = _case(step, tmp_path / "origin", make, monkeypatch)
    with pytest.raises(psp.PrSizeUnmeasured) as exc:
        psp.measure(pr, head, base, clone)
    assert exc.value.step == ("base_branch" if step == "bad_name" else step)
    for mode, outcome in (("refuse", "NO-GO"), ("warn", "WARN")):
        v = psp.verdict(pr, head, base, clone, resolver=_rows(VNX_PR_SIZE_ENFORCE=mode))
        assert (v.outcome, v.reason, v.mode, v.lines) == (outcome, "pr_size_unmeasured", mode, None)
        assert v.detail.startswith(exc.value.step + ":")


@pytest.mark.parametrize("var,value,source", [
    ("VNX_OVERRIDE_PR_SIZE_ENFORCE", "off", "override"), ("VNX_OVERRIDE_PR_SIZE_ENFORCE", "warn", "override"),
    ("VNX_OVERRIDE_PR_SIZE_HOLD", "99999", "override"), ("VNX_PR_SIZE_HOLD", "99999", "env"),
    ("VNX_OVERRIDE_PR_SIZE_HOLD_SECURITY", "301", "override")])
def test_a_looser_loose_layer_under_floor_refuse_is_no_go_naming_key_source_value_floor(monkeypatch, var, value, source):
    monkeypatch.setenv(var, value)
    v = _v(resolver=_REFUSE)
    assert (v.outcome, v.reason, v.source) == ("NO-GO", "pr_size_config_loosened", source)
    key = "VNX_" + var.split("VNX_")[-1].removeprefix("OVERRIDE_")
    assert v.detail.startswith(f"{key}={value!r} from {source} is looser than the floor")


def test_a_stricter_override_applies_and_under_floor_warn_a_loose_one_does(monkeypatch):
    monkeypatch.setenv("VNX_OVERRIDE_PR_SIZE_HOLD", "400")
    v = _v(420, resolver=_REFUSE)
    assert (v.outcome, v.reason, v.limit, v.limit_kind, v.source) == (
        "NO-GO", "pr_size_above_limit", 400, "hold", "override")
    monkeypatch.setenv("VNX_OVERRIDE_PR_SIZE_HOLD", "99999")
    v = _v(600)
    assert (v.outcome, v.limit, v.mode) == ("GO", 99999, "warn")
    monkeypatch.setenv("VNX_OVERRIDE_PR_SIZE_ENFORCE", "off")
    v = _v(600)
    assert (v.outcome, v.waived, v.lines) == ("GO", "pr_size", None)


@pytest.mark.parametrize("key", psp.KEYS)
def test_a_failing_project_store_on_any_key_is_unreadable(key):
    v = _v(1, resolver=lambda pid, k: {}[k] if k == key else None)  # the store raises on this one key
    assert (v.outcome, v.reason, v.detail) == ("NO-GO", "pr_size_config_unreadable", f"{key}: project_config_error")


@pytest.mark.parametrize("value", ["many", "0", "-5", "", "²"])
def test_a_hold_that_is_no_positive_integer_is_unreadable(value):
    v = _v(1, resolver=_rows(VNX_PR_SIZE_HOLD=value))
    assert (v.outcome, v.reason) == ("NO-GO", "pr_size_config_unreadable")


def test_an_unknown_enforce_value_reads_as_refuse_with_an_error_naming_value_and_source(caplog):
    v = _v(500, resolver=_rows(VNX_PR_SIZE_ENFORCE="bogus"))
    assert (v.outcome, v.reason, v.mode) == ("NO-GO", "pr_size_above_limit", "refuse")
    assert "VNX_PR_SIZE_ENFORCE='bogus' from project_config" in v.errors[0] and v.errors[0] in caplog.text


def test_a_security_path_switches_the_limit_to_300_and_a_project_prefix_adds():
    paths = ("docs/x.md", "scripts/dispatcher.sh", "scripts/receipt_processor.py", "scripts/pre_merge_gate.py",
             "scripts/review_gate_manager.py")
    v = _v(350, paths, _REFUSE)
    assert (v.outcome, v.limit, v.limit_kind) == ("NO-GO", 300, "security")
    assert v.security_paths_hit == paths[1:4]
    v = _v(350, paths[:1], _rows(VNX_SECURITY_PATHS="docs/"))
    assert (v.outcome, v.limit_kind) == ("WARN", "security")
    v = _v(450, paths[:1])
    assert (v.outcome, v.limit, v.limit_kind, v.source) == ("GO", 450, "hold", "default")


def test_350_lines_in_the_inbox_cli_under_refuse_is_no_go_at_the_security_limit():
    v = _v(350, ("scripts/inbox_cli.py",), _REFUSE)
    assert (v.outcome, v.reason, v.limit, v.limit_kind) == ("NO-GO", "pr_size_above_limit", 300, "security")
    assert v.security_paths_hit == ("scripts/inbox_cli.py",)


_RULE_1 = ("scripts/hooks/pre_commit.sh", ".claude/hooks/x.py", "scripts/lib/permission_gate.py",
           "scripts/lib/sandbox_run.py", "scripts/lib/auth_check.py", "scripts/lib/secret_store.py",
           "scripts/lib/token_cache.py", "scripts/lib/credential_vault.py", "scripts/lib/provider_spawns/kimi.py",
           ".claude/settings.json", ".github/workflows/ci.yml")


@pytest.mark.parametrize("path", _RULE_1 + tuple(m.rstrip("/") + "/x" if m.endswith("/") else m + ".py"
                                                 for m in psp.GOVERNANCE_PATH_MARKERS + psp.PR_SIZE_SECURITY_EXTRA))
def test_every_prefix_of_rules_1_to_3_gives_the_security_limit(path):
    v = _v(10, (path,))
    assert (v.limit_kind, v.limit, v.security_paths_hit) == ("security", 300, (path,))


@pytest.mark.parametrize("path", ("scripts/lib/plain_module.py", "docs/scripts/pr_merge.py.md",
                                  "docs/schemas/x.md", "tests/scripts/inbox_cli.py"))
def test_a_plain_path_or_a_security_name_below_another_root_gives_hold(path):
    v = _v(10, (path,))
    assert (v.limit_kind, v.limit, v.security_paths_hit) == ("hold", 450, ())


def test_a_name_that_extends_a_prefix_matches_it():
    assert _v(10, ("scripts/lib/operator_queue_x.py",)).limit_kind == "security"
    assert _v(10, ("scripts/lib/attestation_ledger.py",)).limit_kind == "security"


@pytest.mark.parametrize("value", ("", "docs/", " , ", "scripts/lib/plain_"))
def test_the_project_key_only_adds_and_never_removes_rules_1_to_3(value):
    rows = _rows(VNX_SECURITY_PATHS=value) if value else None
    for path in ("scripts/pr_merge.py", "schemas/x.sql", "scripts/dispatcher.sh", "scripts/hooks/x.sh"):
        assert _v(10, (path,), rows).limit_kind == "security"
    added = _v(10, ("scripts/lib/plain_module.py",), rows).limit_kind
    assert added == ("security" if value == "scripts/lib/plain_" else "hold")


def test_security_paths_hit_keeps_the_first_three_in_measurement_order():
    paths = ("docs/a.md", "schemas/a.sql", "scripts/inbox_cli.py", "scripts/hooks/h.sh", "scripts/pr_merge.py")
    assert _v(10, paths).security_paths_hit == paths[1:4]
