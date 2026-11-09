#!/usr/bin/env python3
"""The daily-metrics CLI: collect, daily, recollect and discovered commands.

``collect`` writes one day document through ``DayStore``; ``daily`` catches up the
window, runs the discovered ``DAILY_STEP`` commands, then writes the beacon;
``--recollect`` writes a new revision of a final day. One JSON line per day; a
locked run prints ``skipped: locked``.

Commands are discovered, not registered (decision 4): every ``*.py`` in
``commands_dir()`` that exposes a callable ``register(subparsers)`` and a callable
``run(args)`` becomes a subcommand, and ``daily`` runs each of those that declares
an integer ``DAILY_STEP``, ordered by ``(DAILY_STEP, module __name__)``. A module
that raises on import is reported on stderr as one
``{"command_import_failed": ..., "exception": ...}`` line and makes ``daily`` exit
1 -- it is never skipped silently. Each daily step receives
``argparse.Namespace(project_id, now, store, state_dir)``, where ``store`` is the
run's ``DayStore`` and ``state_dir`` a ``Path``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

_LIB_DIR = Path(__file__).resolve().parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from daily_metrics import collect as collect_mod  # noqa: E402
from daily_metrics import days  # noqa: E402
from daily_metrics import store as store_mod  # noqa: E402
from project_root import resolve_central_data_dir  # noqa: E402

EXIT_OK = 0
EXIT_IMPORT_FAILURE = 1
EXIT_USAGE = 2

COMMANDS_DIR = Path(__file__).resolve().parent / "lib" / "daily_metrics" / "commands"
COMMANDS_DIR_ENV = "VNX_DAILY_METRICS_COMMANDS_DIR"


def commands_dir() -> Path:
    """The commands directory: default under this package, or the env override (decision 18)."""
    override = os.environ.get(COMMANDS_DIR_ENV)
    if override:
        return Path(override)
    return COMMANDS_DIR


def _load_command(path: Path) -> tuple[str, ModuleType | None, str | None]:
    """Load one command module; ``(stem, module|None, exception_class|None)``.

    A module without a callable ``register`` and ``run`` is not a command and is
    not a failure (``module`` and ``exception`` are both ``None``).
    """
    stem = path.stem
    try:
        module = collect_mod.load_module(f"daily_metrics.commands.{stem}", path)
    except ImportError:
        return stem, None, "ImportError"
    except Exception as exc:  # vnx-silent-except: an unimportable command is reported and makes daily exit 1, never skipped
        return stem, None, type(exc).__name__
    if not callable(getattr(module, "register", None)) or not callable(getattr(module, "run", None)):
        return stem, None, None
    return stem, module, None


def discover_commands(path: Path) -> tuple[list[ModuleType], list[tuple[str, str]]]:
    """Import every ``*.py`` in ``path`` except ``__init__`` (decision 4).

    Returns ``(modules, failures)``: the modules with a callable ``register`` and
    ``run``, and the failures as ``(stem, exception class name)`` pairs. A failure
    is never skipped -- ``daily`` reports it and exits 1.
    """
    path = Path(path)
    modules: list[ModuleType] = []
    failures: list[tuple[str, str]] = []
    if not path.is_dir():
        return modules, failures
    for file in sorted(path.glob("*.py")):
        if file.name == "__init__.py":
            continue
        stem, module, exception = _load_command(file)
        if exception is not None:
            failures.append((stem, exception))
        elif module is not None:
            modules.append(module)
    return modules, failures


class _RecordingSubparsers:
    """Wraps the top-level subparsers and records the parsers a ``register`` adds."""

    def __init__(self, subparsers: Any) -> None:
        self._subparsers = subparsers
        self.parsers: list[argparse.ArgumentParser] = []

    def add_parser(self, *args: object, **kwargs: object) -> argparse.ArgumentParser:
        parser = self._subparsers.add_parser(*args, **kwargs)
        self.parsers.append(parser)
        return parser

    def __getattr__(self, name: str) -> object:
        return getattr(self._subparsers, name)


def _register_command(subparsers: Any, module: ModuleType) -> None:
    """Add one command module's subcommand(s); ``args.func`` is its ``run``."""
    recorder = _RecordingSubparsers(subparsers)
    module.register(recorder)
    for parser in recorder.parsers:
        parser.set_defaults(func=module.run)


def _shared_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    for option in ("--project-id", "--now", "--store", "--state-dir"):
        parser.add_argument(option, dest=option[2:].replace("-", "_"), default=None)
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="daily_metrics.py",
        description="Collect and report daily lead-time metrics for one project.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    shared = _shared_parser()
    collect_parser = subparsers.add_parser(
        "collect", parents=[shared], help="collect one day or the catch-up window"
    )
    collect_parser.add_argument("--day", dest="day", default=None, metavar="YYYY-MM-DD")
    collect_parser.add_argument("--recollect", dest="recollect", default=None, metavar="DAY")
    collect_parser.add_argument("--reason", dest="reason", default=None)
    collect_parser.set_defaults(func=_run_collect)
    daily_parser = subparsers.add_parser(
        "daily", parents=[shared],
        help="collect the catch-up window, run the daily steps, then write the beacon",
    )
    daily_parser.set_defaults(func=_run_daily)
    for module in discover_commands(commands_dir())[0]:
        _register_command(subparsers, module)
    return parser


def _resolve_project_id(args: argparse.Namespace) -> str | None:
    value = (args.project_id or os.environ.get("VNX_PROJECT_ID") or "").strip()
    return value or None


def _parse_now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    text = value.strip()
    text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"--now must be an ISO datetime, got {value!r}") from exc
    if parsed.utcoffset() is None:
        raise ValueError("--now must be an aware datetime (Z accepted)")
    return parsed.astimezone(timezone.utc)


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"day must be YYYY-MM-DD, got {value!r}") from exc


def _store_root(args: argparse.Namespace, project_id: str) -> Path:
    return Path(args.store) if args.store else store_mod.default_store_dir(project_id)


def _state_dir(args: argparse.Namespace, project_id: str) -> Path:
    fallback = resolve_central_data_dir(project_id) / "state"
    return Path(args.state_dir) if args.state_dir else fallback


def _emit_written(day: date, status: str, path: Path) -> None:
    print(json.dumps({"day": day.isoformat(), "status": status, "path": str(path)}))


def _emit_skipped(day: date, reason: str) -> None:
    print(json.dumps({"day": day.isoformat(), "skipped": reason}))


def _write_day(store: store_mod.DayStore, project_id: str, day: date, now: datetime) -> Path | None:
    if store.base_status(day.isoformat()) == "final":
        _emit_skipped(day, "final")
        return None
    doc = collect_mod.collect_day(collect_mod.CollectContext(project_id, day, now))
    writer = store.write_final if doc["status"] == "final" else store.write_incomplete
    try:
        path = writer(day.isoformat(), doc)
    except store_mod.AlreadyFinal:
        _emit_skipped(day, "final")
        return None
    _emit_written(day, doc["status"], path)
    return path


def _collect_window(store: store_mod.DayStore, project_id: str, now: datetime) -> None:
    existing: dict[date, str] = {}
    for day in days.completed_days(now):
        status = store.base_status(day.isoformat())
        if status is not None:
            existing[day] = status
    for day in days.plan_catchup(now, existing):
        _write_day(store, project_id, day, now)


def _collect_one_day(args: argparse.Namespace, store: store_mod.DayStore,
                     project_id: str, now: datetime) -> int:
    day = _parse_day(args.day)
    if not days.is_settled(day, now):
        print(f"error: {day.isoformat()} is not settled yet", file=sys.stderr)
        return EXIT_USAGE
    _write_day(store, project_id, day, now)
    return EXIT_OK


def _recollect_day(args: argparse.Namespace, store: store_mod.DayStore,
                   project_id: str, now: datetime) -> int:
    day = _parse_day(args.recollect)
    reason = (args.reason or "").strip()
    if not reason:
        print("error: --recollect requires a non-empty --reason", file=sys.stderr)
        return EXIT_USAGE
    if store.base_status(day.isoformat()) != "final":
        print(f"error: {day.isoformat()} has no final file to revise", file=sys.stderr)
        return EXIT_USAGE
    doc = collect_mod.collect_day(collect_mod.CollectContext(project_id, day, now))
    _emit_written(day, doc["status"], store.write_revision(day.isoformat(), doc, reason))
    return EXIT_OK


def _run_collect(args: argparse.Namespace) -> int:
    project_id = _resolve_project_id(args)
    if project_id is None:
        print("error: --project-id or VNX_PROJECT_ID is required", file=sys.stderr)
        return EXIT_USAGE
    now = _parse_now(args.now)
    store_root = _store_root(args, project_id)
    store = store_mod.DayStore(store_root, project_id)
    with collect_mod.run_lock(store_root) as acquired:
        if not acquired:
            print("skipped: locked")
            return EXIT_OK
        if args.recollect is not None:
            return _recollect_day(args, store, project_id, now)
        if args.day is not None:
            return _collect_one_day(args, store, project_id, now)
        _collect_window(store, project_id, now)
        collect_mod.write_beacon(_state_dir(args, project_id), store, now)
    return EXIT_OK


def _daily_step(module: ModuleType) -> int | None:
    """The module's ``DAILY_STEP`` when it is a real integer, else ``None``."""
    step = getattr(module, "DAILY_STEP", None)
    return step if isinstance(step, int) and not isinstance(step, bool) else None


def _run_daily_commands(args: argparse.Namespace, project_id: str, now: datetime,
                        store: store_mod.DayStore) -> list[tuple[str, str]]:
    """Run every command module with an integer ``DAILY_STEP`` (decision 14).

    Ordered by ``(DAILY_STEP, module __name__)``. Returns the import failures as
    ``(stem, exception class name)`` pairs, so ``daily`` can report them and exit 1.
    """
    modules, failures = discover_commands(commands_dir())
    stepped = [module for module in modules if _daily_step(module) is not None]
    stepped.sort(key=lambda module: (_daily_step(module), module.__name__))
    context = argparse.Namespace(
        project_id=project_id, now=now, store=store,
        state_dir=_state_dir(args, project_id),
    )
    for module in stepped:
        module.run(context)
    return failures


def _run_daily(args: argparse.Namespace) -> int:
    project_id = _resolve_project_id(args)
    if project_id is None:
        print("error: --project-id or VNX_PROJECT_ID is required", file=sys.stderr)
        return EXIT_USAGE
    now = _parse_now(args.now)
    store_root = _store_root(args, project_id)
    store = store_mod.DayStore(store_root, project_id)
    with collect_mod.run_lock(store_root) as acquired:
        if not acquired:
            print("skipped: locked")
            return EXIT_OK
        _collect_window(store, project_id, now)
        failures = _run_daily_commands(args, project_id, now, store)
        for stem, exception in failures:
            print(json.dumps({"command_import_failed": stem, "exception": exception}),
                  file=sys.stderr)
        collect_mod.write_beacon(_state_dir(args, project_id), store, now)
    return EXIT_IMPORT_FAILURE if failures else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
