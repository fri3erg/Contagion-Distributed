#!/usr/bin/env bash
# Regenerate topology/topology.json and docker-compose.yml. Pass through
# any flags topology/generate_topology.py accepts, e.g.:
#   ./scripts/regenerate_topology.sh --nodes 40 --degree 6 --seed 7
set -euo pipefail
cd "$(dirname "$0")/.."
python3 topology/generate_topology.py "$@"
