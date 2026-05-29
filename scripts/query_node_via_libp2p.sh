#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   ./scripts/query_node_via_libp2p.sh 0 "do some math please"
#   ./scripts/query_node_via_libp2p.sh 3 "write some python code"

if [ "$#" -lt 2 ]; then
  echo "Usage: $0 <node_index> <prompt>"
  exit 1
fi

NODE_INDEX="$1"
shift
PROMPT="$*"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ ! -f "$ROOT_DIR/.env" ]; then
  echo "ERROR: missing .env file at $ROOT_DIR/.env"
  echo "Create it before querying the network."
  exit 1
fi

NETWORK_FILE="$ROOT_DIR/.runtime/nodes/state/known_nodes.txt"

if [ ! -f "$NETWORK_FILE" ]; then
  echo "Network file not found: $NETWORK_FILE"
  echo "Start the network first."
  exit 1
fi

ENTRY_ADDR="$(awk -v idx="$NODE_INDEX" '$1 == idx {print $5}' "$NETWORK_FILE")"

if [ -z "$ENTRY_ADDR" ]; then
  echo "Could not find node index $NODE_INDEX in $NETWORK_FILE"
  exit 1
fi

if [[ "$ENTRY_ADDR" == *"<unknown-yet>"* ]]; then
  echo "Node $NODE_INDEX does not have a resolved peer id yet."
  exit 1
fi

python -m src.cli.query_via_libp2p \
  --entry-node "$ENTRY_ADDR" \
  --prompt "$PROMPT"
