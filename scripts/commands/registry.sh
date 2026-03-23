#!/usr/bin/env bash
# VNX Command: registry (register, list-projects, unregister)
# Manages a global project registry at ~/.vnx/projects.json.
#
# This file is sourced by bin/vnx's command loader. All functions and variables
# from the main script (log, err, PROJECT_ROOT, VNX_HOME, etc.)
# are available when this runs.

VNX_REGISTRY_DIR="$HOME/.vnx"
VNX_REGISTRY_FILE="$VNX_REGISTRY_DIR/projects.json"

# ── register ─────────────────────────────────────────────────────────────
cmd_register() {
  local project_path="${1:-$PROJECT_ROOT}"
  local project_name="${2:-$(basename "$project_path")}"

  # Resolve to absolute path
  if [ -d "$project_path" ]; then
    project_path="$(cd "$project_path" && pwd)"
  fi

  # Detect layout
  local layout="unknown"
  if [ -d "$project_path/.vnx/bin" ]; then
    layout="vnx"
  elif [ -d "$project_path/.claude/vnx-system/bin" ]; then
    layout="claude"
  fi

  mkdir -p "$VNX_REGISTRY_DIR"

  if ! command -v python3 &>/dev/null; then
    err "[register] python3 is required for registry management"
    return 1
  fi

  python3 -c "
import json, sys, os
from datetime import datetime, timezone

registry_file = '$VNX_REGISTRY_FILE'
project_path = '$project_path'
project_name = '$project_name'
layout = '$layout'

# Load or create registry
data = {'projects': []}
if os.path.exists(registry_file):
    with open(registry_file) as f:
        data = json.load(f)

# Check for duplicates
for p in data['projects']:
    if p['path'] == project_path:
        p['name'] = project_name
        p['layout'] = layout
        p['updated_at'] = datetime.now(timezone.utc).isoformat()
        with open(registry_file, 'w') as f:
            json.dump(data, f, indent=2)
        print(f'Updated: {project_name} ({project_path})')
        sys.exit(0)

# Add new entry
data['projects'].append({
    'name': project_name,
    'path': project_path,
    'layout': layout,
    'registered_at': datetime.now(timezone.utc).isoformat(),
    'updated_at': datetime.now(timezone.utc).isoformat()
})

with open(registry_file, 'w') as f:
    json.dump(data, f, indent=2)
print(f'Registered: {project_name} ({project_path}) [layout={layout}]')
" || return 1

  log "[register] Project registered in $VNX_REGISTRY_FILE"
}

# ── list-projects ────────────────────────────────────────────────────────
cmd_list_projects() {
  if [ ! -f "$VNX_REGISTRY_FILE" ]; then
    log "No projects registered yet. Use 'vnx register' to add a project."
    return 0
  fi

  if ! command -v python3 &>/dev/null; then
    err "[list-projects] python3 is required"
    return 1
  fi

  python3 -c "
import json, os, sys

registry_file = '$VNX_REGISTRY_FILE'
with open(registry_file) as f:
    data = json.load(f)

projects = data.get('projects', [])
if not projects:
    print('No projects registered.')
    sys.exit(0)

print(f'VNX Projects ({len(projects)} registered):')
print()
for p in projects:
    path = p['path']
    exists = os.path.isdir(path)
    layout = p.get('layout', 'unknown')
    status = 'OK' if exists else 'MISSING'

    # Check if VNX binary exists
    if exists:
        if layout == 'vnx' and os.path.isfile(os.path.join(path, '.vnx', 'bin', 'vnx')):
            status = 'OK'
        elif layout == 'claude' and os.path.isfile(os.path.join(path, '.claude', 'vnx-system', 'bin', 'vnx')):
            status = 'OK'
        else:
            status = 'NO-BIN'

    print(f'  {p[\"name\"]:30s}  [{layout:6s}]  [{status:7s}]  {path}')
" || return 1
}

# ── unregister ───────────────────────────────────────────────────────────
cmd_unregister() {
  local project_path="${1:-$PROJECT_ROOT}"

  if [ -d "$project_path" ]; then
    project_path="$(cd "$project_path" && pwd)"
  fi

  if [ ! -f "$VNX_REGISTRY_FILE" ]; then
    log "No projects registered."
    return 0
  fi

  if ! command -v python3 &>/dev/null; then
    err "[unregister] python3 is required"
    return 1
  fi

  python3 -c "
import json, sys, os

registry_file = '$VNX_REGISTRY_FILE'
project_path = '$project_path'

with open(registry_file) as f:
    data = json.load(f)

original_count = len(data.get('projects', []))
data['projects'] = [p for p in data.get('projects', []) if p['path'] != project_path]
removed = original_count - len(data['projects'])

with open(registry_file, 'w') as f:
    json.dump(data, f, indent=2)

if removed > 0:
    print(f'Unregistered: {project_path}')
else:
    print(f'Not found in registry: {project_path}')
" || return 1
}
