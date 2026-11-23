#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu 22.04/24.04 VM (4 vCPU / 16 GB recommended, ports 80+443 open).
# Usage:  git clone <repo> broll && cd broll && cp /path/to/.env .env && bash deploy/setup_vm.sh
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "copy your .env here first (cp .env.example .env and fill keys)"; exit 1; }

if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
fi

IP=$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')
grep -q '^DEMO_DOMAIN=' .env || echo "DEMO_DOMAIN=${IP}.sslip.io" >> .env
if ! grep -q '^DEMO_ACCESS_TOKEN=.\+' .env; then
  sed -i '/^DEMO_ACCESS_TOKEN=/d' .env
  echo "DEMO_ACCESS_TOKEN=$(openssl rand -hex 16)" >> .env
fi
sed -i 's/^RATE_LIMIT_PER_MINUTE=.*/RATE_LIMIT_PER_MINUTE=10/' .env
sed -i "s/^S3_SECRET_KEY=.*/S3_SECRET_KEY=$(openssl rand -hex 16)/" .env
sed -i "s/^GIT_SHA=.*/GIT_SHA=$(git rev-parse --short HEAD)/" .env

sudo docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml up -d --build
DOMAIN=$(grep '^DEMO_DOMAIN=' .env | cut -d= -f2)
echo "Demo:  https://${DOMAIN}/"
echo "Token: $(grep '^DEMO_ACCESS_TOKEN=' .env | cut -d= -f2)"
