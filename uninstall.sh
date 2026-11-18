#!/usr/bin/env bash
# Remove this checkout's installation. Keep source code and annotation files.
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$ROOT/src/gvat/scripts/setup-common.sh"

# Optional: also remove downloaded tools and media preview caches.
PURGE_CACHE=0
for argument in "$@"; do
    case "$argument" in
        --purge-cache) PURGE_CACHE=1 ;;
        --help|-h)
            printf '%s\n' 'Usage: ./uninstall.sh [--purge-cache]' \
                'Preserves source, media, XML, and label lists.' \
                'Uses python3 or python. Override with GVAT_PYTHON=/path/to/python.'
            exit 0 ;;
        *) printf 'Unknown uninstall option: %s\n' "$argument" >&2; exit 2 ;;
    esac
done

# 1. Check the environment and start a quiet uninstall log.
require_linux
select_python
start_setup_log uninstall uninstall 'Uninstalling GVAT...'

# 2. Remove only the Bash alias and command links owned by this checkout.
"$GVAT_SETUP_PYTHON" "$ROOT/src/gvat/src/gvat_local/bash_alias.py" uninstall "$ROOT" "${GVAT_BASHRC:-$HOME/.bashrc}"

# 3. Remove the virtual environment and generated editor files.
# The helper checks path boundaries and preserves source and annotation data.
"$GVAT_SETUP_PYTHON" "$ROOT/src/gvat/tools/uninstall_files.py" \
    "$ROOT" "${GVAT_BIN_DIR:-$HOME/.local/bin}" "$PURGE_CACHE"

# 4. Explain how to refresh the current shell's alias.
trap - ERR
printf '%s\n' 'GVAT uninstalled. Samples and source code were preserved.' \
    'Open a new Bash terminal, or run: unalias gvat' >&3
