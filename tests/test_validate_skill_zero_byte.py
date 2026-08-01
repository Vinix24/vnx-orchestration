#!/usr/bin/env python3
"""OI-660: a zero-byte/corrupt skills.yaml must fail loud, not crash with a
NoneType AttributeError.

validate_skill.SkillValidator loads the registry with ``yaml.safe_load``; a
zero-byte file parses to ``None`` and the old code crashed on
``None.get('skills', {})``. The guard turns that into a clear ValueError so
the operator sees the real cause, not a confusing traceback tail.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
import sys  # noqa: E402
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))


def _build_validator(skills_dir: Path):
    """Instantiate SkillValidator against a throwaway skills dir by faking the
    environment resolution (the constructor only reads VNX_SKILLS_DIR)."""
    import validate_skill as vs
    with patch("vnx_paths.ensure_env", return_value={"VNX_SKILLS_DIR": str(skills_dir)}):
        return vs.SkillValidator()


def test_zero_byte_registry_raises_clear_valueerror(tmp_path):
    (tmp_path / "skills.yaml").write_text("")

    with pytest.raises(ValueError, match="empty or not valid YAML"):
        _build_validator(tmp_path)


def test_valid_registry_loads(tmp_path):
    (tmp_path / "skills.yaml").write_text(
        "skills:\n  planner:\n    name: \"@planner\"\n"
    )

    validator = _build_validator(tmp_path)
    assert validator.skills == {
        "planner": {"name": "@planner"},
    }
