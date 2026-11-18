#!/usr/bin/env bash
# Install for the current user. No sudo or system package changes.
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$ROOT/src/gvat/scripts/setup-common.sh"

# Optional: skip the command link or Bash alias.
LINK_COMMAND=1
ALIAS_COMMAND=1
for argument in "$@"; do
    case "$argument" in
        --no-link) LINK_COMMAND=0 ;;
        --no-alias) ALIAS_COMMAND=0 ;;
        --help|-h)
            printf '%s\n' 'Usage: ./install.sh [--no-link] [--no-alias]' \
                'Uses python3 or python (3.10+). Override with GVAT_PYTHON=/path/to/python.'
            exit 0 ;;
        *) printf 'Unknown setup option: %s\n' "$argument" >&2; exit 2 ;;
    esac
done

# 1. Check Linux/Python and keep dependency output in a log.
require_linux
select_python
start_setup_log installation install 'Installing GVAT...'

# 2. Create a private Python environment.
if [[ ! -x "$ROOT/.venv-gvat/bin/python" ]]; then
    "$GVAT_SETUP_PYTHON" -m venv "$ROOT/.venv-gvat"
fi
VENV_PYTHON="$ROOT/.venv-gvat/bin/python"

# 3. Prepare local build tools if the annotation exporter needs compiling.
"$VENV_PYTHON" "$ROOT/src/gvat/tools/install_build_tools.py" \
    "${XDG_CACHE_HOME:-$HOME/.cache}/gvat/build-tools" "$ROOT/.gvat-cache/build-env.sh"
source "$ROOT/.gvat-cache/build-env.sh"

# 4. Install GVAT and its export engine without desktop OpenCV dependencies.
export DATUMARO_HEADLESS=1
"$VENV_PYTHON" -m pip install --disable-pip-version-check -e "$ROOT/src/gvat"
"$VENV_PYTHON" "$ROOT/src/gvat/tools/install_config.py" "$ROOT/config.yml"

# 5. Use an existing Node 18+, or download a verified local Node binary.
if ! command -v node >/dev/null || ! node -e 'process.exit(+process.versions.node.split(".")[0] >= 18 ? 0 : 1)'; then
    NODE_CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/gvat/node"
    mkdir -p "$NODE_CACHE"
    "$GVAT_SETUP_PYTHON" "$ROOT/src/gvat/tools/install_node.py" "$NODE_CACHE"
    export PATH="$(cat "$NODE_CACHE/active-path"):$PATH"
fi
command -v npm >/dev/null || { printf '%s\n' 'npm is required alongside Node.' >&2; false; }

# 6. Install locked frontend dependencies and build CVAT's annotation screen.
npm --prefix "$ROOT/src/gvat/web" ci --omit=optional --ignore-scripts --no-audit --no-fund
npm --prefix "$ROOT/src/gvat/web" run build
chmod +x "$ROOT/src/bin/gvat" "$ROOT/install.sh" "$ROOT/uninstall.sh"

# 7. Add the command link and managed Bash alias, preserving unrelated commands.
INSTALLED_LINK=""
if (( LINK_COMMAND )); then
    COMMAND_DIRECTORY="${GVAT_BIN_DIR:-$HOME/.local/bin}"
    mkdir -p "$COMMAND_DIRECTORY"
    if [[ ! -e "$COMMAND_DIRECTORY/gvat" || -L "$COMMAND_DIRECTORY/gvat" ]]; then
        ln -sfn "$ROOT/src/bin/gvat" "$COMMAND_DIRECTORY/gvat"
        INSTALLED_LINK="$COMMAND_DIRECTORY/gvat"
    fi
fi
BASHRC_PATH=""
if (( ALIAS_COMMAND )); then BASHRC_PATH="${GVAT_BASHRC:-$HOME/.bashrc}"; fi
"$VENV_PYTHON" "$ROOT/src/gvat/src/gvat_local/bash_alias.py" install "$ROOT" "$INSTALLED_LINK" "$BASHRC_PATH"

# Report only the next steps; detailed output remains in .gvat-cache/install.log.
trap - ERR
printf '%s\n' 'GVAT installed.' >&3
if (( ALIAS_COMMAND )); then
    printf 'Run: source %q\n' "$BASHRC_PATH" >&3
    printf '%s\n' 'Start: gvat --data_path="/path/to/sample" --mode=annotate' >&3
else
    printf 'Start: %q --data_path="/path/to/sample" --mode=annotate\n' "$ROOT/src/bin/gvat" >&3
fi
