#!/usr/bin/env bash
# Public demo from this Mac via a free Cloudflare quick tunnel (no account, no card).
# Runs the single-container mode (in-process jobs, SQLite, local storage) with an access token
# and rate limit, keeps the Mac awake, and prints the public URL + token.
#
#   bash deploy/run_tunnel_demo.sh          # start (or restart) the demo
#   bash deploy/run_tunnel_demo.sh stop     # stop it
#
# The Mac must stay on, plugged in and online. The URL changes every time this script restarts the tunnel.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"
PORT=8200
NAME=broll-demo
TOKEN_FILE=deploy/.demo_token
LOG=deploy/.tunnel.log

stop() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  pkill -f "cloudflared tunnel --no-autoupdate --url http://localhost:$PORT" 2>/dev/null || true
  pkill -f "caffeinate -dims" 2>/dev/null || true
}
if [ "${1:-}" = "stop" ]; then stop; echo "demo stopped"; exit 0; fi

command -v docker >/dev/null || { echo "docker not found (start Colima: colima start)"; exit 1; }
docker info >/dev/null 2>&1 || colima start
if ! command -v cloudflared >/dev/null; then
  echo "installing cloudflared..."
  brew install cloudflared
fi
[ -f .env ] || { echo ".env missing"; exit 1; }
[ -f "$TOKEN_FILE" ] || { openssl rand -hex 16 > "$TOKEN_FILE"; chmod 600 "$TOKEN_FILE"; }
TOKEN=$(tr -d '[:space:]' < "$TOKEN_FILE")

stop
docker compose stop api worker >/dev/null 2>&1 || true   # free CPU/RAM for the demo container

echo "starting tunnel..."
nohup cloudflared tunnel --no-autoupdate --url "http://localhost:$PORT" > "$LOG" 2>&1 &
URL=""
for _ in $(seq 1 30); do
  URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$LOG" | head -1 || true)
  [ -n "$URL" ] && break
  sleep 2
done
[ -n "$URL" ] || { echo "tunnel did not start; see $LOG"; exit 1; }

echo "building and starting the app..."
docker build -q -t incubrix-broll-engine:latest . >/dev/null
docker run -d --name "$NAME" --restart unless-stopped -p "$PORT:8000" --env-file .env \
  -v incubrix-broll-engine_models:/models \
  -e JOB_RUNNER=inline -e STORAGE_BACKEND=local -e DATABASE_URL=sqlite:////tmp/broll.db \
  -e LOCAL_STORAGE_DIR=/tmp/broll-storage -e WORK_DIR=/tmp/broll-work -e MODEL_CACHE_DIR=/models \
  -e PUBLIC_BASE_URL="$URL" -e DEMO_ACCESS_TOKEN="$TOKEN" -e RATE_LIMIT_PER_MINUTE=10 \
  -e MAX_CONCURRENT_JOBS=2 -e APP_ENV=demo -e FORWARDED_ALLOW_IPS='*' \
  -e GIT_SHA="$(git rev-parse --short HEAD)" \
  incubrix-broll-engine:latest >/dev/null

for _ in $(seq 1 60); do
  curl -fsS -m 5 "$URL/ready" >/dev/null 2>&1 && break
  sleep 3
done

nohup caffeinate -dims >/dev/null 2>&1 &   # keep the Mac awake while the demo runs

echo
echo "Demo:   $URL/"
echo "Docs:   $URL/docs"
echo "Token:  $TOKEN"
echo "Ready:  $(curl -s -m 10 "$URL/ready")"
