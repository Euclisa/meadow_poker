#!/usr/bin/env bash
set -euo pipefail

ALL_SERVICES=(backend telegram web)

usage() {
  cat <<EOF
Usage: $0 [OPTIONS]

Options:
  --backend     Enable meadow-backend service
  --telegram    Enable meadow-telegram service
  --web         Enable meadow-web service
  --all         Enable all services (default if none specified)
  --no-tests    Skip running tests
  -h, --help    Show this help

Examples:
  $0                        # deploy + restart all services
  $0 --backend --web        # deploy + restart only backend and web
  $0 --telegram --no-tests  # deploy telegram without running tests
EOF
  exit 0
}

require_command() {
  local cmd="$1"
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "Missing required command: ${cmd}" >&2
    exit 1
  fi
}

ENABLED_SERVICES=()
RUN_TESTS=true

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend)  ENABLED_SERVICES+=(backend);  shift ;;
    --telegram) ENABLED_SERVICES+=(telegram); shift ;;
    --web)      ENABLED_SERVICES+=(web);      shift ;;
    --all)      ENABLED_SERVICES=("${ALL_SERVICES[@]}"); shift ;;
    --no-tests) RUN_TESTS=false; shift ;;
    -h|--help)  usage ;;
    *) echo "Unknown option: $1" >&2; usage ;;
  esac
done

# Default to all services when none specified
if [[ ${#ENABLED_SERVICES[@]} -eq 0 ]]; then
  ENABLED_SERVICES=("${ALL_SERVICES[@]}")
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${REPO_ROOT}"

require_command git
require_command python3
require_command systemctl

if [[ "${EUID}" -ne 0 ]]; then
  echo "This script must be run as root." >&2
  exit 1
fi

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

if [[ "${RUN_TESTS}" == true ]]; then
  echo "Running focused checks..."
  .venv/bin/pytest \
    tests/test_config.py \
    tests/test_backend_http.py \
    tests/test_telegram_app.py \
    tests/test_main.py
fi

echo "Restarting services: ${ENABLED_SERVICES[*]}..."
UNIT_NAMES=()
for svc in "${ENABLED_SERVICES[@]}"; do
  UNIT_NAMES+=("meadow-${svc}")
done
systemctl restart "${UNIT_NAMES[@]}"

cat <<EOF
Deploy complete. Services restarted: ${UNIT_NAMES[*]}

Check service status:
  systemctl status ${UNIT_NAMES[*]}

Tail logs:
  journalctl $(printf -- '-u %s ' "${UNIT_NAMES[@]}")-f
EOF
