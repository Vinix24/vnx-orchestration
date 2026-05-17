"""unified_report_schema.py — JSONSchema validator for unified report frontmatter.

Used by PR-D5-E guardrail (scripts/guardrails/verify_report_schema.sh) and (from
PR-D5-F onwards) by governance_emit when writing the unified report.

Public surface:
    SchemaViolation
    SCHEMA_VERSION
    SCHEMA_PATH
    load_schema()
    extract_frontmatter(text) -> (frontmatter_text, body)
    parse_frontmatter(text) -> dict
    validate_frontmatter(text) -> dict
    validate_file(path) -> dict
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

SCHEMA_VERSION = 1
SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "schemas" / "unified_report_v1.json"
)


class SchemaViolation(ValueError):
    """Raised when a unified report frontmatter fails schema validation."""


_SCHEMA_CACHE: Optional[Dict[str, Any]] = None
_VALIDATOR_CACHE: Optional[Draft202012Validator] = None


def load_schema(schema_path: Path = SCHEMA_PATH) -> Dict[str, Any]:
    """Load the unified-report schema JSON. Cached on first call."""
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is None or schema_path != SCHEMA_PATH:
        try:
            with open(schema_path, "r", encoding="utf-8") as fh:
                schema = json.load(fh)
        except FileNotFoundError as exc:
            raise SchemaViolation(
                f"schema file not found: {schema_path}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise SchemaViolation(
                f"schema file is not valid JSON ({schema_path}): {exc}"
            ) from exc
        if schema_path == SCHEMA_PATH:
            _SCHEMA_CACHE = schema
        return schema
    return _SCHEMA_CACHE


def _get_validator() -> Draft202012Validator:
    global _VALIDATOR_CACHE
    if _VALIDATOR_CACHE is None:
        schema = load_schema()
        Draft202012Validator.check_schema(schema)
        _VALIDATOR_CACHE = Draft202012Validator(schema)
    return _VALIDATOR_CACHE


def extract_frontmatter(text: str) -> Tuple[str, str]:
    """Split a markdown document into (frontmatter_text, body).

    Frontmatter is recognised only when the document starts with a ``---``
    fence on the first non-empty line and is closed by a second ``---`` fence.

    Raises SchemaViolation when no frontmatter block is present or when the
    fence is not closed.
    """
    if text is None:
        raise SchemaViolation("report text is empty: no frontmatter block")

    lines = text.splitlines()
    idx = 0
    while idx < len(lines) and lines[idx].strip() == "":
        idx += 1

    if idx >= len(lines) or lines[idx].strip() != "---":
        raise SchemaViolation(
            "missing YAML frontmatter: report must start with '---' fence"
        )

    start = idx + 1
    end: Optional[int] = None
    for j in range(start, len(lines)):
        if lines[j].strip() == "---":
            end = j
            break

    if end is None:
        raise SchemaViolation(
            "unterminated YAML frontmatter: closing '---' fence not found"
        )

    frontmatter_text = "\n".join(lines[start:end])
    body = "\n".join(lines[end + 1 :])
    return frontmatter_text, body


def parse_frontmatter(text: str) -> Dict[str, Any]:
    """Extract + YAML-parse the frontmatter mapping.

    Raises SchemaViolation when the YAML is invalid or not a mapping.
    """
    frontmatter_text, _body = extract_frontmatter(text)
    try:
        data = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as exc:
        raise SchemaViolation(f"invalid YAML in frontmatter: {exc}") from exc

    if data is None:
        raise SchemaViolation("empty YAML frontmatter")
    if not isinstance(data, dict):
        raise SchemaViolation(
            f"frontmatter must be a YAML mapping, got {type(data).__name__}"
        )
    return data


def _format_error(err: ValidationError) -> str:
    path = list(err.absolute_path)
    pointer = ".".join(str(p) for p in path) if path else "<root>"
    validator = err.validator

    if validator == "required":
        missing = err.message.split("'")[1] if "'" in err.message else err.message
        return f"missing field: {missing}"

    if validator == "type":
        expected = err.validator_value
        return f"invalid type: {pointer} (expected {expected})"

    if validator == "const":
        return (
            f"invalid type: {pointer} (must equal {err.validator_value!r}, "
            f"got {err.instance!r})"
        )

    if validator == "additionalProperties":
        return f"invalid type: {pointer} ({err.message})"

    if validator == "minimum":
        return (
            f"invalid type: {pointer} (must be >= {err.validator_value}, "
            f"got {err.instance!r})"
        )

    if validator == "minLength":
        return f"invalid type: {pointer} (must be non-empty string)"

    return f"invalid type: {pointer} ({err.message})"


def validate_frontmatter(text: str) -> Dict[str, Any]:
    """Validate unified-report frontmatter found in *text*.

    Returns the parsed frontmatter mapping on success.

    Raises:
        SchemaViolation: when the frontmatter is missing, malformed, or fails
            JSONSchema validation. The first violation is surfaced with a
            human-readable message such as "missing field: provider" or
            "invalid type: cost_usd (expected number)".
    """
    data = parse_frontmatter(text)
    validator = _get_validator()
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    if errors:
        raise SchemaViolation(_format_error(errors[0]))
    return data


def validate_file(path: Path) -> Dict[str, Any]:
    """Read *path* from disk and validate its frontmatter."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SchemaViolation(f"report file not found: {path}") from exc
    except OSError as exc:
        raise SchemaViolation(f"cannot read report {path}: {exc}") from exc
    return validate_frontmatter(text)
