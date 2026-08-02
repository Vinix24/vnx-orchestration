"""Tests voor scripts/refactor_equivalence.py (dispatch 20260802-f0-refactor-proof-tools).

Elke test moet ook rood KUNNEN staan (OI-893): de faalgevallen zijn expliciet getoetst
tegen de ongefixte bronversie uit claudedocs/refactor-tools/ — zie het dispatch-rapport
voor de exit-codes van die rood-runs.

De tool vergelijkt een git-ref met de werkkopie, dus elke test bouwt een eigen tijdelijke
git-repo in tmp_path. Er wordt geen gedeelde --basetemp geforceerd.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "scripts" / "refactor_equivalence.py"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )


def _make_repo(tmp_path: Path, committed: dict[str, str]) -> Path:
    """Maak een git-repo in tmp_path met `committed` als initiële commit (HEAD)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    for rel, content in committed.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=test@example.com", "-c", "user.name=test",
         "commit", "-m", "init")
    return repo


def _run(repo: Path, before_file: str, after_file: str, functions: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable, str(TOOL),
            "--before-ref", "HEAD",
            "--before-file", before_file,
            "--after-file", after_file,
            "--functions", functions,
        ],
        cwd=repo, capture_output=True, text=True,
    )


def test_onveranderde_verplaatsing_is_identiek(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, {
        "oud.py": "def foo(a, b):\n    return a + b\n",
        "nieuw.py": "def foo(a, b):\n    return a + b\n",
    })
    r = _run(repo, "oud.py", "nieuw.py", "foo")
    assert r.returncode == 0, r.stderr
    assert "IDENTIEK" in r.stdout
    assert "1/1 functies bewezen" in r.stdout


def test_gewijzigde_body_is_luide_fail(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, {
        "oud.py": "def foo(a, b):\n    return a + b\n",
        "nieuw.py": "def foo(a, b):\n    return a * b\n",
    })
    r = _run(repo, "oud.py", "nieuw.py", "foo")
    assert r.returncode == 1
    assert "GEWIJZIGD" in r.stdout


def test_ontbrekende_functie_in_doelmodule_faalt_luid(tmp_path: Path) -> None:
    """Een niet-gevonden functie mag nooit stil slagen: exit 1 met melding."""
    repo = _make_repo(tmp_path, {
        "oud.py": "def foo():\n    return 1\n",
        "nieuw.py": "def bar():\n    return 2\n",
    })
    r = _run(repo, "oud.py", "nieuw.py", "foo")
    assert r.returncode == 1
    assert "niet gevonden" in r.stderr
    assert "IDENTIEK" not in r.stdout


def test_klasse_methode_pakt_geen_gelijknamige_toplevel(tmp_path: Path) -> None:
    """'Klasse.methode' moet de methode vergelijken, niet de top-level functie."""
    repo = _make_repo(tmp_path, {
        "oud.py": (
            "def run():\n    return 'top-level'\n\n"
            "class Worker:\n"
            "    def run(self):\n"
            "        return 'methode'\n"
        ),
        "nieuw.py": (
            "class Worker:\n"
            "    def run(self):\n"
            "        return 'methode'\n"
        ),
    })
    r = _run(repo, "oud.py", "nieuw.py", "Worker.run")
    assert r.returncode == 0, r.stderr
    assert "IDENTIEK    Worker.run" in r.stdout


def test_geneste_functie_wordt_genegeerd_toplevel_wint(tmp_path: Path) -> None:
    """Geneste closure met dezelfde naam mag de vergelijking niet vervuilen.

    De geneste `target` verschilt tussen voor en na, de top-level `target` niet.
    De tool moet IDENTIEK zeggen omdat hij de top-level pakt (mankement 1).
    """
    repo = _make_repo(tmp_path, {
        "oud.py": (
            "def wrapper():\n"
            "    def target():\n"
            "        return 'geneste-variant-A'\n"
            "    return target()\n\n"
            "def target():\n"
            "    return 'top-level'\n"
        ),
        "nieuw.py": (
            "def wrapper():\n"
            "    def target():\n"
            "        return 'geneste-variant-B'\n"
            "    return target()\n\n"
            "def target():\n"
            "    return 'top-level'\n"
        ),
    })
    r = _run(repo, "oud.py", "nieuw.py", "target")
    assert r.returncode == 0, r.stderr
    assert "IDENTIEK    target" in r.stdout


def test_ontbrekende_toplevel_valt_niet_stil_terug_op_geneste(tmp_path: Path) -> None:
    """Regressie op mankement 1: de stille-faalmodus van de oude ast.walk.

    Na de verplaatsing bestaat `target` alleen nog als geneste closure, met een
    body die toevallig identiek is aan de oude top-level. De ongefixte tool vond
    die closure via ast.walk en riep IDENTIEK (exit 0) over de verkeerde functie.
    De fix moet luid falen: de top-level functie is weg.
    """
    repo = _make_repo(tmp_path, {
        "oud.py": "def target():\n    return 'real'\n",
        "nieuw.py": (
            "def wrapper():\n"
            "    def target():\n"
            "        return 'real'\n"
            "    return target()\n"
        ),
    })
    r = _run(repo, "oud.py", "nieuw.py", "target")
    assert r.returncode == 1
    assert "niet gevonden" in r.stderr
    assert "IDENTIEK" not in r.stdout


def test_ambigue_toplevel_definities_falen_luid(tmp_path: Path) -> None:
    """Twee definities met dezelfde naam op hetzelfde niveau: geen eerste-pakt-wint."""
    repo = _make_repo(tmp_path, {
        "oud.py": (
            "def foo():\n    return 1\n\n"
            "def foo():\n    return 1\n"
        ),
        "nieuw.py": "def foo():\n    return 1\n",
    })
    r = _run(repo, "oud.py", "nieuw.py", "foo")
    assert r.returncode == 1
    assert "ambigu" in r.stderr


def test_alleen_commentaar_en_inspringing_gewijzigd_blijft_identiek(tmp_path: Path) -> None:
    """AST-equivalentie negeert terecht commentaar, blanko regels en inspringdiepte."""
    repo = _make_repo(tmp_path, {
        "oud.py": (
            "def foo(a):\n"
            "    # deze commentaarregel verdwijnt\n"
            "    return a * 2\n"
        ),
        "nieuw.py": (
            "def wrapper():\n"
            "    if True:\n"
            "        def foo(a):\n"
            "            return a * 2\n\n\n"
            "# los commentaar eromheen\n"
            "def foo(a):\n"
            "    return a * 2\n"
        ),
    })
    r = _run(repo, "oud.py", "nieuw.py", "foo")
    assert r.returncode == 0, r.stderr
    assert "IDENTIEK" in r.stdout


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
