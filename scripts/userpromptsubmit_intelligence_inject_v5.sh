#!/usr/bin/env bash
# Compat shim -> userpromptsubmit_intelligence_inject.sh; remove in 1.0.1
exec "$(dirname "$0")/userpromptsubmit_intelligence_inject.sh" "$@"
