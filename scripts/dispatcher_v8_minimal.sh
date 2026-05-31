#!/usr/bin/env bash
# Compat shim -> dispatcher_minimal.sh; remove in 1.0.1
exec "$(dirname "$0")/dispatcher_minimal.sh" "$@"
