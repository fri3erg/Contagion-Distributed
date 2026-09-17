#!/usr/bin/env bash
# Convenience wrapper: (re)build and start a fresh game, then print the
# dashboard URL. Run from the repo root: ./scripts/play.sh
set -euo pipefail
cd "$(dirname "$0")/.."

docker compose down -v --remove-orphans 2>/dev/null || true
docker compose up --build -d

echo
echo "Dashboard: http://localhost:8000/"
echo "Aggregator status: http://localhost:8000/status"
echo
echo "Tail all logs with: docker compose logs -f"
echo "Stop with:          docker compose down"
