"""FastAPI dashboard for the read-only federation aggregator.

Renders a single HTML page at `/` summarizing each registered project's
DB sizes and WAL bytes. Aggregator is read-only — this dashboard never
opens source DBs in writable mode.

Run:
    uvicorn scripts.aggregator.aggregator_dashboard:app --host 127.0.0.1 --port 8910
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from scripts.aggregator import DEFAULT_AGGREGATOR_DB, DEFAULT_AGGREGATOR_DIR
from scripts.aggregator.build_central_view import (
    SOURCE_DBS,
    _default_registry_path,
    _default_view_db_path,
    load_registry,
)

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)

app = FastAPI(title="VNX Aggregator", docs_url=None, redoc_url=None)


def _file_bytes(p: Path) -> int:
    try:
        return p.stat().st_size
    except FileNotFoundError:
        return 0


def _project_rows() -> list[dict]:
    try:
        projects = load_registry(_default_registry_path())
    except FileNotFoundError:
        return []
    rows: list[dict] = []
    for proj in projects:
        state = proj.state_dir
        qi = state / SOURCE_DBS[0]
        rc = state / SOURCE_DBS[1]
        dt = state / SOURCE_DBS[2]
        wal_total = sum(
            _file_bytes(state / f"{name}-wal") for name in SOURCE_DBS
        )
        rows.append(
            {
                "project_id": proj.project_id,
                "name": proj.name,
                "state_dir": str(state),
                "qi_size": _file_bytes(qi) if qi.is_file() else "missing",
                "rc_size": _file_bytes(rc) if rc.is_file() else "missing",
                "dt_size": _file_bytes(dt) if dt.is_file() else "missing",
                "wal_bytes": wal_total,
                "all_present": qi.is_file() and rc.is_file() and dt.is_file(),
            }
        )
    return rows


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    template = _env.get_template("index.html")
    html = template.render(
        view_db=str(_default_view_db_path()),
        refreshed_at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        rows=_project_rows(),
    )
    return HTMLResponse(html)


@app.get("/healthz", response_class=HTMLResponse)
def healthz() -> HTMLResponse:
    return HTMLResponse("ok")
