#!/usr/bin/env bash
# Shared checks and logging for the two root setup scripts.

require_linux() {
    if [[ "$(uname -s)" != Linux ]]; then
        printf '%s\n' 'GVAT currently supports Linux only.' >&2
        exit 1
    fi
}

select_python() {
    local candidate
    local candidates=(python3 python)
    if [[ -n "${GVAT_PYTHON:-}" ]]; then candidates=("$GVAT_PYTHON"); fi
    for candidate in "${candidates[@]}"; do
        if command -v "$candidate" >/dev/null 2>&1 &&
            "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
            GVAT_SETUP_PYTHON="$candidate"
            return
        fi
    done
    printf '%s\n' 'Python 3.10+ is required. Set GVAT_PYTHON=/path/to/python and rerun.' >&2
    exit 1
}

start_setup_log() {
    GVAT_SETUP_ACTION="$1"
    GVAT_SETUP_LOG="$ROOT/.gvat-cache/$2.log"
    mkdir -p "$ROOT/.gvat-cache"
    printf '%s\n' "$3"
    exec 3>&1 4>&2
    exec >"$GVAT_SETUP_LOG" 2>&1
    trap setup_failed ERR
}

setup_failed() {
    local status=$?
    trap - ERR
    printf 'GVAT %s failed. See %s\n' "$GVAT_SETUP_ACTION" "$GVAT_SETUP_LOG" >&4
    tail -n 1 "$GVAT_SETUP_LOG" >&4
    exit "$status"
}
