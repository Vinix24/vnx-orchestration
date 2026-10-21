"""Standalone guarantees: no leaked identity in the plugin, and a manifest that stands alone."""
from __future__ import annotations

import json
import re

from conftest import PKG


FORBIDDEN = {
    "product name": re.compile("v" + "nx", re.IGNORECASE),
    "runtime dir": re.compile(re.escape(".v" + "nx-data")),
    "home path": re.compile(re.escape("/Us" + "ers/")),
    "email": re.compile(r"[\w.+-]+" + "@" + r"[\w-]+\.[\w.-]+"),
    "uuid": re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
}
CREDITS_ALLOWED: set = set()


def is_credits_line(path, line):
    return False


def plugin_files():
    """Every file the plugin ships. The engine lays its type declarations in .claude-plugin/types/; they are not the plugin's."""
    generated = PKG / ".claude-plugin" / "types"
    return [
        p for p in PKG.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts
        and p.suffix != ".pyc" and generated not in p.parents
    ]


def test_no_identity_leaks_in_the_plugin():
    files = plugin_files()
    assert len(files) > 10
    hits = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            for label, pattern in FORBIDDEN.items():
                if pattern.search(line):
                    hits.append("%s:%d %s" % (path.relative_to(PKG), number, label))
    assert hits == []


def test_leak_patterns_do_detect_what_they_claim():
    assert FORBIDDEN["product name"].search("a V" + "nX line")
    assert FORBIDDEN["email"].search("someone" + "@" + "example.org")
    assert FORBIDDEN["uuid"].search("-".join(["123e4567", "e89b", "12d3", "a456", "426614174000"]))
    assert not FORBIDDEN["uuid"].search("<uuid>")


def test_the_manifest_names_the_plugin_and_its_contract():
    manifest = json.loads((PKG / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "context-meter"
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
    assert (PKG / manifest["types"]).is_file()
    modules = json.loads((PKG / "hooks" / "hooks.json").read_text(encoding="utf-8"))["modules"]
    assert modules == ["./register.tsx"]
    assert all((PKG / "hooks" / module).is_file() for module in modules)


def test_the_manifest_version_has_a_changelog_entry_and_a_readme():
    version = json.loads((PKG / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    assert "## [%s]" % version in (PKG / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "Check your window first" in (PKG / "README.md").read_text(encoding="utf-8")
