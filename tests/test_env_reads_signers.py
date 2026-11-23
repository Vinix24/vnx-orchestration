"""config-een-oppervlak PR-8s: VNX_ALLOWED_SIGNERS and VNX_ATTEST_OVERRIDE_BUDGET are deleted (ADR-087).

The signer files are the only source of the signer list and DEFAULT_OVERRIDE_BUDGET is the only
budget. Part 1 scans the shipped code for any read of the two names; part 2 proves, with the
variables set to hostile values, that neither steers behaviour any more.
"""
from __future__ import annotations

import ast
import importlib
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(_ROOT / "scripts" / "lib"), str(_ROOT / "scripts")]

attest_override = importlib.import_module("attest_override")
delegation_mandate = importlib.import_module("delegation_mandate")
sign_manifest = importlib.import_module("attestation").sign_manifest
verify_attestation = importlib.import_module("attestation").verify_attestation
append_chained_entry = importlib.import_module("ndjson_hash_chain").append_chained_entry

DELETED = ("VNX_ALLOWED_SIGNERS", "VNX_ATTEST_OVERRIDE_BUDGET")
_READ_CALLS = {"get", "getenv", "setdefault", "pop"}


def _is_name(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Constant) and node.value == name


def _python_reads(tree: ast.AST, name: str) -> int:
    """Count env reads of *name*: get/getenv/setdefault/pop calls, ``environ[...]`` and ``in environ``."""
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args and _is_name(node.args[0], name):
            func = node.func
            attr = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            count += attr in _READ_CALLS
        elif isinstance(node, ast.Subscript) and _is_name(node.slice, name):
            count += 1
        elif isinstance(node, ast.Compare) and _is_name(node.left, name):
            count += any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops)
    return count


def _read_sites() -> dict[str, int]:
    counts = dict.fromkeys(DELETED, 0)
    for top in ("scripts", "vnx_cli", "dashboard"):
        for path in sorted((_ROOT / top).rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            if not any(n in source for n in DELETED):
                continue
            tree = ast.parse(source, filename=str(path))  # a parse error fails the test: never skipped
            for name in DELETED:
                counts[name] += _python_reads(tree, name)
        for path in sorted((_ROOT / top).rglob("*.sh")):
            text = path.read_text(encoding="utf-8")
            for name in DELETED:
                counts[name] += len(re.findall(rf"\$\{{?{name}\b", text))
    return counts


def test_no_shipped_code_reads_a_deleted_variable():
    assert _read_sites() == dict.fromkeys(DELETED, 0)


def test_scanner_counts_every_read_form():
    src = "\n".join([
        'import os', 'from os import environ, getenv',
        'os.environ.get("VNX_ALLOWED_SIGNERS")', 'os.environ["VNX_ALLOWED_SIGNERS"]',
        'os.getenv("VNX_ALLOWED_SIGNERS")', 'os.environ.setdefault("VNX_ALLOWED_SIGNERS", "x")',
        'os.environ.pop("VNX_ALLOWED_SIGNERS", None)', '"VNX_ALLOWED_SIGNERS" in os.environ',
        'environ.get("VNX_ALLOWED_SIGNERS")', 'getenv("VNX_ALLOWED_SIGNERS")',
        'print("VNX_ALLOWED_SIGNERS")',
    ])
    assert _python_reads(ast.parse(src), "VNX_ALLOWED_SIGNERS") == 8


# --- the signer list comes only from the files ------------------------------------------------

needs_ssh_keygen = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen not installed")


def _key(tmp_path: Path, name: str, identity: str) -> dict:
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / name)], check=True)
    line = f'{identity} namespaces="vnx-attestation" {(tmp_path / f"{name}.pub").read_text()}'
    return {"key": tmp_path / name, "identity": identity, "line": line}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A project root (no git, so no base branch) with a signer file holding one known key."""
    for name in DELETED:
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "proj"
    (root / ".vnx-attest").mkdir(parents=True)
    known = _key(tmp_path, "known", "known@example.invalid")
    (root / ".vnx-attest" / "allowed_signers").write_text(known["line"])
    return root, known, _key(tmp_path, "stranger", "stranger@example.invalid")


def _verifies(signers: Path, signer: dict) -> bool:
    manifest = {"attestation_type": "test", "content_key": "ck", "signer_identity": signer["identity"]}
    return verify_attestation(sign_manifest(manifest, signer["key"]), signers)


@needs_ssh_keygen
def test_signer_in_the_file_verifies_and_a_stranger_is_refused(repo):
    root, known, stranger = repo
    signers = delegation_mandate.resolve_allowed_signers_for_runtime(root)
    assert signers == root / ".vnx-attest" / "allowed_signers"
    assert _verifies(signers, known) is True
    assert _verifies(signers, stranger) is False


@needs_ssh_keygen
def test_set_allowed_signers_env_pointing_elsewhere_has_no_effect(repo, tmp_path, monkeypatch):
    root, known, stranger = repo
    planted = tmp_path / "planted_signers"
    planted.write_text(stranger["line"])
    monkeypatch.setenv("VNX_ALLOWED_SIGNERS", str(planted))
    signers = delegation_mandate.resolve_allowed_signers_for_runtime(root)
    assert signers == root / ".vnx-attest" / "allowed_signers"
    assert _verifies(signers, stranger) is False
    assert _verifies(signers, known) is True


def test_no_signer_file_resolves_to_none_even_with_the_env_set(tmp_path, monkeypatch):
    planted = tmp_path / "planted_signers"
    planted.write_text("x@example.invalid ssh-ed25519 AAAA\n")
    monkeypatch.setenv("VNX_ALLOWED_SIGNERS", str(planted))
    (tmp_path / "empty").mkdir()
    assert delegation_mandate.resolve_allowed_signers_for_runtime(tmp_path / "empty") is None


# --- the budget is the constant 5 -------------------------------------------------------------

@needs_ssh_keygen
def test_sixth_override_in_one_window_is_refused_with_the_env_set_to_50(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_ATTEST_OVERRIDE_BUDGET", "50")
    signer = _key(tmp_path, "op", "op@example.invalid")
    signers = tmp_path / "allowed_signers"
    signers.write_text(signer["line"])
    trail = tmp_path / attest_override.ATTEST_DIR / attest_override.OVERRIDE_TRAIL_FILE
    trail.parent.mkdir(parents=True)
    ts = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i in range(5):
        manifest = attest_override.build_override_manifest(
            content_key=f"ck{i}", reason=f"r{i}", dispatch_id=f"D-{i}",
            signer_identity=signer["identity"], timestamp=ts)
        append_chained_entry(trail, sign_manifest(manifest, signer["key"]))
    assert attest_override.get_override_budget() == attest_override.DEFAULT_OVERRIDE_BUDGET == 5
    assert attest_override.count_overrides_in_window(trail, allowed_signers=str(signers)) == 5
    with pytest.raises(RuntimeError, match=r"budget exhausted \(5/5 "):
        attest_override.write_override_record(
            content_key="ck-sixth", reason="sixth", dispatch_id="D-6", signer_identity=signer["identity"],
            timestamp=ts, key_path=signer["key"], repo_root=tmp_path, allowed_signers=signers)
    assert not (tmp_path / attest_override.ATTEST_DIR / "override-ck-sixth.json").exists()
