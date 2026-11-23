"""OI-2776: pyproject declares its license as an SPDX expression (PEP 639)."""
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_license_is_spdx_string():
    assert DATA["project"].get("license") == "MIT"


def test_license_files_listed_and_present():
    files = DATA["project"].get("license-files")
    assert isinstance(files, list) and "LICENSE" in files
    assert (ROOT / "LICENSE").is_file()


def test_no_license_classifier():
    bad = [c for c in DATA["project"].get("classifiers", []) if c.startswith("License ::")]
    assert bad == []


def test_setuptools_floor_at_least_77():
    reqs = DATA["build-system"]["requires"]
    specs = [r for r in reqs if re.match(r"\s*setuptools\b", r, re.I)]
    assert len(specs) == 1
    m = re.search(r">=\s*(\d+)", specs[0])
    assert m is not None, specs[0]
    assert int(m.group(1)) >= 77
