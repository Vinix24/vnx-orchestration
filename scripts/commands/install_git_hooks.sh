#!/usr/bin/env bash
# VNX Command: install-git-hooks
# Installs VNX git hooks (prepare-commit-msg, commit-msg) for trace token enforcement.
#
# This file is sourced by bin/vnx's command loader.
#
# Hooks are installed as symlinks so updates propagate automatically.
# Existing hooks are backed up to .bak before replacement.

cmd_install_git_hooks() {
    local git_hooks_dir
    git_hooks_dir="$(git rev-parse --git-common-dir 2>/dev/null)/hooks"

    if [ ! -d "$(git rev-parse --git-common-dir 2>/dev/null)" ]; then
        err "[install-git-hooks] Not a git repository"
        return 1
    fi

    mkdir -p "$git_hooks_dir"

    local vnx_hooks_dir="$VNX_HOME/hooks/git"
    if [ ! -d "$vnx_hooks_dir" ]; then
        err "[install-git-hooks] VNX git hooks directory not found: $vnx_hooks_dir"
        return 1
    fi

    local installed=0
    local skipped=0

    for hook_name in prepare-commit-msg commit-msg; do
        local source="$vnx_hooks_dir/$hook_name"
        local target="$git_hooks_dir/$hook_name"

        if [ ! -f "$source" ]; then
            log "[install-git-hooks] WARN: Hook source not found: $source"
            skipped=$((skipped + 1))
            continue
        fi

        # Check if already correctly linked
        if [ -L "$target" ]; then
            local current_target
            current_target="$(readlink "$target")"
            if [ "$current_target" = "$source" ]; then
                log "[install-git-hooks] OK: $hook_name (already linked)"
                installed=$((installed + 1))
                continue
            fi
        fi

        # Backup existing hook if present and not our symlink
        if [ -e "$target" ] && [ ! -L "$target" ]; then
            local backup="$target.vnx-backup"
            log "[install-git-hooks] Backing up existing $hook_name -> $backup"
            cp "$target" "$backup"
        fi

        # Install as symlink
        ln -sf "$source" "$target"
        chmod +x "$source"
        log "[install-git-hooks] Installed: $hook_name -> $source"
        installed=$((installed + 1))
    done

    log "[install-git-hooks] Done: $installed installed, $skipped skipped"

    # Show current enforcement mode
    local mode="${VNX_PROVENANCE_ENFORCEMENT:-0}"
    if [ "$mode" = "1" ]; then
        log "[install-git-hooks] Enforcement: ACTIVE (commits without trace tokens will be blocked)"
    else
        log "[install-git-hooks] Enforcement: SHADOW (warnings only, commits not blocked)"
    fi

    return 0
}

cmd_uninstall_git_hooks() {
    local git_hooks_dir
    git_hooks_dir="$(git rev-parse --git-common-dir 2>/dev/null)/hooks"

    for hook_name in prepare-commit-msg commit-msg; do
        local target="$git_hooks_dir/$hook_name"
        if [ -L "$target" ]; then
            local link_target
            link_target="$(readlink "$target")"
            if echo "$link_target" | grep -q "vnx-system\|VNX_HOME"; then
                rm "$target"
                log "[uninstall-git-hooks] Removed: $hook_name"

                # Restore backup if present
                local backup="$target.vnx-backup"
                if [ -f "$backup" ]; then
                    mv "$backup" "$target"
                    log "[uninstall-git-hooks] Restored backup: $hook_name"
                fi
            fi
        fi
    done

    log "[uninstall-git-hooks] Done"
    return 0
}
