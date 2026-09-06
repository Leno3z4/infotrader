#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/infotrader}"
REPO_URL="${REPO_URL:-https://github.com/Leno3z4/infotrader.git}"
BRANCH="${BRANCH:-main}"

if [[ ! -d "$APP_DIR/.git" ]]; then
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
else
  git -C "$APP_DIR" fetch origin "$BRANCH"
  git -C "$APP_DIR" reset --hard "origin/$BRANCH"
fi

cd "$APP_DIR"
test -f .env || { echo 'Missing .env; create it from .env.example first.' >&2; exit 1; }
docker compose pull || true
docker compose build --pull
docker compose up -d --remove-orphans
docker compose ps
