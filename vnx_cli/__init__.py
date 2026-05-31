"""VNX CLI package.

``__version__`` is single-sourced from the root ``VERSION`` file. In an
installed wheel it comes back through package metadata (which setuptools
stamps from ``VERSION`` via ``[tool.setuptools.dynamic]``); in a dev checkout
the package is not installed, so we read ``VERSION`` directly. One source of
truth, no 0.9.0/rc3 drift.
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path


def _read_version() -> str:
    try:
        return _pkg_version("vnx-orchestration")
    except PackageNotFoundError:
        version_file = Path(__file__).resolve().parent.parent / "VERSION"
        try:
            text = version_file.read_text(encoding="utf-8").strip()
        except OSError:
            return "0.0.0+unknown"
        return text or "0.0.0+unknown"


__version__ = _read_version()
