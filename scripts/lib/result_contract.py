#!/usr/bin/env python3
"""Shared Result contract helpers for CLI boundary stability."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

EXIT_OK = 0
EXIT_VALIDATION = 10
EXIT_IO = 20
EXIT_DEPENDENCY = 30
EXIT_INTERNAL = 40

DEFAULT_ERROR_EXIT_CODE_MAP: Dict[str, int] = {
    "missing_argument": EXIT_VALIDATION,
    "invalid_argument": EXIT_VALIDATION,
    "unknown_command": EXIT_VALIDATION,
    "invalid_feature_plan": EXIT_VALIDATION,
    "initialization_failed": EXIT_DEPENDENCY,
    "usage_log_init_failed": EXIT_IO,
    "quality_db_unavailable": EXIT_DEPENDENCY,
    "tag_engine_unavailable": EXIT_DEPENDENCY,
    "query_execution_failed": EXIT_DEPENDENCY,
    "dependency_unmet": EXIT_DEPENDENCY,
    "dispatch_not_found": EXIT_IO,
    "pr_not_found": EXIT_IO,
    "operation_failed": EXIT_DEPENDENCY,
    "unexpected_error": EXIT_INTERNAL,
    "invalid_handover": EXIT_VALIDATION,
    "invalid_resume": EXIT_VALIDATION,
}


@dataclass(frozen=True)
class Result:
    ok: bool
    data: Any = None
    error_code: Optional[str] = None
    error_msg: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "data": self.data,
            "error_code": self.error_code,
            "error_msg": self.error_msg,
        }


def result_ok(data: Any = None) -> Result:
    return Result(ok=True, data=data, error_code=None, error_msg=None)


def result_error(error_code: str, error_msg: str, data: Any = None) -> Result:
    return Result(ok=False, data=data, error_code=error_code, error_msg=error_msg)


def result_exit_code(
    result: Result,
    *,
    error_code_map: Optional[Mapping[str, int]] = None,
    default_error_exit_code: int = EXIT_INTERNAL,
) -> int:
    if result.ok:
        return EXIT_OK

    mapped = error_code_map or DEFAULT_ERROR_EXIT_CODE_MAP
    if result.error_code and result.error_code in mapped:
        return mapped[result.error_code]
    return default_error_exit_code
