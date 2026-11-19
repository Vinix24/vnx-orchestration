"""Suggested edits go through a dispatch (ochtendronde D3c, ADR-095 decisions 10 and 13).

``accept`` computes the exact diff on a temp copy and stages one dispatch the T0 fires.
"""

from __future__ import annotations

import difflib
import hashlib
import shutil
import tempfile
from pathlib import Path
from typing import Callable, Optional, Tuple

from suggested_edit_targets import guard_target


def compute_diff(edit: dict, root: Path, handler: Callable[[dict], bool], set_root: Callable[[Path], None],
                 get_root: Callable[[], Path]) -> Tuple[Optional[str], Optional[str]]:
    """``(diff, None)`` or ``(None, reason)``. Runs ``handler`` on a copy of the target, never the real file."""
    target, reason = guard_target(edit.get("target"), root)
    if target is None:
        return None, f"target refused ({reason}): {edit.get('target')!r}"
    before = target.read_text(encoding="utf-8")
    saved = get_root()
    with tempfile.TemporaryDirectory(prefix="suggest-diff-") as tmp:
        copy = Path(tmp) / edit["target"]
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(target, copy)
        set_root(Path(tmp))
        try:
            changed = handler(edit)
        except Exception as exc:  # the reason goes back to the caller, the edit stays pending
            return None, f"handler raised {type(exc).__name__}: {exc}"
        finally:
            set_root(saved)
        if not changed:
            return None, "handler made no change"
        after = copy.read_text(encoding="utf-8")
    if after == before:
        return None, "handler made no change"
    rel = edit["target"]
    diff = "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True), f"a/{rel}", f"b/{rel}"))
    return diff, None


def dispatch_id_for(edit_id: object, diff: str) -> str:
    return f"edit-{edit_id}-{hashlib.sha256(diff.encode('utf-8')).hexdigest()[:8]}"


def instruction_for(edit: dict, diff: str) -> str:
    return (
        f"# Apply suggested edit #{edit.get('id')} to {edit['target']}\n\n"
        "Apply exactly this diff, change nothing else, open a PR.\n\n"
        f"```diff\n{diff}```\n"
    )


def stage_edit(edit: dict, diff: str, *, project_id: str, data_dir: Path, stage: Optional[Callable] = None) -> str:
    """Stage one bundle for the edit and return its dispatch id; an already staged bundle is reused."""
    dispatch_id = dispatch_id_for(edit.get("id"), diff)
    if (Path(data_dir) / "dispatches" / "pending" / dispatch_id / "dispatch-spec.json").is_file():
        return dispatch_id
    if stage is None:
        from dispatch_bridge import stage_spec_bundle as stage
    stage(
        instruction_text=instruction_for(edit, diff), dispatch_id=dispatch_id, role="backend-developer",
        target_slot="T1", project_id=project_id, provider="claude", model="sonnet",
        task_class="04_documentation", dispatch_paths=({"path": edit["target"], "access": "read_write"},),
        data_dir=Path(data_dir),
    )
    return dispatch_id
