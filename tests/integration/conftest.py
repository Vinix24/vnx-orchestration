"""conftest.py — path setup and markers for integration test subdirectory."""
import sys
from pathlib import Path

import pytest

_LIB_DIR = Path(__file__).resolve().parents[2] / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "requires_aws: integration tests requiring AWS credentials (skipped when absent)",
    )
