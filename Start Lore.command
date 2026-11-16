#!/bin/bash
# Double-click to start Lore (macOS). It opens the Connect Lore page in your browser.
# Keep the Terminal window open while you use Lore; close it to stop Lore.
cd "$(dirname "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
clear
echo "Lore"
echo "===="
if ! command -v uv >/dev/null 2>&1; then
  echo
  echo "Lore needs 'uv' (a Python tool) and it isn't installed."
  echo "Install it once by pasting this into Terminal, then double-click Start Lore again:"
  echo
  echo "    curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo
  read -r -p "Press Return to close this window." _
  exit 1
fi
PORT="${LORE_PUBLIC_PORT:-8848}"
if curl -fsS -m 3 "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
  echo "Lore is already running in another window. Opening the Connect Lore page."
  [ -z "${LORE_NO_BROWSER:-}" ] && open "http://127.0.0.1:${PORT}/?user=${LORE_USER:-OWN-002}"
  sleep 3
  exit 0
fi
scripts/public_tunnel.sh "${LORE_USER:-OWN-002}"
status=$?
echo
echo "Lore has stopped."
read -r -p "Press Return to close this window." _
exit $status
