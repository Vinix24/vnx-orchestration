# install-central.sh Operator Runbook

Central VNX install for operators running multiple projects from a shared system install.
Distinct from `install.sh` (per-project embedded install for open-source users).

## When to use which installer

| Scenario | Script |
|---|---|
| Open-source user, one project, self-contained | `install.sh` |
| Operator with multiple projects, shared binary, project pinning | `install-central.sh` |
| CI/CD environment needing reproducible version pin | `install-central.sh --version <pin>` |

`install.sh` copies VNX into `.vnx/` inside the project. `install-central.sh` installs VNX once to `~/.vnx-system/versions/<version>/` and exposes a shim (`~/.vnx-system/bin/vnx`) that reads `.vnx-version` from the project root.

## Two-path model: VNX_HOME vs PROJECT_ROOT

This is the most important concept for central install operators to understand.

| Variable | Central install | Embedded install |
|---|---|---|
| `VNX_HOME` | `~/.vnx-system/versions/<ver>/` — read-only code | `<project>/.vnx/` — bundled with project |
| `PROJECT_ROOT` | CWD's git root (detected per-invocation) | Parent of `.vnx/` |
| `VNX_DATA_DIR` | `<project>/.vnx-data/` | `<project>/.vnx-data/` |
| `VNX_CANONICAL_ROOT` | Project's git root | Project's git root |

**Hard rule**: `VNX_HOME` is read-only code. No script may write files under `VNX_HOME`. All runtime state goes under `PROJECT_ROOT/.vnx-data/`.

Detection hierarchy (highest priority first):

1. Embedded layout: `VNX_HOME` path contains `/.vnx/` → embedded mode
2. `VNX_PROJECT_ROOT` env var exported by shim → overrides detection
3. `.vnx-install-mode` marker file in `VNX_HOME` containing "central"
4. Git heuristic: CWD git root differs from `VNX_HOME` → central mode

This means running `vnx` from inside `~/.vnx-system/...` is a misuse and will trigger a guard error. Always run `vnx` from your project directory.

## Pre-flight checklist

Before running `install-central.sh`:

- [ ] Python >= 3.11 and < 3.14 available: `python3 --version`
- [ ] git available: `git --version`
- [ ] sqlite3 available: `sqlite3 --version`
- [ ] At least 500MB free disk in target parent: `df -h ~`
- [ ] Network access to `github.com` (for clone) unless `--source` points to a local mirror
- [ ] Target directory (`~/.vnx-system` by default) is writable
- [ ] Check for pre-fix contamination (important for upgrades from rc3/rc4):

```bash
ls ~/.vnx-system/versions/*/.vnx-data/ 2>/dev/null && echo "CONTAMINATION FOUND" || echo "clean"
```

If contamination found, see [Pre-fix contamination cleanup](#pre-fix-contamination-cleanup) before proceeding.

Run `--dry-run` first to validate without side effects:

```bash
bash install-central.sh --dry-run
```

## Installation

```bash
# Default: latest stable to ~/.vnx-system
bash install-central.sh

# Pin a specific version
bash install-central.sh --version v1.0.0-rc5

# Custom install root
bash install-central.sh --target /opt/vnx-system --version v1.0.0-rc5

# Internal mirror
bash install-central.sh --source https://internal.git/vnx-orchestration --version v1.0.0-rc5
```

After install, add the shim to PATH:

```bash
export PATH="${PATH}:${HOME}/.vnx-system/bin"
```

Add to `~/.zshrc` or `~/.bashrc` to persist.

## Project pinning via .vnx-version

Each project can pin a specific installed version:

```bash
echo 'v1.0.0-rc5' > /path/to/project/.vnx-version
```

The shim reads `.vnx-version` by traversing up from `cwd`. When no pin is found, it falls back to `~/.vnx-system/current` (the last installed version).

Installing a new version does NOT break pinned projects. Old versions remain at `~/.vnx-system/versions/`.

## Per-project cutover procedure

After installing a new version, each project needs a switchover. Two scenarios:

- **Version upgrade** (already on central install): update `.vnx-version` pin → verify doctor.
- **Embedded-to-central migration**: stop daemons → backup embedded install → set pin → init → verify doctor.

### Step 1: Pre-switchover checks

```bash
# Verify the new version is installed
ls ~/.vnx-system/versions/

# Confirm install-mode marker is present (rc5+ only)
cat ~/.vnx-system/versions/v1.0.0-rc5/.vnx-install-mode  # must print "central"

# Confirm no .vnx-data contamination in the new version dir
ls ~/.vnx-system/versions/v1.0.0-rc5/.vnx-data 2>/dev/null && echo "FAIL: contaminated" || echo "clean"

# Confirm shim is on PATH
which vnx  # must show ~/.vnx-system/bin/vnx
```

### Step 2: Update project pin

```bash
cd /path/to/your-project

# Pin to new version
echo 'v1.0.0-rc5' > .vnx-version

# Verify shim picks up the new version
vnx --version
```

### Step 3: Verify doctor PASS

Run `vnx doctor` from the project directory and confirm all checks pass:

```bash
cd /path/to/your-project
vnx doctor
```

Expected output after successful cutover (rc5 with Wave 4 fixes applied):

```
  [PASS] tool: Required: bash
  [PASS] tool: Required: python3
  [PASS] path: Runtime root: /path/to/your-project
  [PASS] path: Canonical root: /path/to/your-project
  [PASS] dir: VNX config: /path/to/your-project/.vnx
  [PASS] dir: Runtime data: /path/to/your-project/.vnx-data
  [PASS] file: Config: /path/to/your-project/.vnx/config.yml
  [PASS] settings: Valid JSON
  [PASS] hooks: SessionStart hook present
─────────────────────────────────────────────
PASSED — N checks OK
```

**Wave 4 context**: Prior to rc5, four doctor checks consistently failed in central-install mode — `path: Runtime root`, `path: Canonical root`, `file: Config`, and `settings: Valid JSON` — because PROJECT_ROOT resolved to VNX_HOME instead of the project directory. PR-WAVE4-1 through PR-WAVE4-4 fix the root cause. If you still see these failures after cutover to rc5, the shim may not have been reinstalled — re-run `install-central.sh --version v1.0.0-rc5` and try again.

If you see any FAIL mentioning `~/.vnx-system/` paths, the PROJECT_ROOT resolver is pointing at VNX_HOME instead of your project. See [Troubleshooting](#troubleshooting).

### Step 4: Re-initialize if needed

If the project has never been initialized with the new version, or if `.vnx/` is missing:

```bash
cd /path/to/your-project
vnx init
vnx regen-settings --full
vnx bootstrap-hooks
vnx doctor  # re-verify
```

### Step 5: Confirm runtime isolation

Verify that runtime data goes to the project, not to VNX_HOME:

```bash
# After running any vnx command:
ls /path/to/your-project/.vnx-data/   # should exist
ls ~/.vnx-system/versions/v1.0.0-rc5/.vnx-data/ 2>/dev/null && echo "FAIL: leaked" || echo "ok"
```

## Embedded-to-central migration

Projects that started with an embedded VNX install (`.vnx/` directory inside the project, or `.claude/vnx-system/` for some layouts) require additional steps before the version-pin cutover.

### Layout A: `.vnx/` embedded (SEOcrawler pattern)

The embedded `.vnx/` directory contains the full VNX code tree (scripts, skills, templates). After migration, it will be replaced by a minimal `config.yml` only — the code lives in `~/.vnx-system/versions/<ver>/`.

```bash
cd /path/to/your-project

# 1. Stop daemons gracefully (kill PID files)
for pid_file in .vnx-data/pids/*.pid; do
  pid=$(cat "$pid_file" 2>/dev/null) && [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
done
sleep 2

# 2. Backup the embedded install
cp -r .vnx .vnx.backup-$(date +%Y%m%d)-pre-central
echo "backup: .vnx.backup-$(date +%Y%m%d)-pre-central"

# 3. Remove embedded code tree (keep the project's .vnx-data)
rm -rf .vnx

# 4. Ensure shim is on PATH
export PATH="${HOME}/.vnx-system/bin:${PATH}"
which vnx  # must show ~/.vnx-system/bin/vnx

# 5. Pin to rc5
echo 'v1.0.0-rc5' > .vnx-version

# 6. Init — creates minimal .vnx/config.yml in project dir
vnx init

# 7. Generate settings and hooks in project
vnx regen-settings --full
vnx bootstrap-hooks

# 8. Verify
vnx doctor  # all checks must PASS
ls .vnx/config.yml         # minimal config in project
ls ~/.vnx-system/versions/v1.0.0-rc5/.vnx-data 2>/dev/null && echo "FAIL: leaked" || echo "isolated"
```

**Rollback — Layout A:**

```bash
cd /path/to/your-project

# Stop any daemons started under central install
for pid_file in .vnx-data/pids/*.pid; do
  pid=$(cat "$pid_file" 2>/dev/null) && [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
done

# Restore embedded install
rm -rf .vnx
cp -r .vnx.backup-YYYYMMDD-pre-central .vnx

# Remove version pin (shim falls back to current or won't be invoked)
rm -f .vnx-version

# Verify embedded doctor still passes
bash .vnx/bin/vnx doctor
```

### Layout B: `.claude/vnx-system/` embedded (MC pattern)

Some projects store VNX at `.claude/vnx-system/` and reference it via env vars like `VNX_CANONICAL_ROOT`. The cleanest cutover is a symlink replacement, which preserves all existing script path references.

```bash
cd /path/to/your-project

# 1. Stop daemons
for pid_file in .vnx-data/pids/*.pid; do
  pid=$(cat "$pid_file" 2>/dev/null) && [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
done
sleep 2

# 2. Backup the embedded vnx-system directory
mv .claude/vnx-system .claude/vnx-system.backup-$(date +%Y%m%d)-pre-central
echo "backup: .claude/vnx-system.backup-$(date +%Y%m%d)-pre-central"

# 3. Create symlink pointing at central install
ln -sfn ~/.vnx-system/current .claude/vnx-system
ls -la .claude/vnx-system  # must show symlink to ~/.vnx-system/current

# 4. Ensure shim is on PATH
export PATH="${HOME}/.vnx-system/bin:${PATH}"

# 5. Pin to rc5
echo 'v1.0.0-rc5' > .vnx-version

# 6. Init — creates minimal .vnx/config.yml in project dir
vnx init

# 7. Generate settings and hooks
vnx regen-settings --full
vnx bootstrap-hooks

# 8. Verify
vnx doctor  # all checks must PASS
ls -la .claude/vnx-system  # symlink intact
ls ~/.vnx-system/versions/v1.0.0-rc5/.vnx-data 2>/dev/null && echo "FAIL: leaked" || echo "isolated"
```

**Rollback — Layout B:**

```bash
cd /path/to/your-project

# Remove symlink
rm .claude/vnx-system

# Restore backup
mv .claude/vnx-system.backup-YYYYMMDD-pre-central .claude/vnx-system

# Remove version pin
rm -f .vnx-version

# Verify embedded scripts still run
ls .claude/vnx-system/scripts/dispatcher_v8_minimal.sh
```

### After migration: start daemons

Start daemons after doctor PASS is confirmed — use the central install's script path:

```bash
cd /path/to/your-project

# Start dispatcher
nohup bash "$(vnx --print-vnx-home)/scripts/dispatcher_v8_minimal.sh" \
  > .vnx-data/logs/dispatcher.log 2>&1 &

# Start receipt processor
nohup bash "$(vnx --print-vnx-home)/scripts/receipt_processor_v4.sh" \
  > .vnx-data/logs/receipt_processor.log 2>&1 &

# Verify daemons running
ps aux | grep -E "dispatcher|receipt_processor" | grep -v grep
```

> If your project has a `vnx start` command or `bin/vnx start`, use that instead of direct script calls.

## Upgrading

```bash
# Install new version (keeps old versions)
bash install-central.sh --version v1.0.1

# Update project pin
echo 'v1.0.1' > /path/to/project/.vnx-version
```

The `current` symlink points to the newly installed version after each successful run.

## Rollback procedure

### System-level rollback (all projects)

Re-point the `current` symlink to a previous version:

```bash
# List installed versions
ls ~/.vnx-system/versions/

# Re-point current to a previous version
ln -sfn ~/.vnx-system/versions/v1.0.0-rc3 ~/.vnx-system/current
```

Or re-run the installer with the previous version:

```bash
bash install-central.sh --version v1.0.0-rc3
```

Projects without a `.vnx-version` pin will automatically use the rolled-back `current`.

### Per-project rollback

If only one project has issues after cutover, roll back its pin without touching other projects:

```bash
cd /path/to/your-project

# Revert to previous version pin
echo 'v1.0.0-rc4' > .vnx-version

# Verify doctor passes on the old version
vnx doctor
```

This leaves all other projects on their current pins unaffected.

### Emergency unblock (without new install)

If you need to unblock a project immediately and can't wait for a code fix:

```bash
# Manually write the install-mode marker (triggers central detection)
echo "central" > ~/.vnx-system/versions/v1.0.0-rc4/.vnx-install-mode

# Then run doctor from project dir — should detect project root correctly
cd /path/to/your-project && vnx doctor
```

This works on rc4 installs that predate the automatic marker write in `clone_version()`.

## Pre-fix contamination cleanup

If you ran `vnx init` before rc5 (before the path resolver fix), runtime data may have been written to `~/.vnx-system/versions/rc4/.vnx-data/` instead of your project.

The same applies to `settings.json`: if `regen-settings` ran before rc5, it may have written `.claude/settings.json` to `~/.vnx-system/versions/rc4/.claude/settings.json` instead of your project's `.claude/` directory.

**Check all contamination targets:**

```bash
VERSION_DIR=~/.vnx-system/versions/v1.0.0-rc4  # adjust to your version

echo "=== Checking for pre-fix contamination ==="
ls "$VERSION_DIR/.vnx-data" 2>/dev/null && echo "[!] .vnx-data contamination found" || echo "[ok] .vnx-data clean"
ls "$VERSION_DIR/.claude"   2>/dev/null && echo "[!] .claude contamination found"   || echo "[ok] .claude clean"
ls "$VERSION_DIR/.vnx-intelligence" 2>/dev/null && echo "[!] .vnx-intelligence contamination found" || echo "[ok] .vnx-intelligence clean"
```

**Clean:**

```bash
VERSION_DIR=~/.vnx-system/versions/v1.0.0-rc4  # adjust to your version
BACKUP="/tmp/central-vnx-contamination-backup-$(date +%Y%m%d)"
mkdir -p "$BACKUP"

# Back up contaminated data before removing
[ -d "$VERSION_DIR/.vnx-data" ]        && cp -r "$VERSION_DIR/.vnx-data"        "$BACKUP/.vnx-data"
[ -d "$VERSION_DIR/.claude" ]           && cp -r "$VERSION_DIR/.claude"           "$BACKUP/.claude"
[ -d "$VERSION_DIR/.vnx-intelligence" ] && cp -r "$VERSION_DIR/.vnx-intelligence" "$BACKUP/.vnx-intelligence"
echo "backup: $BACKUP"

# Remove contamination
rm -rf "$VERSION_DIR/.vnx-data"
rm -rf "$VERSION_DIR/.claude"
rm -rf "$VERSION_DIR/.vnx-intelligence"

# Verify clean
ls "$VERSION_DIR/.vnx-data" 2>/dev/null && echo "FAIL: .vnx-data still contaminated" || echo "clean: .vnx-data"
ls "$VERSION_DIR/.claude"   2>/dev/null && echo "FAIL: .claude still contaminated"   || echo "clean: .claude"
```

After cleanup, run `vnx init` and `vnx regen-settings --full` from each project directory to initialize clean runtime state in the correct location.

**If you need receipts from the contaminated `.vnx-data/`**: they are backed up at `$BACKUP/.vnx-data/receipts/`. You can copy them to your project's `.vnx-data/receipts/` directory for historical record.

## Troubleshooting

### Doctor FAIL: Runtime root missing or points to VNX_HOME

**Symptom:**

```
[FAIL] path: Runtime root missing: /Users/you/.vnx-system/versions/v1.0.0-rc4
[FAIL] dir: Missing: VNX config (/Users/you/.vnx-system/versions/v1.0.0-rc4/.vnx)
[FAIL] file: Missing config: /Users/you/.vnx-system/versions/v1.0.0-rc4/.vnx/config.yml
        Fix: Run: vnx init
```

**Cause:** PROJECT_ROOT resolver is using VNX_HOME as the project root. This means either (a) the `.vnx-install-mode` marker is missing from the version dir, or (b) you're running `vnx` from inside `~/.vnx-system/...` instead of from your project directory.

**Fix:**

```bash
# Check the marker
cat ~/.vnx-system/versions/v1.0.0-rc5/.vnx-install-mode  # must print "central"

# If missing: write it manually (rc4 compatibility)
echo "central" > ~/.vnx-system/versions/v1.0.0-rc4/.vnx-install-mode

# Then run from your project dir
cd /path/to/your-project && vnx doctor
```

If the marker is present but doctor still fails, re-run `install-central.sh` to reinstall the shim (which exports `VNX_PROJECT_ROOT`):

```bash
bash install-central.sh --version v1.0.0-rc5
```

### Doctor FAIL: Missing .claude/settings.json

**Symptom:**

```
[FAIL] settings: Missing .claude/settings.json
        Fix: Run: vnx regen-settings --full
```

**Cause:** Either the project was never initialized, or a previous run wrote settings to VNX_HOME (contamination).

**Fix:**

```bash
cd /path/to/your-project

# Check if settings landed in wrong place
ls ~/.vnx-system/versions/v1.0.0-rc4/.claude/settings.json 2>/dev/null && echo "CONTAMINATION"

# Clean up contamination if found
rm -rf ~/.vnx-system/versions/v1.0.0-rc4/.claude

# Generate settings in correct location
vnx regen-settings --full
ls .claude/settings.json  # should now exist
```

### Doctor FAIL: Missing hook

**Symptom:**

```
[FAIL] hooks: Missing hook: /Users/you/.vnx-system/.../hooks/sessionstart.sh
        Fix: Run: vnx bootstrap-hooks
```

**Cause:** Same as settings — resolver pointed at VNX_HOME. After fixing the marker, hooks need to be regenerated in the project.

**Fix:**

```bash
cd /path/to/your-project && vnx bootstrap-hooks && vnx doctor
```

### regen-settings ABORT: PROJECT_ROOT equals VNX_HOME

**Symptom:**

```
[regen-settings] ABORT: PROJECT_ROOT equals VNX_HOME (/Users/you/.vnx-system/versions/...)
[regen-settings] Run from your project directory or re-run install-central.sh
```

**Cause:** You're running `vnx regen-settings` from inside `VNX_HOME` or the resolver mis-detected. This is a safety guard added in PR-4.

**Fix:**

```bash
cd /path/to/your-project && vnx regen-settings --merge
```

### PROJECT_ROOT mis-detection after shell env contamination

If you previously exported `VNX_PROJECT_ROOT` in your shell and it persists:

```bash
# Clear stale env var
unset VNX_PROJECT_ROOT

# The shim re-detects on each invocation and will set it correctly
cd /path/to/your-project && vnx doctor
```

### `git clone failed`

- Verify network access to the source URL
- Check the version tag exists: `git ls-remote --tags https://github.com/Vinix24/vnx-orchestration`
- Use `--source` to point to a local clone: `--source /path/to/local/vnx-orchestration`

### `Pinned version X not installed`

The shim found `.vnx-version` but that version is not in `~/.vnx-system/versions/`. Run:

```bash
bash install-central.sh --version <pin>
```

### `Insufficient disk space`

Free at least 500MB in the target parent directory. Each version install is roughly 50-100MB.

### `Python version check failed`

Install or activate Python 3.11-3.13. With pyenv:

```bash
pyenv install 3.11.9
pyenv global 3.11.9
```

### Schema bootstrap check non-zero

The install succeeded but schema validation returned non-zero. Run init-db manually:

```bash
cd /path/to/your-project && vnx init-db
```

### Idempotency: re-running with the same version

Re-running `install-central.sh` with the same `--version` skips the clone (`already installed` message) and re-runs symlink swap and shim install. Safe to run multiple times. This is the correct way to refresh the shim after a VNX update that changes shim behavior.
