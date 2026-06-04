"""verify.py for task 05 — EmailLinkExtractor subclass."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


SEED_REL = "scripts/benchmark/field-tests/tasks/t2_medium/05_extractor_subclass/seed"


def verify(workdir: Path, task_meta: dict) -> dict[str, Any]:
    seed_dir = workdir / SEED_REL
    extractor_file = seed_dir / "email_link_extractor.py"
    test_file = seed_dir / "tests" / "test_email_link_extractor.py"

    files_written = []
    for rel in ("email_link_extractor.py", "tests/test_email_link_extractor.py"):
        if (seed_dir / rel).exists():
            files_written.append(rel)

    missing = [f for f in ("email_link_extractor.py", "tests/test_email_link_extractor.py")
               if f not in files_written]
    if missing:
        return {
            "pass": False,
            "evidence": f"worker did not write: {missing}",
            "details": {"pass_count": 0, "expected": 3, "files_written": files_written},
        }

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for src_rel in (
            "base_extractor.py", "meta_tag_extractor.py",
            "email_link_extractor.py", "tests/test_email_link_extractor.py",
        ):
            src = seed_dir / src_rel
            if src.exists():
                dst = tmp_dir / src_rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(src, dst)
        (tmp_dir / "tests" / "__init__.py").touch()

        checks: list[tuple[str, bool, str]] = []

        envelope_proc = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, '.'); "
             "from email_link_extractor import EmailLinkExtractor; "
             "r = EmailLinkExtractor().extract('', 'https://example.com'); "
             "import json; print(json.dumps(r))"],
            cwd=tmp_dir, capture_output=True, text=True, timeout=30, check=False,
        )
        envelope_ok = False
        if envelope_proc.returncode == 0:
            try:
                env = json.loads(envelope_proc.stdout.strip())
                envelope_ok = (
                    env.get("name") == "email_link"
                    and isinstance(env.get("data"), dict)
                    and isinstance(env.get("errors"), list)
                )
            except json.JSONDecodeError:
                pass
        checks.append((
            "empty-html envelope shape correct",
            envelope_ok,
            f"stdout={envelope_proc.stdout[-200:]} stderr={envelope_proc.stderr[-200:]}",
        ))

        pytest_proc = subprocess.run(
            [sys.executable, "-m", "pytest",
             "tests/test_email_link_extractor.py", "-v", "--tb=short", "-q"],
            cwd=tmp_dir, capture_output=True, text=True, timeout=60, check=False,
        )
        pytest_out = pytest_proc.stdout + "\n" + pytest_proc.stderr
        pass_match = re.search(r"(\d+) passed", pytest_out)
        fail_match = re.search(r"(\d+) failed", pytest_out)
        pytest_pass = int(pass_match.group(1)) if pass_match else 0
        pytest_fail = int(fail_match.group(1)) if fail_match else 0
        checks.append((
            "5 tests pass",
            pytest_pass >= 5 and pytest_fail == 0,
            f"passed={pytest_pass} failed={pytest_fail}",
        ))

        sample_proc = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, '.'); "
             "from email_link_extractor import EmailLinkExtractor; "
             'html = \'<a href="mailto:A@Example.com">x</a>'
             '<a href="mailto:a@example.com?subject=Hi">y</a>'
             '<a href="mailto:b@example.com">z</a>\'; '
             "r = EmailLinkExtractor().extract(html, 'https://x'); "
             "import json; print(json.dumps(r['data']))"],
            cwd=tmp_dir, capture_output=True, text=True, timeout=30, check=False,
        )
        smoke_ok = False
        if sample_proc.returncode == 0:
            try:
                d = json.loads(sample_proc.stdout.strip())
                smoke_ok = (
                    d.get("total_links") == 3
                    and d.get("unique_emails") == 2
                    and sorted(d.get("emails", [])) == ["a@example.com", "b@example.com"]
                )
            except json.JSONDecodeError:
                pass
        checks.append((
            "dedup + lowercase + subject-strip work",
            smoke_ok,
            f"stdout={sample_proc.stdout[-300:]}",
        ))

    pass_count = sum(1 for _, ok, _ in checks if ok)

    return {
        "pass": pass_count >= 3,
        "evidence": "; ".join(
            f"{'PASS' if ok else 'FAIL'} {name}" for name, ok, _ in checks
        ),
        "details": {
            "pass_count": min(pass_count, 3),
            "expected": 3,
            "files_written": files_written,
            "checks": [{"name": n, "ok": ok, "note": note} for n, ok, note in checks],
        },
    }


if __name__ == "__main__":
    print(json.dumps(verify(Path.cwd(), {"tier": "t2_medium"}), indent=2))
