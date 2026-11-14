"""OI-2830: the wheel-install smoke preflight checks installed distributions.

``python -m build --no-isolation`` checks the ``[build-system] requires`` list
against installed distributions. setuptools vendors ``wheel`` on ``sys.path``,
so ``import wheel`` can succeed while no ``wheel`` distribution is installed.
"""

import importlib.metadata
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_wheel_install as smoke  # noqa: E402  # vnx-ci-lint: sys.path shim for sibling test module

REAL_DISTRIBUTION = importlib.metadata.distribution


def _patch_missing(monkeypatch, missing):
    def fake(name):
        if name in missing:
            raise importlib.metadata.PackageNotFoundError(name)
        return object()

    monkeypatch.setattr(importlib.metadata, "distribution", fake)


def test_all_three_distributions_present(monkeypatch):
    _patch_missing(monkeypatch, set())
    assert smoke._build_toolchain_available() is True


@pytest.mark.parametrize("missing", ["build", "setuptools", "wheel"])
def test_one_missing_distribution_is_unavailable(monkeypatch, missing):
    _patch_missing(monkeypatch, {missing})
    assert smoke._build_toolchain_available() is False


def test_module_import_does_not_count_as_distribution(monkeypatch):
    # `wheel` importable (vendored) but no distribution: must stay unavailable.
    _patch_missing(monkeypatch, {"wheel"})
    assert smoke._build_toolchain_available() is False


def test_shell_preflight_exits_2_without_wheel_distribution(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    venv_dir = tmp_path / "venv"
    try:
        venv.EnvBuilder(with_pip=False).create(venv_dir)
    except Exception as exc:  # vnx-silent-except: venv creation unsupported here, skip with reason
        pytest.skip(f"venv creation not possible: {exc}")
    py = venv_dir / "bin" / "python"
    if not py.is_file():
        pytest.skip("venv has no bin/python (non-POSIX layout)")
    version_ok = subprocess.run(
        [str(py), "-c", "import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) else 1)"]
    ).returncode
    if version_ok != 0:
        pytest.skip("interpreter outside requires-python; preflight would exit 2 for another reason")
    # Empty venv: no build/setuptools/wheel distributions at all.
    result = subprocess.run(
        ["bash", str(smoke.SMOKE_SCRIPT)],
        env={"PATH": "/usr/bin:/bin", "SMOKE_PYTHON": str(py)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "build toolchain missing" in result.stderr


def test_shell_preflight_exits_2_with_vendored_wheel_only(tmp_path):
    """Venv with setuptools+build metadata but vendored wheel only: still exit 2."""
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    if not (3, 11) <= sys.version_info[:2] < (3, 14):
        pytest.skip("running interpreter outside requires-python")
    venv_dir = tmp_path / "venv"
    try:
        venv.EnvBuilder(with_pip=False).create(venv_dir)
    except Exception as exc:  # vnx-silent-except: venv creation unsupported here, skip with reason
        pytest.skip(f"venv creation not possible: {exc}")
    py = venv_dir / "bin" / "python"
    site = next((venv_dir / "lib").glob("python*/site-packages"), None)
    if site is None or not py.is_file():
        pytest.skip("unexpected venv layout")
    # Fake dist-info for build and setuptools only; an importable `wheel` module
    # exists but has no dist-info, mirroring the vendored-wheel situation.
    for name in ("build", "setuptools"):
        info = site / f"{name}-1.0.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n")
    (site / "wheel.py").write_text("")
    result = subprocess.run(
        ["bash", str(smoke.SMOKE_SCRIPT)],
        env={"PATH": "/usr/bin:/bin", "SMOKE_PYTHON": str(py)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "build toolchain missing" in result.stderr
