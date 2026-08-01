"""VNX CLI package.

``__version__`` is single-sourced from the root ``VERSION`` file, which is
read first. In a real wheel install VERSION ships inside the package and
matches the metadata anyway; in the editable-install deployment (one install,
code swapped underneath via the ``current`` symlink) the pip metadata is
stamped once at install time and goes stale as the code moves, so it must
NOT win. Package metadata is the fallback for installs where VERSION is
genuinely absent, with ``0.0.0+unknown`` as the last resort.
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path


def _read_version() -> str:
    version_file = Path(__file__).resolve().parent.parent / "VERSION"
    try:
        text = version_file.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if text:
        return text
    try:
        return _pkg_version("vnx-orchestration")
    except PackageNotFoundError:
        return "0.0.0+unknown"


__version__ = _read_version()
