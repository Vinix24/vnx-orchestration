"""skill_application — deterministic proof that a spec-bound skill reached the worker.

ADR-048 §3 (track refactor-skill-en-levering, RS-3) makes the receipt truthful
about skill delivery: ``skill_applied`` / ``skill_source_path`` /
``skill_unapplied_reason``. The measurement is taken on the FINAL prompt the
lane handed the worker, never on a flag the injection seam sets — a seam that
believed it injected something is not evidence that the bytes arrived.

The contract table (ADR-048 §3), one row per outcome:

  no ``skill`` in the spec      -> ``None`` (all three receipt fields absent)
  body literally in the prompt  -> ``True`` + absolute path of the resolved SKILL.md
  resolution failed at delivery -> ``False`` + ``resolution_failed: <reason>``
  dispatch bound to a reviewer  -> ``False`` + ``reviewer_bound``

The resolved ``SKILL.md`` is read HERE, at delivery time, and its body (with the
YAML frontmatter stripped, exactly as ``skill_prefix.build_skill_prefix``
embedded it) must occur literally in the prompt. ``enrichment_allowed`` is
checked first: a reviewer with a skill in its spec never claims arrival, even
if the body is present, because by decision the skill does not go to reviewers.

``role_applied`` is deliberately NOT stretched to cover skills (ADR-048 §3): for
a reviewer it already reads ``True`` with no source, and a skill under it would
make the receipt lie about which source proved arrival.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from reviewer_roles import enrichment_allowed

logger = logging.getLogger(__name__)

_REASON_REVIEWER_BOUND = "reviewer_bound"


@dataclass(frozen=True)
class SkillApplicationVerdict:
    """Outcome of the skill-applied check, stamped on the dispatch receipt.

    ``skill_source_path`` is only set when ``skill_applied`` is True;
    ``skill_unapplied_reason`` is only set when it is False. When the spec
    carries no skill there is no verdict at all (``None``), so an absent field
    on the ledger reads as "no skill requested" (ADR-048 §3).
    """

    skill_applied: bool
    skill_source_path: Optional[str] = None
    skill_unapplied_reason: Optional[str] = None


def skill_receipt_fields(verdict: Optional[SkillApplicationVerdict]) -> dict[str, Any]:
    """The three receipt keyword arguments for *verdict*; empty when it is None.

    Keeps every lane's stamp to one ``**skill_receipt_fields(...)`` line so a
    large govern function gains no branch of its own for the skill fields.
    """
    if verdict is None:
        return {}
    return {
        "skill_applied": verdict.skill_applied,
        "skill_source_path": verdict.skill_source_path,
        "skill_unapplied_reason": verdict.skill_unapplied_reason,
    }


def _skill_slug(dispatch_metadata: "dict | None") -> str:
    """The spec-bound slug, or '' when the metadata carries none.

    A non-string value (a mocked attribute, a hand-built dict) is not a slug;
    treating it as absent is the fail-closed answer for the "no skill" row and
    never turns into a path.
    """
    if not isinstance(dispatch_metadata, dict):
        return ""
    raw = dispatch_metadata.get("skill")
    return raw.strip() if isinstance(raw, str) else ""


def _resolution_failed(reason: str) -> SkillApplicationVerdict:
    return SkillApplicationVerdict(
        skill_applied=False,
        skill_source_path=None,
        skill_unapplied_reason=f"resolution_failed: {reason}",
    )


def verify_skill_applied(
    prompt_text: str,
    dispatch_metadata: "dict | None",
    role: Optional[str],
    project_root: "Path | str | None" = None,
) -> Optional[SkillApplicationVerdict]:
    """Verify the spec-bound skill's body reached *prompt_text*; None = no skill.

    ``project_root`` defaults to ``role_application.resolve_project_root()`` —
    the same walk (``subprocess_dispatch.__file__`` → parents[2]) the injector
    and the role control use — and is explicit so tests and migrations can
    point the check at a throwaway tree.

    The skill is resolved through ``skill_prefix.resolve_skill_dir`` (the ONE
    existence resolver staging and the door's compile_plan rule share), then
    ``SKILL.md`` is read at this moment: a file that vanished, became
    unreadable or was replaced between staging and delivery fails the check
    instead of inheriting an older verdict.
    """
    slug = _skill_slug(dispatch_metadata)
    if not slug:
        return None

    # Row 4 first (ADR-048 §3): a reviewer with a skill never claims arrival,
    # even when the body happens to be in the prompt — the decision that it is
    # not sent is what the receipt must record.
    if not enrichment_allowed(role):
        return SkillApplicationVerdict(
            skill_applied=False,
            skill_source_path=None,
            skill_unapplied_reason=_REASON_REVIEWER_BOUND,
        )

    from role_application import resolve_project_root  # noqa: PLC0415
    from skill_prefix import (  # noqa: PLC0415
        _strip_frontmatter,
        resolve_skill_dir,
        skill_search_paths,
    )

    if project_root is None:
        project_root = resolve_project_root()
    root = Path(project_root)

    if not skill_search_paths(root, slug):
        return _resolution_failed("invalid_skill_slug")

    folder = resolve_skill_dir(root, slug)
    if folder is None:
        return _resolution_failed("skill_not_found")

    skill_md = folder / "SKILL.md"
    try:
        raw = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("skill_application: cannot read %s: %s", skill_md, exc)
        return _resolution_failed(f"skill_unreadable: {exc}")

    body = _strip_frontmatter(raw)
    if not body.strip():
        # An empty body occurs in every prompt; letting it match would claim
        # arrival for a skill that carries nothing to deliver.
        return _resolution_failed("empty_skill_body")

    if body not in (prompt_text or ""):
        return _resolution_failed("body_not_in_prompt")

    return SkillApplicationVerdict(
        skill_applied=True,
        skill_source_path=str(skill_md.resolve()),
        skill_unapplied_reason=None,
    )
