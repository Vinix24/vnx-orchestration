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

After installing a new version, each project needs a switchover. This is a three-step process: symlink update, version pin, and doctor verification.

### Step 1: Pre-switchover checks

```bash
# Verify the new version is installed
ls ~/.vnx-system/versions/

# Confirm install-mode marker is present
cat ~/.vnx-system/versions/<version>/.vnx-install-mode  # must print "central"

# Confirm no .vnx-data contamination in the new version dir
ls ~/.vnx-system/versions/<version>/.vnx-data 2>/dev/null && echo "FAIL: contaminated" || echo "clean"
```

### Step 2: Update project pin

```bash
cd /path/to/your-project

# Pin to new version
echo '<version>' > .vnx-version

# Verify shim picks up the new version
vnx --version
```

### Step 3: Verify doctor PASS

Run `vnx doctor` from the project directory and confirm all checks pass:

```bash
cd /path/to/your-project
vnx doctor
```

Expected output after successful cutover:

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

If you see any FAIL mentioning `~/.vnx-system/` paths, the PROJECT_ROOT resolver is pointing at VNX_HOME instead of your project. See [Troubleshooting](#troubleshooting).

### Step 4: Re-initialize if needed

If the project has never been initialized with the new version, or if `.vnx/` is missing:

```bash
cd /path/to/your-project
vnx init
vnx regen-settings --merge --dry-run   # review what changes, nothing written yet
vnx regen-settings --merge
vnx bootstrap-hooks
vnx doctor  # re-verify
```

`regen-settings` and `bootstrap-hooks` are fabric commands: the central shim
`~/.vnx-system/bin/vnx` and `./bin/vnx` from a checkout both carry them, while the pip
`vnx` at v1.7.6 answers `invalid choice: 'regen-settings'`. Check which binary answers
before you run them: `command -v vnx` prints the path, and `vnx --help` lists
`regen-settings` only when the fabric CLI is the one running (call the shim by its path
if it does not).

`vnx regen-settings --merge` refreshes the VNX-owned keys and keeps the project's own
hooks. `--full` renders the whole `settings.json` from the template and stays for the
first-init case only: a project that has no `.claude/settings.json` yet. On an existing
project it drops every project hook. `--replace-hooks` (with `--merge`) is the explicit
way to get that replace-everything behaviour and drop the project hooks on purpose.

### Step 5: Confirm runtime isolation

Verify that runtime data goes to the project, not to VNX_HOME:

```bash
# After running any vnx command:
ls /path/to/your-project/.vnx-data/   # should exist
ls ~/.vnx-system/versions/<version>/.vnx-data/ 2>/dev/null && echo "FAIL: leaked" || echo "ok"
```

## Upgrading

```bash
# Install new version (keeps old versions)
bash install-central.sh --version <version>

# Update project pin
echo '<version>' > /path/to/project/.vnx-version
```

The `current` symlink points to the newly installed version after each successful run.

## Before a rollout

`preflight` is a verb of the pip `vnx` (`vnx_cli`) that is on main but not in the
deployed release: measured 28-10, the pip CLI at v1.7.6 answers `invalid choice:
'preflight'`, and the central shim `~/.vnx-system/bin/vnx` refuses the same name
(`Error: 'preflight' requires a different mode`). Run this section from a checkout at
main with its pip CLI installed (`pip install -e .` from the repository root);
`vnx release verify` is main-only too, and `./bin/vnx` reaches it from that same
checkout. Confirm which binary answers with `command -v vnx` and `vnx --help` —
`preflight` appears in that list only on an install that carries it.

Measure first. `vnx preflight` is read-only: it installs nothing, reloads no launchd job
and writes no settings file. Run it from the project, or scope it with `--project <id>`
(repeatable; an unknown id exits `2`). Add `--json` for a machine-readable report.

```bash
vnx preflight
vnx preflight --project <project-id> --json
```

Exit codes: `0` no blocking finding, `1` at least one blocking finding, `2` a measurement
itself failed (an unreadable store, a raising check). Exit `2` is never a pass.

It measures four blocking classes per project:

| Class | What a finding means |
|---|---|
| `pin` | The project's `.vnx-version` names no installed release dir (a dead pin, or an engine link that dangles), or the pinned dir fails `vnx release verify`. |
| `launchd` | An installed plist violates the pin alignment check. The healthy state is `pin_followed`; every other state is a violation: `pin_ignored_current` (the job follows the rolling `current` while the project pins a version), `pin_ignored_other_version` (another release, a missing dir or no release), `pin_dead` (the pinned release dir does not exist) and `pin_unmeasurable` (the command line assigns `VNX_HOME` through a variable, so no release can be measured). |
| `hooks` | A `vnx regen-settings --merge --dry-run` render would change a hook command. Both the old and the new command are named. A dry-run that fails is itself a finding. |
| `main_drift` | The doctor check `release:main-drift` fails: the running release lacks commits the local main ref already has. |

A blocking finding in any class exits `1`. A class reported as `unmeasured` was never
measured — a project without a pin reports `pin` and `launchd` as unmeasured — so it is
never a pass. Treat it as a stop and measure the class before rolling out. The exit code
alone stays `0` for that case, so read the report, not only the exit code.

Then verify the release dir itself:

```bash
vnx release verify <version>
```

Every file in the tag must be byte-identical on disk and no extra file may exist, apart
from the stripped `.vnx-project-id` and the written install-mode marker. It exits `1` and
names every differing path. A missing dir, a dir that is not a git checkout, an
unresolvable tag and an unreadable tag tree all fail closed.

Then the doctor check on the running release:

```bash
vnx doctor
```

`release:main-drift` is `FAIL` while the running release lags the local main ref; it names
the commits it is missing and fails closed when it cannot measure. Only a release that
cannot be compared at all — no release, not a git checkout, no local main ref — reports
`skipped: <reason>` and leaves doctor green.

Three steps stay operator actions. VNX does not do them for you:

1. **The release itself.** `vnx release publish --tag <tag>` materializes the tag into
   `~/.vnx-system/versions/<tag>/`. Publishing is refused for a tag outside the default
   branch unless `--allow-unmerged-tag "<reason>"` names a non-empty reason.
2. **The `current` flip.** Publishing does not flip `current` by default. The flip is
   `--set-current` on publish, or `vnx update`. Consumers with a tracked `.vnx-version`
   pin do not follow `current`; they need their pin updated.
3. **Reloading the launchd jobs.** `bash scripts/launchd/reload_plist.sh <template>
   [project_id]` per family and per project. A running job keeps the tree it started
   from; only the next start resolves the pin again, through
   `scripts/launchd/vnx_pinned_launcher.sh`.

## Rollback procedure

### System-level rollback (all projects)

Re-point the `current` symlink to a previous version:

```bash
# List installed versions
ls ~/.vnx-system/versions/

# Re-point current to a previous version
ln -sfn ~/.vnx-system/versions/<previous-version> ~/.vnx-system/current
```

Or re-run the installer with the previous version:

```bash
bash install-central.sh --version <previous-version>
```

Projects without a `.vnx-version` pin will automatically use the rolled-back `current`.

### Per-project rollback

If only one project has issues after cutover, roll back its pin without touching other projects:

```bash
cd /path/to/your-project

# Revert to previous version pin
echo '<previous-version>' > .vnx-version

# Verify doctor passes on the old version
vnx doctor
```

This leaves all other projects on their current pins unaffected.

### Emergency unblock (without new install)

If you need to unblock a project immediately and can't wait for a code fix:

```bash
# Manually write the install-mode marker (triggers central detection)
echo "central" > ~/.vnx-system/versions/<version>/.vnx-install-mode

# Then run doctor from project dir — should detect project root correctly
cd /path/to/your-project && vnx doctor
```

This is a historical workaround for installs that predate the automatic marker
write in `clone_version()` (pre-1.0.0-rc5); every version currently installable
writes the marker automatically.

## Pre-fix contamination cleanup

Historical: applies only to installs from before the path resolver fix
(pre-1.0.0-rc5). If you ran `vnx init` on one of those, runtime data may have
been written to `~/.vnx-system/versions/<version>/.vnx-data/` instead of your
project.

**Check:**

```bash
ls ~/.vnx-system/versions/*/'.vnx-data/' 2>/dev/null
```

**Clean:**

```bash
# Back up contaminated data (receipts, DB snapshots) before removing
VERSION_DIR=~/.vnx-system/versions/<version>  # adjust to your version

cp -r "$VERSION_DIR/.vnx-data" /tmp/central-vnx-data-backup-$(date +%Y%m%d)

# Remove the contamination
rm -rf "$VERSION_DIR/.vnx-data"
rm -rf "$VERSION_DIR/.claude"
rm -rf "$VERSION_DIR/.vnx-intelligence"

# Verify clean
ls "$VERSION_DIR/.vnx-data" 2>/dev/null && echo "FAIL: still contaminated" || echo "clean"
```

After cleanup, run `vnx init` from each project directory to initialize clean runtime state in the correct location.

## Troubleshooting

### Doctor FAIL: Runtime root missing or points to VNX_HOME

**Symptom:**

```
[FAIL] path: Runtime root missing: /Users/you/.vnx-system/versions/<version>
[FAIL] dir: Missing: VNX config (/Users/you/.vnx-system/versions/<version>/.vnx)
[FAIL] file: Missing config: /Users/you/.vnx-system/versions/<version>/.vnx/config.yml
        Fix: Run: vnx init
```

**Cause:** PROJECT_ROOT resolver is using VNX_HOME as the project root. This means either (a) the `.vnx-install-mode` marker is missing from the version dir, or (b) you're running `vnx` from inside `~/.vnx-system/...` instead of from your project directory.

**Fix:**

```bash
# Check the marker
cat ~/.vnx-system/versions/<version>/.vnx-install-mode  # must print "central"

# If missing: write it manually
echo "central" > ~/.vnx-system/versions/<version>/.vnx-install-mode

# Then run from your project dir
cd /path/to/your-project && vnx doctor
```

If the marker is present but doctor still fails, re-run `install-central.sh` to reinstall the shim (which exports `VNX_PROJECT_ROOT`):

```bash
bash install-central.sh --version <version>
```

### Doctor FAIL: Missing .claude/settings.json

**Symptom:**

```
[FAIL] settings: Missing .claude/settings.json
        Fix: Run: vnx regen-settings --full
```

The `--full` in that line is the first-init case: it applies only when there is no
`.claude/settings.json` at all. The fix below uses `--merge`, which also creates the file
when it is absent and keeps any project hooks that are present.

**Cause:** Either the project was never initialized, or a previous run wrote settings to VNX_HOME (contamination).

**Fix:**

```bash
cd /path/to/your-project

# Check if settings landed in wrong place
ls ~/.vnx-system/versions/<version>/.claude/settings.json 2>/dev/null && echo "CONTAMINATION"

# Clean up contamination if found
rm -rf ~/.vnx-system/versions/<version>/.claude

# Generate settings in correct location
vnx regen-settings --merge --dry-run
vnx regen-settings --merge
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

### Dead pin: the launchd job refuses to start, or regen-settings refuses to render

**Symptom:** A launchd job exits `78` (EX_CONFIG) with ONE stderr line, and nothing else:

```
project 'my-project' pins '1.0.9', but nothing under /Users/you/.vnx-system/versions answers it (not installed, or it escapes the versions root)
```

`vnx regen-settings` makes the same resolution and prints the same line through `ERROR:`,
then exits `1`:

```
ERROR: checkout '/path/to/your-project' pins '1.0.9', but nothing under /Users/you/.vnx-system/versions answers it (not installed, or it escapes the versions root)
```

An unregistered project, a pin that is malformed or escapes the versions root, and a
pinned tree without the script the job needs also exit `78` from the launcher, each with
one line naming what was missing. There is no fallback to another tree.

**Cause:** `.vnx-version` names a version that is not installed under
`~/.vnx-system/versions/`, or the pin is not usable.

**Fix:** install the pinned version, or change the pin to an installed one:

```bash
# Install the pinned version
bash install-central.sh --version <pin>

# Or change the pin to an installed version
ls ~/.vnx-system/versions/
echo '<installed-version>' > /path/to/your-project/.vnx-version
```

The next start (launchd) or render (`regen-settings`) resolves again; a run already in
progress is not switched.

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
