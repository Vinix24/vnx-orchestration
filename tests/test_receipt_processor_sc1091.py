"""OI-676: receipt_processor.sh sources must carry shellcheck SC1091 directives.

shellcheck SC1091 ("Not following: ... openBinaryFile: does not exist") fires
when a ``source`` statement's target cannot be resolved. In
scripts/receipt_processor.sh every source line is a runtime path (``$SCRIPT_DIR``,
``$SCRIPTS_DIR``, ``$RP_LIB``), so shellcheck cannot resolve it statically.
From the repo root ``shellcheck -x scripts/receipt_processor.sh`` flagged
``source "$SCRIPT_DIR/lib/vnx_paths.sh"`` (line 6) with SC1091.

The fix is the ``source-path=SCRIPTDIR`` + ``source=<path>`` directive pair
immediately above each source line: ``source-path=SCRIPTDIR`` anchors the
``source=`` path to the script's own directory, so the directive resolves the
same way regardless of the shellcheck invocation CWD (repo root or scripts/).

Like test_receipt_processor_sc2181.py this is a static scan so it needs no
shellcheck binary in CI and fails deterministically on the pre-fix code.
"""

from __future__ import annotations

import re
from pathlib import Path

_RECEIPT_PROCESSOR_SH = Path(__file__).resolve().parent.parent / "scripts" / "receipt_processor.sh"

# A runtime-parameterised source statement, e.g.
#   source "$SCRIPT_DIR/lib/vnx_paths.sh"
#   source "$SCRIPTS_DIR/pane_manager.sh"
#   source "$RP_LIB/rp_logging.sh"
_SOURCE_LINE_RE = re.compile(r"^\s*source\s+[^#]*\.sh[\"']?\s*$")

# The shellcheck directive pair immediately above a source line:
#   # shellcheck source-path=SCRIPTDIR
#   # shellcheck source=lib/vnx_paths.sh
_SOURCE_PATH_RE = re.compile(r"^\s*#\s+shellcheck\s+source-path=SCRIPTDIR\s*$")
_SOURCE_DIRECTIVE_RE = re.compile(r"^\s*#\s+shellcheck\s+source=([^\s]+)\s*$")


def _sourced_basename(source_line: str) -> str:
    """Extract the basename of the file a source statement points at."""
    # Strip quotes and the leading variable-dir token: "$SCRIPT_DIR/lib/x.sh" -> "lib/x.sh"
    body = source_line.strip()
    body = re.sub(r'^source\s+["\']?\$?\{?[A-Z_]+}?["\']?/', "", body)
    body = body.strip('"\'')
    return body.rsplit("/", 1)[-1].strip("'\"")


def _directive_present(lines: list[str], lineno: int, pattern: re.Pattern) -> str | None:
    """Scan up to 3 lines above ``lineno`` (1-indexed) for a matching directive."""
    for back in (1, 2, 3):
        idx = lineno - 1 - back
        if idx < 0:
            break
        candidate = lines[idx]
        if not candidate.strip():
            continue
        m = pattern.search(candidate)
        if m:
            return candidate.strip()
    return None


def test_every_source_line_has_shellcheck_resolution_directives():
    text = _RECEIPT_PROCESSOR_SH.read_text(encoding="utf-8")
    lines = text.splitlines()

    bare_sources: list[str] = []
    unresolved_sources: list[str] = []

    for lineno, line in enumerate(lines, 1):
        if not _SOURCE_LINE_RE.search(line):
            continue
        sourced = _sourced_basename(line)

        source_path = _directive_present(lines, lineno, _SOURCE_PATH_RE)
        source_directive = _directive_present(lines, lineno, _SOURCE_DIRECTIVE_RE)

        if source_path is None or source_directive is None:
            bare_sources.append(f"  {lineno}: {line.strip()}")
            continue

        m = _SOURCE_DIRECTIVE_RE.search(source_directive)
        assert m is not None
        directive_target = m.group(1).rsplit("/", 1)[-1]
        if directive_target != sourced:
            unresolved_sources.append(
                f"  {lineno}: sources {sourced!r} but directive says {directive_target!r}"
            )

    assert not bare_sources, (
        "shellcheck SC1091 would fire on source lines without a "
        "# shellcheck source-path=SCRIPTDIR + source= pair:\n"
        + "\n".join(bare_sources)
    )
    assert not unresolved_sources, (
        "source= directive target does not match the sourced file:\n"
        + "\n".join(unresolved_sources)
    )


def test_vnx_paths_source_line_is_annotated():
    """The OI-676 flagged line must carry the directive pair.

    Guards against a future edit removing the annotation from the specific
    source line that was filed.
    """
    lines = _RECEIPT_PROCESSOR_SH.read_text(encoding="utf-8").splitlines()
    for lineno, line in enumerate(lines, 1):
        if "lib/vnx_paths.sh" in line and line.strip().startswith("source "):
            source_path = _directive_present(lines, lineno, _SOURCE_PATH_RE)
            source_directive = _directive_present(lines, lineno, _SOURCE_DIRECTIVE_RE)
            assert source_path is not None, f"line {lineno}: missing source-path=SCRIPTDIR directive"
            assert source_directive is not None, f"line {lineno}: missing source= directive"
            return
    raise AssertionError(f"no source statement for lib/vnx_paths.sh found in {_RECEIPT_PROCESSOR_SH}")
