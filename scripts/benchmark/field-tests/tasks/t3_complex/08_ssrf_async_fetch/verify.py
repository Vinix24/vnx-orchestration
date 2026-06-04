"""verify.py for task 08 — URLPolicy SSRF validator."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


SEED_REL = "scripts/benchmark/field-tests/tasks/t3_complex/08_ssrf_async_fetch/seed"
REQUIRED = ["url_policy.py", "tests/test_url_policy.py"]


def verify(workdir: Path, task_meta: dict) -> dict[str, Any]:
    seed_dir = workdir / SEED_REL
    files_written = [f for f in REQUIRED if (seed_dir / f).exists()]
    missing = [f for f in REQUIRED if f not in files_written]
    if missing:
        return {
            "pass": False,
            "evidence": f"worker did not write: {missing}",
            "details": {"pass_count": 0, "expected": 3, "files_written": files_written},
        }

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for rel in REQUIRED:
            src = seed_dir / rel
            dst = tmp_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, dst)
        (tmp_dir / "tests" / "__init__.py").touch()

        checks: list[tuple[str, bool, str]] = []

        pytest_proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_url_policy.py",
             "-v", "--tb=short", "-q"],
            cwd=tmp_dir, capture_output=True, text=True, timeout=120, check=False,
        )
        pytest_out = pytest_proc.stdout + "\n" + pytest_proc.stderr
        pass_match = re.search(r"(\d+) passed", pytest_out)
        fail_match = re.search(r"(\d+) failed", pytest_out)
        pytest_pass = int(pass_match.group(1)) if pass_match else 0
        pytest_fail = int(fail_match.group(1)) if fail_match else 0
        checks.append((
            "13 tests pass (10 adversarial + 3 positive)",
            pytest_pass >= 13 and pytest_fail == 0,
            f"passed={pytest_pass} failed={pytest_fail}",
        ))

        adversarial_smoke = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, '.'); "
             "from url_policy import URLPolicy, URLPolicyViolation; "
             "p = URLPolicy(); raised = 0; total = 0\n"
             "urls = ['file:///etc/passwd', 'http://169.254.169.254/', "
             "'http://127.0.0.1/', 'http://localhost/', 'javascript:alert(1)']\n"
             "for u in urls:\n"
             "  total += 1\n"
             "  try: p.validate(u)\n"
             "  except URLPolicyViolation: raised += 1\n"
             "  except Exception: pass\n"
             "print(f'{raised}/{total}')"],
            cwd=tmp_dir, capture_output=True, text=True, timeout=30, check=False,
        )
        adversarial_ok = (
            adversarial_smoke.returncode == 0
            and adversarial_smoke.stdout.strip().endswith("5/5")
        )
        checks.append((
            "5 sentinel adversarial URLs all rejected",
            adversarial_ok,
            f"stdout={adversarial_smoke.stdout[-200:]}",
        ))

        public_smoke = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, '.'); "
             "from url_policy import URLPolicy, URLPolicyViolation; "
             "p = URLPolicy(); ok = 0\n"
             "for u in ['https://example.com/', 'https://api.example.com/v1']:\n"
             "  try: p.validate(u); ok += 1\n"
             "  except URLPolicyViolation: pass\n"
             "print(f'{ok}/2')"],
            cwd=tmp_dir, capture_output=True, text=True, timeout=30, check=False,
        )
        public_ok = (
            public_smoke.returncode == 0
            and public_smoke.stdout.strip().endswith("2/2")
        )
        checks.append((
            "public URLs not blocked",
            public_ok,
            f"stdout={public_smoke.stdout[-200:]}",
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
    print(json.dumps(verify(Path.cwd(), {"tier": "t3_complex"}), indent=2))
