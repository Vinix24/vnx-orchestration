#!/usr/bin/env bash
# Compat shim -> pane_manager.sh; remove in 1.0.1
exec "$(dirname "$0")/pane_manager.sh" "$@"
