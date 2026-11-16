#!/usr/bin/env bash
# Start Lore with a public link ChatGPT can reach, then open the Connect Lore page in your browser.
# Non-technical users: double-click "Start Lore.command" in the Lore folder instead of running this.
# Uses an SSH tunnel, so it works on networks that block Cloudflare Tunnel's port 7844.
#   LORE_TUNNEL=pinggy (default): ssh to free.pinggy.io on port 443. The URL stays fixed for the whole session,
#                                 but free sessions end after 60 minutes, so start it shortly before the demo.
#   LORE_TUNNEL=lhr:              ssh to localhost.run on port 22. No time limit, but the hostname rotates
#                                 (about every 20 minutes), and every rotation breaks the ChatGPT plugin URL.
#   LORE_NO_BROWSER=1:            don't open the browser.
# Usage: scripts/public_tunnel.sh [lore-user-id]   (default OWN-002). Ctrl-C stops the server and the tunnel.
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${LORE_PUBLIC_PORT:-8848}"
USER_ID="${1:-OWN-002}"
TUNNEL="${LORE_TUNNEL:-pinggy}"
LOCAL_PAGE="http://127.0.0.1:${PORT}/?user=${USER_ID}"
STATE="${LORE_VAR_DIR:-var}/public_url.json"
LOG="$(mktemp -t lore-tunnel.XXXXXX)"
SSH_OPTS=(-T -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes)
case "$TUNNEL" in
  pinggy)
    HOST_RE='[a-z0-9-]+\.free\.pinggy\.net'
    SSH_ARGS=(-p 443 -R "0:127.0.0.1:${PORT}" free.pinggy.io)
    LIFETIME=3600
    NOTE="This link lasts 60 minutes. After that, start Lore again and update ChatGPT."
    ;;
  lhr)
    HOST_RE='[a-z0-9]+\.app\.lhr\.life'
    SSH_ARGS=(-R "80:127.0.0.1:${PORT}" nokey@localhost.run)
    LIFETIME=""
    NOTE="localhost.run changes this link about every 20 minutes; new links are printed here when that happens."
    ;;
  *)
    echo "Unknown LORE_TUNNEL '$TUNNEL' (use pinggy or lhr)" >&2
    exit 1
    ;;
esac

open_page() {
  [ -n "${LORE_NO_BROWSER:-}" ] && return 0
  if command -v open >/dev/null 2>&1; then open "$1"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$1" >/dev/null 2>&1
  fi
}

write_state() {  # $1 = public host
  local expires="null"
  [ -n "$LIFETIME" ] && expires=$(( $(date +%s) + LIFETIME ))
  mkdir -p "$(dirname "$STATE")"
  printf '{"url": "https://%s", "expires_at": %s}\n' "$1" "$expires" >"$STATE"
}

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  if curl -fsS -m 3 "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
    echo "Lore is already running. Opening the Connect Lore page: ${LOCAL_PAGE}"
    open_page "$LOCAL_PAGE"
    exit 0
  fi
  echo "Port $PORT is used by another program. Close it, or set LORE_PUBLIC_PORT to a free port:" >&2
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2
  exit 1
fi

echo "Starting Lore's public link..."
ssh "${SSH_OPTS[@]}" "${SSH_ARGS[@]}" >"$LOG" 2>&1 &
SSH_PID=$!
WATCH_PID=""
trap 'rm -f "$STATE"; kill "$SSH_PID" ${WATCH_PID:+"$WATCH_PID"} 2>/dev/null || true' EXIT

HOST=""
for _ in $(seq 1 30); do
  HOST="$(grep -oE "$HOST_RE" "$LOG" | head -1 || true)"
  [ -n "$HOST" ] && break
  kill -0 "$SSH_PID" 2>/dev/null || break
  sleep 1
done
if [ -z "$HOST" ]; then
  echo "Could not create a public link (is the internet connected?). Details:" >&2
  cat "$LOG" >&2
  exit 1
fi
write_state "$HOST"

MCP_URL="https://${HOST}/mcp?user=${USER_ID}"
COPIED=""
if command -v pbcopy >/dev/null 2>&1; then printf '%s' "$MCP_URL" | pbcopy && COPIED=" (copied to your clipboard)"; fi

echo
echo "  Lore is starting. The Connect Lore page opens in your browser in a few seconds."
echo
echo "  ChatGPT link${COPIED}:"
echo "    ${MCP_URL}"
echo "  Connect page (share or open on another device):"
echo "    https://${HOST}/?user=${USER_ID}"
echo
echo "  ${NOTE}"
echo "  Keep this window open while you use Lore. Close it or press Ctrl-C to stop."
echo

# Track hostname changes (localhost.run rotates them) and open the page once the server answers.
(
  last="$HOST"
  opened=""
  while kill -0 "$SSH_PID" 2>/dev/null; do
    if [ -z "$opened" ] && curl -fsS -m 2 "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
      open_page "$LOCAL_PAGE"
      opened=1
    fi
    sleep 2
    h="$(grep -oE "$HOST_RE" "$LOG" | tail -1 || true)"
    if [ -n "$h" ] && [ "$h" != "$last" ]; then
      write_state "$h"
      echo
      echo ">>> Lore's public link changed. Update the ChatGPT plugin with: https://${h}/mcp?user=${USER_ID}"
      echo ">>> The Connect Lore page shows the new link: ${LOCAL_PAGE}"
      echo
      last="$h"
    fi
  done
  rm -f "$STATE"
  echo ">>> The public link closed. Start Lore again." >&2
) &
WATCH_PID=$!

# The hostname rotates, so accept any Host header ("*").
LORE_PUBLIC_ALLOWED_HOSTS="*" LORE_PUBLIC_PORT="$PORT" uv run lore serve --http
