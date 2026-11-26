"""OI-3090: spec discovery distinguishes absent evidence from an unreadable scan."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from blast_radius_sources import spec_scope
from review_risk_class import DiffReading, derive_risk_class, spec_flag
from review_vendor import dispatch_spec_paths, resolve_builder_vendor


@pytest.mark.parametrize("layout", ["absent", "empty", "no_matching_id", "no_spec"])
def test_no_spec_is_explicit_absence(tmp_path, layout):
    root = tmp_path / "dispatches"
    if layout != "absent":
        root.mkdir()
    if layout == "no_matching_id":
        (root / "active" / "other").mkdir(parents=True)
    if layout == "no_spec":
        (root / "active" / "wanted").mkdir(parents=True)
    paths, reason = dispatch_spec_paths("wanted", data_dir=tmp_path)
    assert paths == [] and "no dispatch-spec.json" in reason


def test_exact_names_sorted_paths_and_regular_files(tmp_path):
    expected = []
    for bucket in ("completed", "active"):
        for did in ("wanted[1]", "wanted1", "wanted[1]-other"):
            path = tmp_path / "dispatches" / bucket / did / "dispatch-spec.json"
            path.parent.mkdir(parents=True)
            path.write_text("{}", encoding="utf-8")
            if did == "wanted[1]":
                expected.append(path)
    (tmp_path / "dispatches" / "README").write_text("buckets", encoding="utf-8")
    (tmp_path / "dispatches" / "pending").mkdir()
    (tmp_path / "dispatches" / "pending" / "wanted[1]").write_text("not a directory", encoding="utf-8")
    assert dispatch_spec_paths("wanted[1]", data_dir=tmp_path) == (sorted(expected), "")


@pytest.mark.parametrize("layout", ["root_file", "broken_bucket", "broken_dispatch"])
def test_scan_error_is_not_absence(tmp_path, layout):
    root = tmp_path / "dispatches"
    if layout == "root_file":
        root.write_text("not a directory", encoding="utf-8")
    else:
        parent = root if layout == "broken_bucket" else root / "active"
        parent.mkdir(parents=True)
        (parent / "wanted").symlink_to(tmp_path / "missing", target_is_directory=True)
    paths, reason = dispatch_spec_paths("wanted", data_dir=tmp_path)
    assert paths is None and "cannot scan dispatch specs" in reason
    assert ("NotADirectoryError" if layout == "root_file" else "FileNotFoundError") in reason


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read chmod(0) directories")
@pytest.mark.parametrize("level", ["dispatches", "bucket", "dispatch"])
def test_scan_errors_remain_visible_to_all_callers(tmp_path, level):
    path = tmp_path / "dispatches" / "active" / "wanted" / "dispatch-spec.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"provider": "codex", "irreversible": true}', encoding="utf-8")
    denied = {"dispatches": path.parents[2], "bucket": path.parents[1], "dispatch": path.parent}[level]
    mode = denied.stat().st_mode
    denied.chmod(0)
    try:
        paths, reason = dispatch_spec_paths("wanted", data_dir=tmp_path)
        builder = resolve_builder_vendor("wanted", data_dir=tmp_path)
        flag = spec_flag("wanted", data_dir=tmp_path)
        scope, scope_reason = spec_scope(tmp_path, "wanted")
    finally:
        denied.chmod(mode)
    assert paths is None and "PermissionError" in reason and str(denied) in reason
    assert builder.vendor is None and builder.reason == reason
    assert flag.irreversible is None and flag.detail == reason
    risk = derive_risk_class(DiffReading(("docs/change.md",)), spec=flag)
    assert risk.risk_class == "high" and reason in risk.reason
    assert scope is None and scope_reason == f"unknown: {reason}"
