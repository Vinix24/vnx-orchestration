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

## Pre-flight checklist

Before running `install-central.sh`:

- [ ] Python >= 3.11 and < 3.14 available: `python3 --version`
- [ ] git available: `git --version`
- [ ] sqlite3 available: `sqlite3 --version`
- [ ] At least 500MB free disk in target parent: `df -h ~`
- [ ] Network access to `github.com` (for clone) unless `--source` points to a local mirror
- [ ] Target directory (`~/.vnx-system` by default) is writable

Run `--dry-run` first to validate without side effects:

```bash
bash install-central.sh --dry-run
```

## Installation

```bash
# Default: latest stable to ~/.vnx-system
bash install-central.sh

# Pin a specific version
bash install-central.sh --version v1.0.0-rc3

# Custom install root
bash install-central.sh --target /opt/vnx-system --version v1.0.0-rc3

# Internal mirror
bash install-central.sh --source https://internal.git/vnx-orchestration --version v1.0.0-rc3
```

After install, add the shim to PATH:

```bash
export PATH="${PATH}:${HOME}/.vnx-system/bin"
```

Add to `~/.zshrc` or `~/.bashrc` to persist.

## Project pinning via .vnx-version

Each project can pin a specific installed version:

```bash
echo 'v1.0.0-rc3' > /path/to/project/.vnx-version
```

The shim reads `.vnx-version` by traversing up from `cwd`. When no pin is found, it falls back to `~/.vnx-system/current` (the last installed version).

Installing a new version does NOT break pinned projects. Old versions remain at `~/.vnx-system/versions/`.

## Upgrading

```bash
# Install new version (keeps old versions)
bash install-central.sh --version v1.0.1

# Update project pin
echo 'v1.0.1' > /path/to/project/.vnx-version
```

The `current` symlink points to the newly installed version after each successful run.

## Rollback procedure

If the new version causes issues, roll back the symlink manually:

```bash
# List installed versions
ls ~/.vnx-system/versions/

# Re-point current to a previous version
ln -sfn ~/.vnx-system/versions/v1.0.0-rc1 ~/.vnx-system/current
```

Or re-run the installer with the previous version:

```bash
bash install-central.sh --version v1.0.0-rc1
```

Project pins in `.vnx-version` are unaffected and continue to work.

## Troubleshooting

**`git clone failed`**

- Verify network access to the source URL
- Check the version tag exists: `git ls-remote --tags https://github.com/Vinix24/vnx-orchestration`
- Use `--source` to point to a local clone: `--source /path/to/local/vnx-orchestration`

**`Pinned version X not installed`**

The shim found `.vnx-version` but that version is not in `~/.vnx-system/versions/`. Run:

```bash
bash install-central.sh --version <pin>
```

**`Insufficient disk space`**

Free at least 500MB in the target parent directory. Each version install is roughly 50-100MB.

**`Python version check failed`**

Install or activate Python 3.11-3.13. With pyenv:

```bash
pyenv install 3.11.9
pyenv global 3.11.9
```

**Schema bootstrap check non-zero**

The install succeeded but `quality_db_init.py --check-only` returned non-zero. Run it manually to inspect:

```bash
python3 ~/.vnx-system/current/scripts/quality_db_init.py
```

**Idempotency: re-running with the same version**

Re-running `install-central.sh` with the same `--version` skips the clone (`already installed` message) and re-runs symlink swap and shim install. Safe to run multiple times.
