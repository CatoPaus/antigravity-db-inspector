#!/usr/bin/env bash
#
# Antigravity DB Inspector Installer for Linux (Ubuntu / Debian)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_SCRIPT="${SCRIPT_DIR}/server.py"

INSTALL_GLOBAL=0
INSTALL_SERVICE=0
DO_UNINSTALL=0

for arg in "$@"; do
  case "$arg" in
    --global|-g)
      INSTALL_GLOBAL=1
      ;;
    --service|-s)
      INSTALL_SERVICE=1
      ;;
    --uninstall|-u)
      DO_UNINSTALL=1
      ;;
    --help|-h)
      cat << 'USAGE'
Antigravity DB Inspector Installer

Usage:
  ./install.sh [options]

Options:
  --global, -g     Install globally to /usr/local/bin (requires sudo)
  --service, -s    Enable background systemd user service (starts on login)
  --uninstall, -u  Remove installed launcher and systemd service
  --help, -h       Show this help message

Default:
  Installs executable launcher to ~/.local/bin/antigravity-db-inspector (no sudo required)
USAGE
      exit 0
      ;;
  esac
done

# Handle Uninstall
if [ "$DO_UNINSTALL" -eq 1 ]; then
  echo "Uninstalling Antigravity DB Inspector..."
  rm -f "${HOME}/.local/bin/antigravity-db-inspector"
  if [ -w "/usr/local/bin" ]; then
    rm -f "/usr/local/bin/antigravity-db-inspector"
  fi
  if [ -f "${HOME}/.config/systemd/user/antigravity-db-inspector.service" ]; then
    systemctl --user stop antigravity-db-inspector.service 2>/dev/null || true
    systemctl --user disable antigravity-db-inspector.service 2>/dev/null || true
    rm -f "${HOME}/.config/systemd/user/antigravity-db-inspector.service"
    systemctl --user daemon-reload 2>/dev/null || true
  fi
  echo "Successfully uninstalled!"
  exit 0
fi

echo "========================================================"
echo " Installing Antigravity DB Inspector (Ubuntu / Linux)"
echo "========================================================"

# 1. Verify Python 3 & sqlite3
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 is not installed." >&2
  echo "Install it via: sudo apt update && sudo apt install -y python3" >&2
  exit 1
fi

PYTHON_VER=$(python3 -c 'import sys; print("%d.%d" % (sys.version_info.major, sys.version_info.minor))')
echo "✓ Python version: ${PYTHON_VER}"

if ! python3 -c 'import sqlite3' >/dev/null 2>&1; then
  echo "ERROR: Python sqlite3 module missing." >&2
  echo "Install it via: sudo apt install -y python3-sqlite3" >&2
  exit 1
fi
echo "✓ Python sqlite3 support verified (no pip dependencies required)"

# 2. Check Antigravity Database Discovery
echo ""
echo "Checking Antigravity database locations on this system..."
DESKTOP_DB_DIR="${HOME}/.gemini/antigravity/conversations"
CLI_DB_DIR="${HOME}/.gemini/antigravity-cli/conversations"

FOUND_ANY=0
if [ -d "$DESKTOP_DB_DIR" ]; then
  COUNT=$(find "$DESKTOP_DB_DIR" -maxdepth 1 -name "*.db" 2>/dev/null | wc -l)
  echo "✓ Desktop conversations found: ${DESKTOP_DB_DIR} (${COUNT} databases)"
  FOUND_ANY=1
else
  echo "ℹ Desktop conversations directory not yet created at: ${DESKTOP_DB_DIR}"
fi

if [ -d "$CLI_DB_DIR" ]; then
  COUNT=$(find "$CLI_DB_DIR" -maxdepth 1 -name "*.db" 2>/dev/null | wc -l)
  echo "✓ CLI conversations found: ${CLI_DB_DIR} (${COUNT} databases)"
  FOUND_ANY=1
else
  echo "ℹ CLI conversations directory not found at: ${CLI_DB_DIR}"
fi

if [ "$FOUND_ANY" -eq 0 ]; then
  echo "⚠ Note: No conversation databases detected yet. Make sure Antigravity has been launched."
fi

# 3. Install CLI Launcher
echo ""
chmod +x "$SERVER_SCRIPT"

if [ "$INSTALL_GLOBAL" -eq 1 ]; then
  TARGET_BIN="/usr/local/bin/antigravity-db-inspector"
  if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: --global requires root privileges. Please run with sudo." >&2
    exit 1
  fi
else
  TARGET_DIR="${HOME}/.local/bin"
  mkdir -p "$TARGET_DIR"
  TARGET_BIN="${TARGET_DIR}/antigravity-db-inspector"
fi

cat << LAUNCHER > "$TARGET_BIN"
#!/usr/bin/env bash
# Antigravity DB Inspector Launcher
exec python3 "${SERVER_SCRIPT}" "\$@"
LAUNCHER

chmod +x "$TARGET_BIN"
echo "✓ Installed launcher: ${TARGET_BIN}"

# Check PATH for local install
if [ "$INSTALL_GLOBAL" -eq 0 ]; then
  case ":$PATH:" in
    *":${HOME}/.local/bin:"*) ;;
    *)
      echo ""
      echo "⚠ Notice: ~/.local/bin is not in your current PATH."
      echo "  Add it by running:"
      echo '    echo '\''export PATH="$HOME/.local/bin:$PATH"'\'' >> ~/.bashrc'
      echo "  or for zsh:"
      echo '    echo '\''export PATH="$HOME/.local/bin:$PATH"'\'' >> ~/.zshrc'
      ;;
  esac
fi

# 4. Optional Systemd User Service
if [ "$INSTALL_SERVICE" -eq 1 ]; then
  echo ""
  echo "Setting up systemd user service..."
  SERVICE_DIR="${HOME}/.config/systemd/user"
  mkdir -p "$SERVICE_DIR"
  SERVICE_FILE="${SERVICE_DIR}/antigravity-db-inspector.service"

  cat << SERVICE > "$SERVICE_FILE"
[Unit]
Description=Antigravity SQLite Database Inspector
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 ${SERVER_SCRIPT} --port 8990 --host 0.0.0.0
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
SERVICE

  systemctl --user daemon-reload
  systemctl --user enable --now antigravity-db-inspector.service
  echo "✓ systemd user service enabled and started (antigravity-db-inspector.service)"
fi

echo ""
echo "========================================================"
echo " Installation Complete!"
echo " Launch the inspector anytime by running:"
echo "   antigravity-db-inspector"
echo ""
echo " Web UI: http://localhost:8990"
echo "========================================================"
