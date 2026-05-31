#!/usr/bin/env bash
# Compat shim -> receipt_processor.sh; remove in 1.0.1
exec "$(dirname "$0")/receipt_processor.sh" "$@"
