#!/usr/bin/env python3
"""Regression tests for playwright.config.ts webServer configuration.

Verifies that the Playwright config defines a webServer block so the
E2E suite is self-starting from a clean environment.

Dispatch-ID: 20260429-fix-pr312-codex
"""

import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLAYWRIGHT_CONFIG = PROJECT_ROOT / "dashboard" / "token-dashboard" / "playwright.config.ts"


class TestPlaywrightWebServerConfig(unittest.TestCase):
    """Ensure playwright.config.ts has a webServer block."""

    @classmethod
    def setUpClass(cls):
        cls.config_text = PLAYWRIGHT_CONFIG.read_text(encoding="utf-8")

    def test_config_file_exists(self):
        self.assertTrue(PLAYWRIGHT_CONFIG.exists(), f"Missing: {PLAYWRIGHT_CONFIG}")

    def test_webserver_block_present(self):
        """playwright.config.ts must define a webServer key."""
        self.assertIn(
            "webServer",
            self.config_text,
            "playwright.config.ts is missing a 'webServer' block — "
            "the E2E suite will not self-start from a clean environment.",
        )

    def test_webserver_has_command(self):
        """webServer block must include a command to launch the Next.js server."""
        match = re.search(r"webServer\s*:\s*\{([^}]+)\}", self.config_text, re.DOTALL)
        self.assertIsNotNone(match, "Could not locate webServer block body")
        block = match.group(1)
        self.assertIn("command", block, "webServer block must have a 'command' key")

    def test_webserver_command_starts_next(self):
        """webServer command must start the Next.js dev server."""
        match = re.search(r"command\s*:\s*['\"]([^'\"]+)['\"]", self.config_text)
        self.assertIsNotNone(match, "Could not parse webServer command value")
        command = match.group(1)
        self.assertIn("npm", command, f"webServer command should invoke npm, got: {command!r}")

    def test_webserver_has_url(self):
        """webServer block must include a url for readiness polling."""
        match = re.search(r"webServer\s*:\s*\{([^}]+)\}", self.config_text, re.DOTALL)
        self.assertIsNotNone(match, "Could not locate webServer block body")
        block = match.group(1)
        self.assertIn("url", block, "webServer block must have a 'url' key for readiness polling")

    def test_webserver_url_matches_base_url(self):
        """webServer url must match the baseURL port (3100)."""
        self.assertIn(
            "3100",
            self.config_text,
            "webServer url should reference port 3100 to match baseURL",
        )

    def test_webserver_reuse_existing_server_set(self):
        """webServer must set reuseExistingServer to avoid CI conflicts."""
        match = re.search(r"webServer\s*:\s*\{([^}]+)\}", self.config_text, re.DOTALL)
        self.assertIsNotNone(match, "Could not locate webServer block body")
        block = match.group(1)
        self.assertIn(
            "reuseExistingServer",
            block,
            "webServer block should set 'reuseExistingServer' to control CI vs local behavior",
        )

    def test_webserver_timeout_set(self):
        """webServer must set a timeout so CI doesn't hang indefinitely."""
        match = re.search(r"webServer\s*:\s*\{([^}]+)\}", self.config_text, re.DOTALL)
        self.assertIsNotNone(match, "Could not locate webServer block body")
        block = match.group(1)
        self.assertIn(
            "timeout",
            block,
            "webServer block should set a 'timeout' for server startup",
        )

    def test_base_url_still_configured(self):
        """baseURL must still be present in the use block."""
        self.assertIn("baseURL", self.config_text, "baseURL must remain in use block")

    def test_base_url_port_3100(self):
        """baseURL must reference port 3100."""
        match = re.search(r"baseURL\s*:\s*[^\n,]+", self.config_text)
        self.assertIsNotNone(match, "Could not find baseURL line")
        self.assertIn("3100", match.group(0), "baseURL must use port 3100")


if __name__ == "__main__":
    unittest.main()
