#!/bin/zsh
set -eu
SCRIPT_DIR=${0:A:h}
PYTHON="$SCRIPT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  exec "$SCRIPT_DIR/setup.command"
fi
exec "$PYTHON" "$SCRIPT_DIR/dashboard.py"
