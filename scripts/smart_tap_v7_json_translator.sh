#!/usr/bin/env bash
# Compat shim -> smart_tap_json_translator.sh; remove in 1.0.1
exec "$(dirname "$0")/smart_tap_json_translator.sh" "$@"
