"""Config control-plane API handlers (P0 PR 4).

The HTTP surface over the config DAO (config_registry #947 + config_store_db #948/#949):

  GET  /api/operator/config        -> the registry inventory with effective values + provenance
  POST /api/operator/config/set    -> set_config (validate vs registry, write value + mandatory audit)
  GET  /api/operator/config/audit  -> recent project_config_audit rows for this project (newest first)

Single-tenant: the dashboard serves one project (CANONICAL_STATE_DIR). Every write is tenant-scoped
to that project_id and lands in its runtime_coordination.db. Handlers accept optional ``project_id``
/ ``db_path`` overrides for testing (mirrors api_operator's ``config_path=None`` gate handlers).
"""
from __future__ import annotations

import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# One source of truth for "which project / which state dir" — reuse the operator module's derivation.
from api_operator import CANONICAL_STATE_DIR, VNX_DIR, _op_dashboard_project_id

_logger = logging.getLogger(__name__)

_CFG_SCRIPTS_LIB = str(VNX_DIR / "scripts" / "lib")
if _CFG_SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, _CFG_SCRIPTS_LIB)

try:
    import config_registry as _cr   # type: ignore[import]
    import config_store_db as _cs    # type: ignore[import]
    _CONFIG_AVAILABLE = True
except Exception:
    # Optional dependency: the config-store modules may be absent in a stripped install. Log so a
    # real init error is visible (not silently mistaken for "registry unavailable") but stay non-fatal.
    _logger.warning("config control-plane modules unavailable; config API will report 503", exc_info=True)
    _cr = None  # type: ignore[assignment]
    _cs = None  # type: ignore[assignment]
    _CONFIG_AVAILABLE = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _config_db_path(db_path: "Path | None" = None) -> Path:
    return db_path or (CANONICAL_STATE_DIR / "runtime_coordination.db")


def _wire_resolver(state_dir: "Path | None" = None) -> None:
    """Wire config_registry's DB layer (precedence step 2) to a state dir so the inventory reflects
    UI-set values. Single-tenant: every project_id maps to this one dir. Idempotent; fail-soft."""
    if not _CONFIG_AVAILABLE:
        return
    target = state_dir or CANONICAL_STATE_DIR
    try:
        _cr.set_db_resolver(_cs.make_db_resolver(lambda _pid: target))
    except Exception:  # vnx-silent-except: resolver wiring is best-effort; a failure must never break dashboard import
        pass


# Wire at import for the production dashboard process.
_wire_resolver()


def operator_get_config(params: dict, *, project_id: "str | None" = None) -> "tuple[dict, int]":
    """GET /api/operator/config -- every operator flag with its effective value + provenance.

    Returns (body, status): 200 on success; 503 when the registry is unavailable / errors (the body
    carries a generic message — the exception detail is logged server-side, never returned)."""
    pid = project_id or _op_dashboard_project_id()
    if not _CONFIG_AVAILABLE:
        return {"project_id": pid, "config": [], "queried_at": _now(),
                "error": "config registry unavailable"}, 503
    try:
        rows = _cr.all_effective(pid)
    except Exception:
        _logger.exception("config inventory failed for project_id=%s", pid)
        return {"project_id": pid, "config": [], "queried_at": _now(),
                "error": "config inventory failed"}, 503
    return {"project_id": pid, "config": rows, "queried_at": _now()}, 200


def operator_post_config_set(
    body: dict, *, project_id: "str | None" = None, db_path: "Path | None" = None,
) -> "tuple[dict, int]":
    """POST /api/operator/config/set -- validate vs registry, then write value + mandatory audit row.

    Body: ``{key, value, actor?, approval_id?}``. Maps the DAO's exceptions to HTTP:
    ValueError (unknown key / bad value) -> 400; PermissionError (not writable / approval required)
    -> 403; success -> 200.
    """
    now = _now()
    if not _CONFIG_AVAILABLE:
        return {"status": "failed", "message": "config store unavailable", "timestamp": now}, 503

    key = body.get("key", "")
    if not key or not isinstance(key, str):
        return {"status": "failed", "message": "key is required", "timestamp": now}, 400
    if "value" not in body:
        return {"status": "failed", "message": "value is required", "timestamp": now}, 400
    value = body.get("value")
    actor = body.get("actor") or "operator"
    approval_id = body.get("approval_id") or None

    pid = project_id or _op_dashboard_project_id()
    if not pid:
        return {"status": "failed", "message": "could not resolve project_id", "timestamp": now}, 500

    path = _config_db_path(db_path)
    try:
        conn = sqlite3.connect(str(path))
    except sqlite3.Error:
        _logger.exception("config db open failed (project_id=%s)", pid)
        return {"status": "failed", "message": "database unavailable", "timestamp": now}, 500
    try:
        result = _cs.set_config(conn, pid, key, value, actor=actor, approval_id=approval_id)
    except ValueError as exc:
        # Validation messages are caller-facing by design (echo the supplied key + a static reason).
        return {"status": "failed", "message": str(exc), "timestamp": now}, 400
    except PermissionError as exc:
        return {"status": "failed", "message": str(exc), "timestamp": now}, 403
    except Exception:
        _logger.exception("config set failed (key=%s project_id=%s)", key, pid)
        return {"status": "failed", "message": "config write failed", "timestamp": now}, 500
    finally:
        conn.close()

    return {
        "status": "success",
        "action": "config/set",
        "project_id": pid,
        "key": key,
        "old_value": result["old_value"],
        "new_value": result["new_value"],
        "event_id": result["event_id"],
        "actor": actor,
        "approval_id": approval_id,
        "timestamp": now,
    }, 200


def operator_get_config_audit(
    params: dict, *, project_id: "str | None" = None, db_path: "Path | None" = None,
) -> "tuple[dict, int]":
    """GET /api/operator/config/audit -- recent config changes for this project (newest first).

    Query: ``?limit=<1..500>`` (default 50), optional ``?key=<config_key>`` filter. Fail-open: a
    missing DB / missing tables -> empty audit (200). A real DB error logs server-side and returns
    an empty, ``degraded``-flagged body (200) — the exception detail is never returned to clients."""
    pid = project_id or _op_dashboard_project_id()
    limit_raw = (params.get("limit") or ["50"])[0]
    try:
        limit = max(1, min(int(limit_raw), 500))
    except (TypeError, ValueError):
        limit = 50
    key_filter = (params.get("key") or [None])[0]

    out: dict = {"project_id": pid, "audit": [], "queried_at": _now()}
    path = _config_db_path(db_path)
    if not _CONFIG_AVAILABLE or not path.exists():
        return out, 200
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        _logger.exception("config audit db open failed (project_id=%s)", pid)
        out["degraded"] = True
        return out, 200
    try:
        if not _cs.has_config_tables(conn):
            return out, 200
        conn.row_factory = sqlite3.Row
        sql = (
            "SELECT config_key, old_value, new_value, changed_by, changed_at, approval_id, event_id "
            "FROM project_config_audit WHERE project_id = ?"
        )
        args: list = [pid]
        if key_filter:
            sql += " AND config_key = ?"
            args.append(key_filter)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        out["audit"] = [dict(r) for r in conn.execute(sql, args).fetchall()]
    except sqlite3.Error:
        _logger.exception("config audit query failed (project_id=%s)", pid)
        out["degraded"] = True
    finally:
        conn.close()
    return out, 200
