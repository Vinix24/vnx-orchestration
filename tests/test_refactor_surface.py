"""Tests voor scripts/refactor_surface.py (dispatch 20260802-f0-refactor-proof-tools).

Elke test moet ook rood KUNNEN staan (OI-893): de faalgevallen zijn expliciet getoetst
tegen de ongefixte bronversie uit claudedocs/refactor-tools/ — zie het dispatch-rapport
voor de exit-codes van die rood-runs.

Fixture-modules staan in tmp_path en worden via --lib-dir op sys.path gezet. Er wordt
geen gedeelde --basetemp geforceerd.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "scripts" / "refactor_surface.py"


def _write_pair(lib: Path, original: str, new: str) -> None:
    lib.mkdir(parents=True, exist_ok=True)
    (lib / "original_mod.py").write_text(original, encoding="utf-8")
    (lib / "new_mod.py").write_text(new, encoding="utf-8")


def _run(lib_dir: str | None, tmp_path: Path, cwd: Path | None = None) -> subprocess.CompletedProcess:
    args = [
        sys.executable, str(TOOL),
        "--original", "original_mod.py",
        "--new", "new_mod.py",
        "--names", "foo",
    ]
    if lib_dir is not None:
        args += ["--lib-dir", lib_dir]
    return subprocess.run(
        args, cwd=cwd or tmp_path, capture_output=True, text=True
    )


def test_geldig_reexport_paar_is_ok(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    _write_pair(
        lib,
        original="from new_mod import foo\n",
        new="def foo():\n    return 1\n",
    )
    r = _run(str(lib), tmp_path)
    assert r.returncode == 0, r.stderr
    assert "OK          foo" in r.stdout
    assert "1/1 namen bewezen intact" in r.stdout


def test_vergeten_reexport_in_originele_module_faalt(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    _write_pair(
        lib,
        original="# geen re-export: elke bestaande importeur breekt\n",
        new="def foo():\n    return 1\n",
    )
    r = _run(str(lib), tmp_path)
    assert r.returncode == 1
    assert "ONTBREEKT in de originele module" in r.stdout


def test_naam_ontbreekt_in_nieuwe_module_faalt(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    _write_pair(
        lib,
        original="def foo():\n    return 1\n",
        new="# naam is hier niet geland\n",
    )
    r = _run(str(lib), tmp_path)
    assert r.returncode == 1
    assert "ONTBREEKT in de nieuwe module" in r.stdout


def test_zelfde_naam_ander_object_faalt(tmp_path: Path) -> None:
    """Twee onafhankelijke defs met dezelfde naam: glipt door een hasattr-check heen."""
    lib = tmp_path / "lib"
    _write_pair(
        lib,
        original="def foo():\n    return 'eigen definitie in origineel'\n",
        new="def foo():\n    return 'onafhankelijke definitie in nieuw'\n",
    )
    r = _run(str(lib), tmp_path)
    assert r.returncode == 1
    assert "ANDER object" in r.stdout


def test_niet_importeerbare_module_faalt_met_zichtbare_importfout(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    _write_pair(
        lib,
        original="from new_mod import foo\n",
        new="import module_die_niet_bestaat_abc123\n",
    )
    r = _run(str(lib), tmp_path)
    assert r.returncode == 1
    assert "niet importeerbaar" in r.stderr
    assert "module_die_niet_bestaat_abc123" in r.stderr


def test_lib_dir_default_werkt_vanuit_andere_cwd(tmp_path: Path) -> None:
    """Regressie op mankement 3: de tool moet cwd-onafhankelijk kunnen draaien.

    De cwd-onafhankelijkheid komt van de module-level bootstrap in
    refactor_surface.py (die <script>/../lib vanuit de eigen ligging op sys.path
    zet) — dat is het enige mechanisme, er is geen tweede default in main().
    Deze test draait het script in een subprocess vanuit een lege tmp-cwd ZONDER
    --lib-dir tegen een echt re-export-paar in de repo (data_dir_guard
    re-exporteert resolve_project_id uit project_root). PYTHONPATH wordt uit de
    omgeving gescrubd zodat alleen de bootstrap de modules kan leveren: valt
    de bootstrap weg of wordt hij cwd-relatief, dan is scripts/lib onvindbaar
    en gaat deze test rood.
    """
    vreemde_cwd = tmp_path / "ergens-anders"
    vreemde_cwd.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    args = [
        sys.executable, str(TOOL),
        "--original", "data_dir_guard.py",
        "--new", "project_root.py",
        "--names", "resolve_project_id",
    ]
    r = subprocess.run(args, cwd=vreemde_cwd, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "OK          resolve_project_id" in r.stdout


def test_geen_dode_repo_constante() -> None:
    """Regressie op mankement 2: REPO was gedefinieerd en nooit gebruikt."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("refactor_surface_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert not hasattr(module, "REPO"), "dode module-level constante REPO is terug"


def test_expliciete_lib_dir_blijft_leidend(tmp_path: Path) -> None:
    """Een expliciet meegegeven --lib-dir gaat vóór op de project-root-default."""
    lib = tmp_path / "eigen-lib"
    _write_pair(
        lib,
        original="from new_mod import foo\n",
        new="def foo():\n    return 1\n",
    )
    r = _run(str(lib), tmp_path)
    assert r.returncode == 0, r.stderr
    assert "OK          foo" in r.stdout


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
