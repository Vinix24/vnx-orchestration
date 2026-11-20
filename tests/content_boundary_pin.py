"""Point the content-boundary default path at a test file.

``content_class.default_boundary_path`` has no environment override: a test pins
the file by replacing that function on the module every reader looks it up on.
"""

from __future__ import annotations

from pathlib import Path


def pin_boundary(monkeypatch, path) -> None:
    import content_class

    pinned = Path(path)
    monkeypatch.setattr(content_class, "default_boundary_path", lambda: pinned)
