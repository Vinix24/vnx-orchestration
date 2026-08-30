#!/usr/bin/env python3
"""Generate/check the "Supervised Components" and "Hooks" sections of
``docs/core/00_VNX_ARCHITECTURE.md`` (D6).

The generated content is spliced between HTML-comment markers so the rest of
the (still hand-written) document is untouched. Generation logic lives in
``scripts/lib/architecture_components.py``; this script only reads the
committed file, renders the two sections, and either diffs (default -- CI
mode) or writes (``--write``) the result.

Usage:
  python3 scripts/generate_architecture_doc.py            # check, exit 1 on drift
  python3 scripts/generate_architecture_doc.py --write     # regenerate in place

Wired via ``make architecture-doc-check`` and
``.github/workflows/architecture-doc-drift.yml``.

Exit codes: 0 = in sync (or written), 1 = drift detected, 2 = internal error.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
for sub in (_REPO_ROOT / "scripts", _REPO_ROOT / "scripts" / "lib"):
    s = str(sub)
    if s not in sys.path:
        sys.path.insert(0, s)

DOC_PATH = _REPO_ROOT / "docs" / "core" / "00_VNX_ARCHITECTURE.md"

_MARKERS = ("supervised-components", "hooks")


def _marker_re(name: str) -> "re.Pattern[str]":
    begin = re.escape(f"<!-- BEGIN GENERATED: {name} -->")
    end = re.escape(f"<!-- END GENERATED: {name} -->")
    return re.compile(f"({begin})(.*?)({end})", re.DOTALL)


def splice_block(text: str, name: str, new_body: str) -> str:
    """Replace the content between a marker pair with *new_body*.

    Raises ``ValueError`` if the marker pair is not found -- a missing
    marker means the doc was hand-edited out from under the generator,
    which must fail loudly rather than silently skip the section.
    """
    pattern = _marker_re(name)
    if not pattern.search(text):
        raise ValueError(
            f"marker pair 'BEGIN/END GENERATED: {name}' not found in {DOC_PATH} "
            "-- the doc was edited out from under the generator."
        )
    return pattern.sub(lambda m: f"{m.group(1)}\n{new_body}\n{m.group(3)}", text, count=1)


def render(committed_text: str) -> str:
    import architecture_components as ac

    daemon_rows = ac.build_daemon_rows()
    hook_rows = ac.build_hook_rows()

    text = splice_block(committed_text, "supervised-components", ac.render_daemon_md(daemon_rows))
    text = splice_block(text, "hooks", ac.render_hooks_md(hook_rows))
    return text


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    write = "--write" in args

    try:
        committed_text = DOC_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"internal error: cannot read {DOC_PATH}: {exc}", file=sys.stderr)
        return 2

    try:
        generated_text = render(committed_text)
    except (ValueError, ImportError, OSError) as exc:
        print(f"internal error: {exc}", file=sys.stderr)
        return 2

    if write:
        DOC_PATH.write_text(generated_text, encoding="utf-8")
        print(f"wrote {DOC_PATH}")
        return 0

    if generated_text == committed_text:
        print(f"OK — {DOC_PATH} generated sections match the live registry.")
        return 0

    print(
        f"DRIFT — {DOC_PATH}'s generated sections do not match the live "
        "registry. Regenerate with:\n"
        "  python3 scripts/generate_architecture_doc.py --write\n",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
