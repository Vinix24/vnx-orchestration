"""Tests for the worktree receipt-ledger backfill."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.backfill_wt_receipts import BACKFILLED_FROM, build_plan, main, receipt_key


def _json_line(**values: str) -> str:
    return json.dumps(values, separators=(",", ":"))


def _write_lines(path: Path, lines: list[str], *, final_newline: bool = True) -> None:
    text = "\n".join(lines)
    if final_newline and lines:
        text += "\n"
    path.write_text(text, encoding="utf-8")


def _make_ledgers(tmp_path: Path) -> tuple[Path, Path]:
    archive = tmp_path / "archive.ndjson"
    central = tmp_path / "central.ndjson"
    _write_lines(
        archive,
        [
            "",
            "{bad-json",
            _json_line(dispatch_id="20260515-old", event_type="task_complete"),
            _json_line(dispatch_id="20260516-recent", event_type="task_started"),
            _json_line(dispatch_id="20260516-recent", event_type="task_complete"),
            _json_line(
                dispatch_id="bench-debug-1",
                event_type="task_complete",
                timestamp="2026-06-04T12:00:00Z",
            ),
            _json_line(
                dispatch_id="headless-review:old",
                event_type="task_complete",
                timestamp="2026-04-01T12:00:00Z",
            ),
            _json_line(dispatch_id="20260601-existing", event_type="task_complete"),
        ],
    )
    _write_lines(
        central,
        [_json_line(dispatch_id="20260601-existing", event_type="task_complete")],
        final_newline=False,
    )
    return archive, central


def test_receipt_key_uses_required_precedence_and_stable_sha1_fallback() -> None:
    raw = _json_line(event_type="task_complete")

    assert receipt_key(
        {
            "dispatch_id": "dispatch",
            "cmd_id": "command",
            "trace_token": "trace",
            "task_id": "task",
        },
        raw,
    ) == "dispatch"
    assert receipt_key({"cmd_id": "command", "task_id": "task"}, raw) == "command"
    assert receipt_key({}, raw) == hashlib.sha1(raw.encode("utf-8")).hexdigest()


def test_selection_includes_recent_and_non_date_but_excludes_old_and_existing(
    tmp_path: Path,
) -> None:
    archive, central = _make_ledgers(tmp_path)

    plan = build_plan(archive, central)

    assert plan.selected_keys == ("20260516-recent", "bench-debug-1")
    assert [line.key for line in plan.lines_to_append] == [
        "20260516-recent",
        "20260516-recent",
        "bench-debug-1",
    ]
    assert "20260515-old" not in plan.selected_keys
    assert "headless-review:old" not in plan.selected_keys
    assert "20260601-existing" not in plan.selected_keys
    assert plan.archive.blank_lines == 1
    assert plan.archive.unparseable_lines == 1


def test_apply_appends_selected_lines_with_marker_backs_up_and_is_idempotent(
    tmp_path: Path,
    capsys,
) -> None:
    archive, central = _make_ledgers(tmp_path)
    original_central = central.read_bytes()
    args = ["--archive", str(archive), "--central", str(central), "--apply"]

    assert main(args) == 0
    first_output = capsys.readouterr().out

    backups = list(tmp_path.glob("central.ndjson.bak-backfill-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original_central
    records = [
        json.loads(line)
        for line in central.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(records) == 4
    appended = records[1:]
    assert [record["dispatch_id"] for record in appended] == [
        "20260516-recent",
        "20260516-recent",
        "bench-debug-1",
    ]
    assert all(record["backfilled_from"] == BACKFILLED_FROM for record in appended)
    assert "Line-count verification: old=1, new=4, appended=3" in first_output

    after_first_apply = central.read_bytes()
    assert main(args) == 0
    second_output = capsys.readouterr().out

    assert central.read_bytes() == after_first_apply
    assert len(list(tmp_path.glob("central.ndjson.bak-backfill-*"))) == 1
    assert "Selected key count: 0" in second_output
    assert "selected 0 lines, no backup or append performed" in second_output
