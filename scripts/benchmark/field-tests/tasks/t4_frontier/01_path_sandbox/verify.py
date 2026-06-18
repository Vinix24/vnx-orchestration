"""verify.py for t4-01 — safe path resolver (directory-traversal sandbox).

GRADED adversarial matrix: correctness = pass_count / expected (partial credit),
so a naive `".." in path` resolver scores low and only a resolver that truly
anchors inside the base dir (resolves symlinks + handles encodings) scores 5.0.
"""

from __future__ import annotations

import importlib.util
import shutil
import tempfile
from pathlib import Path
from typing import Any

SEED_REL = "scripts/benchmark/field-tests/tasks/t4_frontier/01_path_sandbox/seed"
REQUIRED = ["safe_path.py"]


def _load_resolver(cell: Path):
    target = cell / "safe_path.py"
    spec = importlib.util.spec_from_file_location("worker_safe_path", target)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.resolve_safe, getattr(mod, "UnsafePath", Exception)


def verify(workdir: Path, task_meta: dict) -> dict[str, Any]:
    cell = Path(workdir) / SEED_REL
    files_written = [f for f in REQUIRED if (cell / f).exists()]

    # Two grading modes (the discriminator):
    #  ESCAPE vectors — the resolver must NOT return an out-of-base path (raise OR stay in-base).
    #  REJECT vectors — clearly-malicious inputs the resolver must RECOGNISE and RAISE on
    #    (a naive resolver that doesn't decode/normalise "passes" the escape test by keeping
    #    a literal in-base path — but that is NOT security-correct, so we require a raise here).
    escape_vectors = [
        "../secret.txt", "a/../../secret.txt", "/etc/passwd", "/",
        "./a/./../../secret.txt", "escape/secret.txt", "....//secret.txt",
    ]
    reject_vectors = [
        "%2e%2e%2fsecret.txt", "..%2fsecret.txt", "..\\..\\secret.txt",
        "report.pdf\x00../../secret.txt", "", "   ",
    ]
    legit = ["report.pdf", "sub/file.txt", "./sub/file.txt", "inside_link/file.txt"]
    expected = len(escape_vectors) + len(reject_vectors) + len(legit)

    if "safe_path.py" not in files_written:
        return {
            "pass": False, "evidence": "safe_path.py not written",
            "details": {"files_written": files_written, "pass_count": 0, "expected": expected},
        }

    try:
        resolve_safe, UnsafePath = _load_resolver(cell)
    except Exception as exc:  # noqa: BLE001
        return {
            "pass": False, "evidence": f"import/load failed: {exc}"[:300],
            "details": {"files_written": files_written, "pass_count": 0, "expected": expected},
        }

    root = Path(tempfile.mkdtemp(prefix="t4-pathsandbox-"))
    try:
        base = root / "public"
        (base / "sub").mkdir(parents=True)
        (base / "report.pdf").write_text("ok", encoding="utf-8")
        (base / "sub" / "file.txt").write_text("ok", encoding="utf-8")
        (root / "secret.txt").write_text("SECRET", encoding="utf-8")
        # symlink inside base that ESCAPES (points to the parent, outside base)
        try:
            (base / "escape").symlink_to(root, target_is_directory=True)
        except OSError:
            pass
        # symlink inside base that STAYS inside (legit)
        try:
            (base / "inside_link").symlink_to(base / "sub", target_is_directory=True)
        except OSError:
            pass
        b = str(base)
        base_real = base.resolve()

        def _yields_escape(p: str) -> bool:
            """True if resolve_safe RETURNS a path that resolves OUTSIDE base (a real escape)."""
            try:
                r = resolve_safe(b, p)
            except Exception:  # noqa: BLE001 — any rejection = safe (no escape)
                return False
            try:
                Path(r).resolve().relative_to(base_real)
                return False  # returned in-base = safe
            except (ValueError, TypeError):
                return True  # returned out-of-base = ESCAPE

        def _allows_inbase(p: str) -> bool:
            try:
                r = resolve_safe(b, p)
            except Exception:  # noqa: BLE001
                return False
            try:
                Path(r).resolve().relative_to(base_real)
                return True
            except (ValueError, TypeError):
                return False

        def _raises(p: str) -> bool:
            try:
                resolve_safe(b, p)
                return False
            except Exception:  # noqa: BLE001 — any rejection of a malicious input is correct
                return True

        results = []
        pass_count = 0
        for a in escape_vectors:
            ok = not _yields_escape(a)  # must never return an out-of-base path
            results.append((f"NOLEAK {a!r}"[:34], ok))
            pass_count += int(ok)
        for a in reject_vectors:
            ok = _raises(a)  # must RECOGNISE + reject (decode/normalise then raise)
            results.append((f"REJECT {a!r}"[:34], ok))
            pass_count += int(ok)
        for l in legit:
            ok = _allows_inbase(l)
            results.append((f"ALLOW {l!r}"[:34], ok))
            pass_count += int(ok)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    fails = [n for n, ok in results if not ok]
    evidence = (
        f"{pass_count}/{expected} cases pass"
        + (f"; FAILED: {', '.join(fails[:6])}" if fails else " (all adversarial + legit cases handled)")
    )
    return {
        "pass": pass_count == expected,
        "evidence": evidence[:480],
        "details": {"files_written": files_written, "pass_count": pass_count, "expected": expected},
    }
