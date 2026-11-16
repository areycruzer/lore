#!/usr/bin/env bash
# Expose Lore's public MCP to ChatGPT through a localhost.run SSH tunnel.
# Uses SSH (port 22), so it works on networks that block Cloudflare Tunnel's port 7844.
# Usage: scripts/public_tunnel.sh [lore-user-id]   (default OWN-002). Ctrl-C stops the server and the tunnel.
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${LORE_PUBLIC_PORT:-8848}"
USER_ID="${1:-OWN-002}"
LOG="$(mktemp -t lore-tunnel.XXXXXX)"
HOST_RE='[a-z0-9]+\.app\.lhr\.life'

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $PORT is already in use (another Lore server?). Stop it first:" >&2
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2
  exit 1
fi

ssh -T -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes \
    -R "80:127.0.0.1:${PORT}" nokey@localhost.run >"$LOG" 2>&1 &
SSH_PID=$!
WATCH_PID=""
trap 'kill "$SSH_PID" ${WATCH_PID:+"$WATCH_PID"} 2>/dev/null || true' EXIT

HOST=""
for _ in $(seq 1 30); do
  HOST="$(grep -oE "$HOST_RE" "$LOG" | head -1 || true)"
  [ -n "$HOST" ] && break
  kill -0 "$SSH_PID" 2>/dev/null || break
  sleep 1
done
if [ -z "$HOST" ]; then
  echo "Tunnel did not start. ssh output:" >&2
  cat "$LOG" >&2
  exit 1
fi

echo
echo "ChatGPT connector URL (no auth): https://${HOST}/mcp?user=${USER_ID}"
echo "localhost.run rotates free hostnames; a new URL is printed here when that happens. Update the connector with it."
echo

# Print the new URL whenever localhost.run rotates the hostname.
(
  last="$HOST"
  while kill -0 "$SSH_PID" 2>/dev/null; do
    sleep 5
    h="$(grep -oE "$HOST_RE" "$LOG" | tail -1 || true)"
    if [ -n "$h" ] && [ "$h" != "$last" ]; then
      echo
      echo ">>> Tunnel hostname changed. New ChatGPT connector URL: https://${h}/mcp?user=${USER_ID}"
      echo
      last="$h"
    fi
  done
  echo ">>> Tunnel closed. Rerun scripts/public_tunnel.sh." >&2
) &
WATCH_PID=$!

# The hostname rotates, so accept any Host header ("*").
LORE_PUBLIC_ALLOWED_HOSTS="*" LORE_PUBLIC_PORT="$PORT" uv run lore serve --http
