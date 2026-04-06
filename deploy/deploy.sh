#!/usr/bin/env bash
set -euo pipefail

require_command() {
  local command_name="$1"
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "Missing required command: ${command_name}" >&2
    exit 1
  fi
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${REPO_ROOT}"

require_command git
require_command python3
require_command systemctl
require_command sudo

echo "Updating code..."
git pull --ff-only

if [[ ! -d .venv ]]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

echo "Upgrading pip..."
.venv/bin/python -m pip install --upgrade pip

echo "Installing dependencies..."
.venv/bin/pip install -r requirements.txt

echo "Running focused checks..."
PYTHONPATH=src .venv/bin/pytest \
  tests/test_config.py \
  tests/test_backend_http.py \
  tests/test_telegram_app.py \
  tests/test_main.py

echo "Restarting services..."
sudo systemctl restart meadow-backend meadow-telegram

cat <<'EOF'
Deploy complete.

Check service status:
  sudo systemctl status meadow-backend meadow-telegram

Tail logs:
  sudo journalctl -u meadow-backend -u meadow-telegram -f
EOF
