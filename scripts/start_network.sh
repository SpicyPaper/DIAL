#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   ./scripts/start_network.sh 5
#
# Starts N nodes.
# Node 0 is the bootstrap / entry-friendly node.
# Other nodes bootstrap to node 0.
#
# Outputs:
#   .runtime/nodes/state/pids.txt
#   .runtime/nodes/state/known_nodes.txt
#   .runtime/nodes/logs/node_<i>.log

require_env() {
  local name="$1"
  if [ -z "${!name:-}" ]; then
    echo "ERROR: missing $name in .env"
    exit 1
  fi
}

ollama_model_installed() {
  local model="$1"
  curl -fsS "${OLLAMA_HOST}/api/tags" \
    | MODEL_TO_CHECK="$model" python -c "import json,os,sys; data=json.load(sys.stdin); names={m.get('name') for m in data.get('models', [])}; sys.exit(0 if os.environ['MODEL_TO_CHECK'] in names else 1)" \
    >/dev/null 2>&1
}

ollama_pull_model() {
  local model="$1"
  echo "Model '${model}' is missing. Pulling it with Ollama..."
  local payload
  payload="$(MODEL_TO_PULL="$model" python -c "import json,os; print(json.dumps({'name': os.environ['MODEL_TO_PULL'], 'stream': False}))")"
  if ! curl -fsS "${OLLAMA_HOST}/api/pull" \
    -H "Content-Type: application/json" \
    -d "$payload" \
    | python -m json.tool; then
    echo "ERROR: failed to pull Ollama model '${model}'."
    exit 1
  fi
}

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ ! -f "$ROOT_DIR/.env" ]; then
  echo "ERROR: missing .env file at $ROOT_DIR/.env"
  echo "Create it before starting the network."
  exit 1
fi

set -a
# shellcheck disable=SC1091
. "$ROOT_DIR/.env"
set +a

require_env BASE_PORT
require_env API_BASE_PORT
require_env API_HOST
require_env ADVERTISE_ADDRESS_MODE
require_env REQUEST_BACKEND
require_env CLASSIFIER_BACKEND
require_env CLASSIFIER_TIMEOUT
require_env QUERY_TIMEOUT
require_env QUERY_CONNECT_TIMEOUT

NUM_NODES="${1:-${NUM_NODES:-}}"

if ! [[ "$NUM_NODES" =~ ^[0-9]+$ ]] || [ "$NUM_NODES" -lt 1 ]; then
  echo "Usage: $0 <num_nodes>=1.."
  exit 1
fi

RUNTIME_DIR="$ROOT_DIR/.runtime"
NODES_RUNTIME_DIR="$RUNTIME_DIR/nodes"
WEB_RUNTIME_DIR="$RUNTIME_DIR/web"
STATE_DIR="$NODES_RUNTIME_DIR/state"
WEB_CONFIG_DIR="$WEB_RUNTIME_DIR/config"
LOG_DIR="$NODES_RUNTIME_DIR/logs"
PID_FILE="$STATE_DIR/pids.txt"
NETWORK_FILE="$STATE_DIR/known_nodes.txt"
WEB_BOOTSTRAP_FILE="$WEB_CONFIG_DIR/bootstrap_nodes.txt"

mkdir -p "$STATE_DIR" "$WEB_CONFIG_DIR" "$LOG_DIR"

# Stop previous network if still running.
if [ -f "$PID_FILE" ]; then
  while read -r pid; do
    [ -n "${pid:-}" ] && kill "$pid" 2>/dev/null || true
  done < "$PID_FILE"
fi
: > "$PID_FILE"
: > "$NETWORK_FILE"

# Capability pool.
CAPS_POOL=(
  "general"
  "math"
  "programming"
  "writing"
  "summarization"
  "research"
  "planning"
  "creative"
)


if [ "$REQUEST_BACKEND" != "dummy" ] && [ "$REQUEST_BACKEND" != "ollama" ] && [ "$REQUEST_BACKEND" != "local" ] && [ "$REQUEST_BACKEND" != "aiass" ]; then
  echo "ERROR: REQUEST_BACKEND must be one of: dummy, ollama, local, aiass."
  exit 1
fi

if [ "$CLASSIFIER_BACKEND" != "dummy" ] && [ "$CLASSIFIER_BACKEND" != "ollama" ] && [ "$CLASSIFIER_BACKEND" != "local" ] && [ "$CLASSIFIER_BACKEND" != "aiass" ]; then
  echo "ERROR: CLASSIFIER_BACKEND must be one of: dummy, ollama, local, aiass."
  exit 1
fi

if [ "$REQUEST_BACKEND" = "ollama" ] || [ "$CLASSIFIER_BACKEND" = "ollama" ]; then
  require_env OLLAMA_MODEL
  require_env OLLAMA_CLASSIFIER_MODEL
  require_env OLLAMA_HOST
  require_env OLLAMA_TIMEOUT
  require_env OLLAMA_NUM_PREDICT
  require_env OLLAMA_CHECK_TIMEOUT

  echo "Checking Ollama backend..."
  if ! command -v curl >/dev/null 2>&1; then
    echo "ERROR: curl is required to check the Ollama API before starting nodes."
    exit 1
  fi

  if ! curl -fsS "${OLLAMA_HOST}/api/tags" >/dev/null 2>&1; then
    echo "ERROR: Ollama API is not reachable at ${OLLAMA_HOST}."
    echo "Start it with:"
    echo "  ollama serve"
    echo "Or open the Ollama desktop app, then rerun this script."
    exit 1
  fi

  OLLAMA_MODELS_TO_CHECK=()
  if [ "$REQUEST_BACKEND" = "ollama" ]; then
    OLLAMA_MODELS_TO_CHECK+=("$OLLAMA_MODEL")
  fi
  if [ "$CLASSIFIER_BACKEND" = "ollama" ]; then
    OLLAMA_MODELS_TO_CHECK+=("$OLLAMA_CLASSIFIER_MODEL")
  fi

  for MODEL_TO_CHECK in "${OLLAMA_MODELS_TO_CHECK[@]}"; do
    if ! ollama_model_installed "$MODEL_TO_CHECK"; then
      ollama_pull_model "$MODEL_TO_CHECK"
    fi

    if ! ollama_model_installed "$MODEL_TO_CHECK"; then
      echo "ERROR: Ollama model '${MODEL_TO_CHECK}' is still not installed after pull."
      exit 1
    fi
  done

  echo "Ollama ready: ${OLLAMA_HOST} request_model=${OLLAMA_MODEL} classifier_model=${OLLAMA_CLASSIFIER_MODEL}"
fi

if [ "$REQUEST_BACKEND" = "aiass" ] || [ "$CLASSIFIER_BACKEND" = "aiass" ]; then
  require_env AIASS_API_KEY
  require_env AIASS_BASE_URL
  require_env AIASS_MODEL
  require_env AIASS_CLASSIFIER_MODEL
  require_env AIASS_TIMEOUT
  require_env AIASS_MAX_TOKENS

  if [ "$AIASS_API_KEY" = "YOUR_AIASS_API_KEY_HERE" ]; then
    echo "ERROR: AIASS_API_KEY still has the placeholder value."
    echo "Set your real AIaaS key in .env before using the AIaaS backend."
    exit 1
  fi

  if [ "$REQUEST_BACKEND" = "aiass" ] && awk -v aiass="$AIASS_TIMEOUT" -v query="$QUERY_TIMEOUT" 'BEGIN { exit !(aiass >= query) }'; then
    echo "WARNING: AIASS_TIMEOUT (${AIASS_TIMEOUT}s) is >= QUERY_TIMEOUT (${QUERY_TIMEOUT}s)."
    echo "The entry node may stop waiting before the selected AIaaS node finishes."
  fi

  echo "AIaaS configured: ${AIASS_BASE_URL} request_model=${AIASS_MODEL} classifier_model=${AIASS_CLASSIFIER_MODEL}"
fi

if [ "$REQUEST_BACKEND" = "local" ] || [ "$CLASSIFIER_BACKEND" = "local" ]; then
  require_env LOCAL_MODEL_ID
  require_env LOCAL_CLASSIFIER_MODEL_ID
  require_env LOCAL_MAX_NEW_TOKENS
  require_env LOCAL_ENABLE_THINKING
  require_env LOCAL_TIMEOUT

  echo "Local Transformers configured: request_model=${LOCAL_MODEL_ID} classifier_model=${LOCAL_CLASSIFIER_MODEL_ID}"
  echo "Preloading local Hugging Face model files before starting nodes..."
  python -m src.cli.preload_local_models
fi

if [ "$REQUEST_BACKEND" = "local" ] || [ "$CLASSIFIER_BACKEND" = "local" ]; then
  NODE_STARTUP_WAIT_S="${NODE_STARTUP_WAIT_S:-300}"
else
  NODE_STARTUP_WAIT_S="${NODE_STARTUP_WAIT_S:-25}"
fi
NODE_STARTUP_WAIT_ATTEMPTS=$((NODE_STARTUP_WAIT_S * 2))

echo "Request backend: ${REQUEST_BACKEND}"
echo "Classifier backend: ${CLASSIFIER_BACKEND}"
echo "Capability scores: simulated"
echo

echo "Starting $NUM_NODES nodes..."
echo

# Start node 0 first so others can bootstrap to it.
PORT0=$BASE_PORT
API_PORT0=$API_BASE_PORT
SEED0=1000
CAP0="general"
MODEL0="node-0-entry"

SYSTEM_PROMPT0="You are a general-purpose AI node. Give clear, balanced answers across many topics. Stay concise and avoid expert-level detail unless the task is simple and well-known."

LOG0="$LOG_DIR/node_0.log"
python -m src.cli.run_node \
  --port "$PORT0" \
  --api-port "$API_PORT0" \
  --seed "$SEED0" \
  --capabilities "$CAP0" \
  --model-name "$MODEL0" \
  --system-prompt "$SYSTEM_PROMPT0" \
  > "$LOG0" 2>&1 &

PID0=$!
echo "$PID0" >> "$PID_FILE"

echo "Started node 0 (pid=$PID0, port=$PORT0, api=$API_PORT0, caps=$CAP0)"
echo "Waiting up to ${NODE_STARTUP_WAIT_S}s for node 0 peer id..."

ENTRY_PEER_ID=""
for _ in $(seq 1 "$NODE_STARTUP_WAIT_ATTEMPTS"); do
  if grep -q "^\[.*\] \[NODE\] I am " "$LOG0"; then
    ENTRY_PEER_ID="$(grep -m1 "^\[.*\] \[NODE\] I am " "$LOG0" | sed -E 's/.*I am ([^ ]+).*/\1/')"
    break
  fi
  sleep 0.5
done

if [ -z "$ENTRY_PEER_ID" ]; then
  echo "Failed to read node 0 peer id from $LOG0"
  exit 1
fi

ENTRY_ADDR="/ip6/::1/tcp/${PORT0}/p2p/${ENTRY_PEER_ID}"
echo "0 $PORT0 $CAP0 $MODEL0 $ENTRY_ADDR http://${API_HOST}:${API_PORT0}" >> "$NETWORK_FILE"
if [ ! -s "$WEB_BOOTSTRAP_FILE" ]; then
  echo "http://${API_HOST}:${API_PORT0}" > "$WEB_BOOTSTRAP_FILE"
  echo "Wrote web bootstrap node: $WEB_BOOTSTRAP_FILE"
else
  echo "Keeping existing web bootstrap nodes: $WEB_BOOTSTRAP_FILE"
fi

echo "Bootstrap node address:"
echo "  $ENTRY_ADDR"
echo "Bootstrap node API:"
echo "  http://${API_HOST}:${API_PORT0}/api/query"
echo

# Start remaining nodes.
for ((i=1; i<NUM_NODES; i++)); do
  PORT=$((BASE_PORT + i))
  API_PORT=$((API_BASE_PORT + i))
  SEED=$((1000 + i))
  PRIMARY_CAP="${CAPS_POOL[$((i % ${#CAPS_POOL[@]}))]}"
  CAP="$PRIMARY_CAP"
  MODEL="node-${i}-${PRIMARY_CAP}"

  case "$PRIMARY_CAP" in
    math)
      SYSTEM_PROMPT="You are a specialized AI node. Specialty: mathematics. For mathematics tasks: give your best answer, show formulas and key steps, and verify results. For tasks outside mathematics: give a very basic answer, keep it brief, and do not use expert reasoning."
      ;;
    programming)
      SYSTEM_PROMPT="You are a specialized AI node. Specialty: programming. For programming tasks: write correct runnable code, handle edge cases, and explain bugs before fixing them. For tasks outside programming: give a very basic answer, keep it brief, and do not use expert reasoning."
      ;;
    writing)
      SYSTEM_PROMPT="You are a specialized AI node. Specialty: writing. For writing tasks: improve tone, clarity, structure, and wording while preserving intent. For tasks outside writing: give a very basic answer, keep it brief, and do not use expert reasoning."
      ;;
    summarization)
      SYSTEM_PROMPT="You are a specialized AI node. Specialty: summarization. For summarization tasks: keep key facts, relationships, constraints, and caveats while removing unnecessary detail. For tasks outside summarization: give a very basic answer, keep it brief, and do not use expert reasoning."
      ;;
    research)
      SYSTEM_PROMPT="You are a specialized AI node. Specialty: research. For research tasks: check evidence, source quality, uncertainty, and alternatives. Separate facts from assumptions. For tasks outside research: give a very basic answer, keep it brief, and do not use expert reasoning."
      ;;
    planning)
      SYSTEM_PROMPT="You are a specialized AI node. Specialty: planning. For planning tasks: give ordered realistic steps, dependencies, timing, checkpoints, and risks. For tasks outside planning: give a very basic answer, keep it brief, and do not use expert reasoning."
      ;;
    creative)
      SYSTEM_PROMPT="You are a specialized AI node. Specialty: creative ideation. For creative tasks: give original, vivid, coherent ideas that respect the constraints. Avoid generic ideas. For tasks outside creative ideation: give a very basic answer, keep it brief, and do not use expert reasoning."
      ;;
    *)
      SYSTEM_PROMPT="You are a general-purpose AI node. Give clear, balanced answers across many topics. Stay concise and avoid expert-level detail unless the task is simple and well-known."
      ;;
  esac

  LOG_FILE="$LOG_DIR/node_${i}.log"

  python -m src.cli.run_node \
    --port "$PORT" \
    --api-port "$API_PORT" \
    --seed "$SEED" \
    --capabilities "$CAP" \
    --model-name "$MODEL" \
    --bootstrap "$ENTRY_ADDR" \
    --system-prompt "$SYSTEM_PROMPT" \
    > "$LOG_FILE" 2>&1 &

  PID=$!
  echo "$PID" >> "$PID_FILE"

  echo "Started node $i (pid=$PID, port=$PORT, api=$API_PORT, caps=$CAP)"

  PEER_ID=""
  for _ in $(seq 1 "$NODE_STARTUP_WAIT_ATTEMPTS"); do
    if grep -q "^\[.*\] \[NODE\] I am " "$LOG_FILE"; then
      PEER_ID="$(grep -m1 "^\[.*\] \[NODE\] I am " "$LOG_FILE" | sed -E 's/.*I am ([^ ]+).*/\1/')"
      break
    fi
    sleep 0.5
  done

  if [ -z "$PEER_ID" ]; then
    echo "Warning: could not read peer id for node $i yet"
    ADDR="/ip6/::1/tcp/${PORT}/p2p/<unknown-yet>"
  else
    ADDR="/ip6/::1/tcp/${PORT}/p2p/${PEER_ID}"
  fi

  echo "$i $PORT $CAP $MODEL $ADDR http://${API_HOST}:${API_PORT}" >> "$NETWORK_FILE"
done

echo
echo "Network started."
echo
echo "Available nodes:"
column -t "$NETWORK_FILE" || cat "$NETWORK_FILE"
echo
echo "Logs:"
echo "  $LOG_DIR"
echo
echo "PIDs:"
echo "  $PID_FILE"
