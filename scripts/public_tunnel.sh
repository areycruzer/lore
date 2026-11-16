#!/usr/bin/env bash
# Expose Lore's public MCP to ChatGPT through an SSH tunnel (works on networks that block Cloudflare Tunnel's port 7844).
#   LORE_TUNNEL=pinggy (default): ssh to free.pinggy.io on port 443. The URL stays fixed for the whole session,
#                                 but free sessions end after 60 minutes, so start it shortly before the demo.
#   LORE_TUNNEL=lhr:              ssh to localhost.run on port 22. No time limit, but the hostname rotates
#                                 (about every 20 minutes), and every rotation breaks the ChatGPT plugin URL.
# Usage: scripts/public_tunnel.sh [lore-user-id]   (default OWN-002). Ctrl-C stops the server and the tunnel.
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${LORE_PUBLIC_PORT:-8848}"
USER_ID="${1:-OWN-002}"
TUNNEL="${LORE_TUNNEL:-pinggy}"
LOG="$(mktemp -t lore-tunnel.XXXXXX)"
SSH_OPTS=(-T -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes)
case "$TUNNEL" in
  pinggy)
    HOST_RE='[a-z0-9-]+\.free\.pinggy\.net'
    SSH_ARGS=(-p 443 -R "0:127.0.0.1:${PORT}" free.pinggy.io)
    NOTE="Pinggy keeps this URL for 60 minutes, then the tunnel closes. Rerun the script for a new URL."
    ;;
  lhr)
    HOST_RE='[a-z0-9]+\.app\.lhr\.life'
    SSH_ARGS=(-R "80:127.0.0.1:${PORT}" nokey@localhost.run)
    NOTE="localhost.run rotates free hostnames; new links are printed here when that happens."
    ;;
  *)
    echo "Unknown LORE_TUNNEL '$TUNNEL' (use pinggy or lhr)" >&2
    exit 1
    ;;
esac

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $PORT is already in use (another Lore server?). Stop it first:" >&2
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2
  exit 1
fi

ssh "${SSH_OPTS[@]}" "${SSH_ARGS[@]}" >"$LOG" 2>&1 &
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
echo "Connect page (open in a browser): https://${HOST}/?user=${USER_ID}"
echo "MCP server URL (no auth):         https://${HOST}/mcp?user=${USER_ID}"
echo "$NOTE"
echo

# Print the new URL whenever the hostname changes (localhost.run rotates it).
(
  last="$HOST"
  while kill -0 "$SSH_PID" 2>/dev/null; do
    sleep 5
    h="$(grep -oE "$HOST_RE" "$LOG" | tail -1 || true)"
    if [ -n "$h" ] && [ "$h" != "$last" ]; then
      echo
      echo ">>> Tunnel hostname changed. New connect page: https://${h}/?user=${USER_ID}"
      echo ">>> New MCP server URL (update the ChatGPT plugin): https://${h}/mcp?user=${USER_ID}"
      echo
      last="$h"
    fi
  done
  echo ">>> Tunnel closed. Rerun scripts/public_tunnel.sh." >&2
) &
WATCH_PID=$!

# The hostname rotates, so accept any Host header ("*").
LORE_PUBLIC_ALLOWED_HOSTS="*" LORE_PUBLIC_PORT="$PORT" uv run lore serve --http
