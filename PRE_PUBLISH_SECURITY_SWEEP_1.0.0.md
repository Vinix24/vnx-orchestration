# VNX Orchestration 1.0.0 — Pre-Publish Security Sweep Report

**Date:** 2026-05-30  
**Scope:** Full wheel (`vnx_orchestration-1.0.0-py3-none-any.whl`, 815 files) + repo tip  
**Baseline:** gitleaks clean (0 leaks over 1852 commits), `/Users/vincentvandeth` guard scripts already known  
**Angle:** hardcoded-paths / machine-specific-assumptions / config-env-leaks / supply-chain / code vulnerabilities  

---

## VERDICT: NO-GO

**One blocker prevents an immutable public PyPI publish.** Fix the command-injection vulnerability below, rebuild the wheel, and re-scan before publishing.

The remainder of the package is in reasonable shape: no leaked secrets, no build-time arbitrary code execution, no unsafe deserialization, and no `.env` files in the wheel. The warnings are hygiene issues that should be cleaned up but do not block publication on their own.

---

## BLOCKER (1)

### B-1 — Command Injection via `shell=True` in heartbeat monitor
- **File:** `vnx_orchestration/scripts/heartbeat_ack_monitor.py:489–490`
- **Code:**
  ```python
  cmd = f"tmux list-panes -a -F '#{{session_name}}:#{{window_name}} #{{pane_current_command}}' | grep -i {terminal}"
  result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=1)
  ```
- **Why it matters for a PUBLIC immutable publish:**  
  The `terminal` variable is read directly from the Unix socket server (`_socket_server`, line 752–767) without validation before it reaches `_check_terminal_activity`. A crafted dispatch message (or any process that can write to `$VNX_DATA_DIR/sockets/heartbeat_ack_monitor.sock`) can inject arbitrary shell commands. On a shared host, or if the socket path has loose permissions, this is remote code execution as the user running the monitor.
- **Fix:** Replace `shell=True` with a list argument and `grep` as a separate pipeline stage, or shell-escape `terminal` with `shlex.quote()`:
  ```python
  import shlex
  cmd = (
      f"tmux list-panes -a -F '#{{session_name}}:#{{window_name}} #{{pane_current_command}}' "
      f"| grep -i {shlex.quote(terminal)}"
  )
  result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=1)
  ```
  Better: avoid `shell=True` entirely by piping `tmux` stdout into a Python regex match.

---

## WARNINGS (4)

### W-1 — Machine-specific memory paths baked into shipped SKILL.md docs
- **Files:**
  - `vnx_orchestration/skills/security-engineer/SKILL.md:55`
  - `vnx_orchestration/skills/database-engineer/SKILL.md:37`
  - `vnx_orchestration/skills/architect/SKILL.md:28`
  - `vnx_orchestration/skills/debugger/SKILL.md:43`
  - `vnx_orchestration/skills/intelligence-engineer/SKILL.md:37`
- **Content:** Each references  
  `~/.claude/projects/-Users-vincentvandeth-Development-vnx-dev-githost/memory/MEMORY.md`
- **Why it matters:** These are shipped to every pip install. They expose the author's local directory structure, confuse other users (the path will not exist on their machines), and signal that the skill documentation was not sanitized before packaging.
- **Fix:** Replace the absolute path with a generic placeholder:
  `~/.claude/projects/<project-slug>/memory/MEMORY.md`

### W-2 — `vnx_doctor.sh` ships a hardcoded `/Users/` search pattern
- **File:** `vnx_orchestration/scripts/vnx_doctor.sh:10`
- **Content:** `PATTERN='\.claude/vnx-system|/Users/|\.nvm/versions/node/v[0-9]'`
- **Assessment:** The `/Users/` segment is there by design (the script searches for forbidden local paths in other files). It is **not** a leaked secret. However, it is a hardcoded platform-specific path string in a shipped runtime script.
- **Fix:** Acceptable as-is if documented; ideally the pattern should be generated from `uname` detection or moved to a config file so macOS-specific paths do not fire on Linux installs.

### W-3 — `check_installer_no_template_leak.sh` ships developer username
- **File:** `vnx_orchestration/scripts/check_installer_no_template_leak.sh:26`
- **Content:** `"/Users/vincentvandeth"` appears in the `FORBIDDEN_PATTERNS` array.
- **Assessment:** This is a CI leak-guard script; the string is a **search target**, not a leaked path. It is intentionally hunting for the developer's own path in installer output. Benign by design.
- **Fix:** None required. Consider adding a comment explaining the pattern is a guard, not a leak.

### W-4 — Inline test code writes to `/tmp` in a production module
- **File:** `vnx_orchestration/scripts/pr_queue_manager.py` (lines ~1993–2031)
- **Content:** Dead `if __name__ == "__main__":` test blocks that write `/tmp/test_valid_plan.md`, `/tmp/test_no_feature.md`, etc.
- **Why it matters:** Test code should not ship in production wheels. It clutters the attack surface and writes predictable filenames in `/tmp` (race-condition risk, though low severity here).
- **Fix:** Move the tests to a `tests/` directory (already excluded from the wheel via `pyproject.toml`) or delete the inline test blocks.

---

## INFO / CLEAN AREAS (Explicitly checked)

| Check | Result | Evidence |
|-------|--------|----------|
| `.env` / `.envrc` files in wheel | **NONE FOUND** | `find` across wheel returned 0 hits |
| `eval()` / `exec()` calls in Python | **NONE FOUND** | `grep` for `\beval(` and `\bexec(` returned empty |
| `yaml.load` (unsafe) | **NONE** | All YAML parsing uses `yaml.safe_load` |
| `pickle.load` / `pickle.loads` | **NONE** | Not present in wheel |
| `os.system` / `os.popen` | **NONE** | Not present in Python files |
| Secrets (API keys, tokens, passwords) in configs | **NONE** | Configs only reference env-var *names* (`ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, etc.); no values |
| Build hooks / custom `setup.py` | **NONE** | `pyproject.toml` uses standard `setuptools.build_meta`; no `setup.py`, `build.py`, or post-install scripts in wheel |
| `.pth` / `egg-link` files | **NONE** | Not present in wheel |
| Console script entry points | **1 only** | `vnx = vnx_cli.main:main` — standard argparse CLI |
| `requests` library usage | **NONE** | Only `urllib.request` used (Vertex AI, Ollama local) |
| SSRF via user-controlled URLs | **LOW RISK** | `vertex_ai_runner.py` builds URL from gcloud project ID; Ollama adapters default to `localhost:11434` via env override |
| SQL injection (SQLite) | **LOW RISK** | A few f-strings in `PRAGMA` / `CREATE TABLE` / `DROP TABLE`, but table/alias names are internally generated, not user-facing |
| Path traversal from dispatch IDs | **MITIGATED** | `tmux_worktree.py` validates dispatch IDs with `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$` |
| Unsafe temp files | **MOSTLY CLEAN** | `heartbeat_ack_monitor.py` uses `NamedTemporaryFile(delete=False)` and correctly `os.unlink`s; `intelligence_daemon.py` uses atomic rename after temp write |
| `subprocess` with `shell=True` (Python) | **1 ONLY** | The blocker above (B-1). `tmux_worktree.py` correctly uses list args with `shell=False` |
| `vincentvandeth` in wheel | **3 hits** | `METADATA` (author email, expected), `check_installer_no_template_leak.sh` (guard), 5× `SKILL.md` (W-1) |
| `install.sh` in wheel | **NOT PRESENT** | Only referenced in comments/docs |

---

## SUPPLY-CHAIN ASSESSMENT

- **Build backend:** `setuptools.build_meta` (standard, no custom hooks).
- **Dependencies:** `watchdog`, `opentelemetry-*`, `jinja2` — all well-known, no pinned malicious versions.
- **Optional deps:** `vulture`, `ruff` — dev-quality tools, harmless.
- **No `setup.py` / `build.py` / `MANIFEST.in` / post-install scripts** in the wheel.
- **No code execution on `pip install`** beyond normal setuptools file extraction.
- **Entry point:** `vnx_cli.main:main` is a plain argparse CLI; no import-time side effects in `vnx_cli/__init__.py` other than reading `VERSION`.

**Supply-chain verdict:** Clean.

---

## SUMMARY TABLE

| ID | Severity | File | Line | Issue | Fix |
|----|----------|------|------|-------|-----|
| B-1 | **BLOCKER** | `scripts/heartbeat_ack_monitor.py` | 489–490 | `shell=True` with unsanitized `terminal` from socket JSON → command injection | Use `shlex.quote(terminal)` or replace `shell=True` with list args + Python regex |
| W-1 | WARN | `skills/*/SKILL.md` (5 files) | — | Hardcoded `~/.claude/projects/-Users-vincentvandeth-...` path in docs | Replace with `<project-slug>` placeholder |
| W-2 | WARN | `scripts/vnx_doctor.sh` | 10 | Ships `/Users/` as a search pattern | Document or make platform-conditional |
| W-3 | WARN | `scripts/check_installer_no_template_leak.sh` | 26 | Ships developer username as forbidden pattern | Add explanatory comment |
| W-4 | WARN | `scripts/pr_queue_manager.py` | ~1993–2031 | Inline test code writes to `/tmp` in production module | Move tests out of shipped code |

---

## RECOMMENDATION

1. **Fix B-1** (command injection) immediately.
2. **Rebuild the wheel** (`python -m build`).
3. **Re-run this sweep** on the new wheel to confirm the fix and verify no new artifacts appeared.
4. **Address W-1** (SKILL.md paths) before publish — it looks unprofessional and leaks local structure.
5. **Address W-4** (inline tests) in a follow-up PR — not blocking but poor hygiene.
6. **After fixes:** Publish. The package is otherwise ready for an immutable public release.
