#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
TOOLS_DIR="$SCRIPT_DIR/.blockops-tools"
UV="$TOOLS_DIR/uv"
PYTHON="$SCRIPT_DIR/.venv/bin/python"

echo "BlockOps setup"
echo "=============="
echo "This installs a private Python runtime inside BlockOps. It does not require admin access."

if [[ ! -x "$UV" ]]; then
  echo "[1/3] Downloading the trusted uv bootstrap tool from Astral..."
  mkdir -p "$TOOLS_DIR"
  curl -LsSf https://astral.sh/uv/0.12.1/install.sh | env UV_UNMANAGED_INSTALL="$TOOLS_DIR" sh
else
  echo "[1/3] Bootstrap tool is ready."
fi

echo "[2/3] Preparing private Python 3.12..."
if [[ ! -x "$PYTHON" ]]; then
  "$UV" venv --python 3.12 "$SCRIPT_DIR/.venv"
fi

echo "[3/3] Checking the app..."
cd "$SCRIPT_DIR"
"$PYTHON" -m unittest discover -s tests >/dev/null

echo
echo "Setup complete. You can use BlockOps.app or BlockOps.command from now on."
if [[ "${1:-}" != "--no-launch" ]]; then
  echo "BlockOps is opening in your browser."
  exec "$PYTHON" "$SCRIPT_DIR/dashboard.py"
fi
