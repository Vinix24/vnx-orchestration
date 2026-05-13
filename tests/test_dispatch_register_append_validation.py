"""Codex round-1 finding: append_dispatch_event must reject non-dict, missing dispatch_id, invalid event_type."""
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

from dispatch_register import append_dispatch_event


class TestAppendValidation:
    def test_rejects_null(self, tmp_path):
        register = tmp_path / "reg.ndjson"
        with pytest.raises(ValueError, match="object"):
            append_dispatch_event(register, "null")

    def test_rejects_array(self, tmp_path):
        register = tmp_path / "reg.ndjson"
        with pytest.raises(ValueError, match="object"):
            append_dispatch_event(register, "[1,2,3]")

    def test_rejects_scalar(self, tmp_path):
        register = tmp_path / "reg.ndjson"
        with pytest.raises(ValueError, match="object"):
            append_dispatch_event(register, "42")

    def test_rejects_missing_dispatch_id(self, tmp_path):
        register = tmp_path / "reg.ndjson"
        with pytest.raises(ValueError, match="dispatch_id"):
            append_dispatch_event(register, '{"event_type": "dispatch_promoted"}')

    def test_rejects_invalid_event_type(self, tmp_path):
        register = tmp_path / "reg.ndjson"
        with pytest.raises(ValueError, match="event_type"):
            append_dispatch_event(register, '{"dispatch_id": "d1", "event_type": "bogus"}')

    def test_rejects_invalid_json(self, tmp_path):
        register = tmp_path / "reg.ndjson"
        with pytest.raises(ValueError, match="invalid JSON"):
            append_dispatch_event(register, "not json")

    def test_accepts_valid_event(self, tmp_path):
        register = tmp_path / "reg.ndjson"
        append_dispatch_event(register, '{"dispatch_id": "d1", "event_type": "dispatch_promoted"}')
        assert register.exists()
