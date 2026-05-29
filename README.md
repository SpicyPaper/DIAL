# DIAL

Decentralized Intelligence Access Layer.

Beyond Centralized AI: Peer-to-Peer Discovery and Routing for Distributed LLM
Services.

This project runs a local network of libp2p nodes. Each node advertises its
capabilities through a Kademlia DHT; an entry node classifies a query, discovers
suitable peers, and either answers locally or forwards the query to a peer with
a suitable routing score.

The recommended backend is Ollama with Qwen3 1.7B. The project can also use the
EPFL AIaaS API, local Hugging Face Transformers models, or a dummy backend for
tests.

## Quick Start

These commands start a small local network with the recommended Ollama backend
and send one query through node 0.

In one terminal, start Ollama:

```bash
ollama serve
```

In another terminal, set up the project and run a query:

```bash
pip install uv
uv sync
cp .env.example .env
./scripts/start_network.sh
./scripts/query_any.sh 0 "Solve 2x + 4 = 10."
```

If the model is not already available, `scripts/start_network.sh` asks the
running Ollama server to pull it. Startup can take longer the first time.

You should see runtime files under `.runtime/`, including node logs and the list
of started nodes:

```text
.runtime/nodes/state/known_nodes.txt
.runtime/nodes/logs/node_<i>.log
```

Stop the network when you are done:

```bash
./scripts/stop_network.sh
```

## Features

- libp2p peer-to-peer networking with Kademlia DHT discovery
- capability routing for general, math, programming, writing, summarization,
  research, planning, and creative tasks
- LLM-based capability classification at the entry node
- simulated per-node capability scores for predictable local demos
- Ollama, AIaaS, local Hugging Face, and dummy backends
- HTTP APIs, a small web UI, and CLI scripts for local testing

## Requirements

- Python >= 3.13
- uv
- Ollama, for the recommended backend
- Bash-compatible shell for the scripts in `scripts/`

Install Ollama from:

```text
https://ollama.com/download
```

Start Ollama before running the network by opening the Ollama app or running:

```bash
ollama serve
```

Then check that the API responds:

```bash
curl http://localhost:11434/api/tags
```

If an Ollama model is missing, `scripts/start_network.sh` pulls it automatically
from the running Ollama server.

## Setup Details

```bash
pip install uv
uv sync
cp .env.example .env
```

Activate the virtual environment if you want to run commands without `uv run`:

```bash
source .venv/Scripts/activate
```

On Windows PowerShell, use:

```powershell
.\.venv\Scripts\Activate.ps1
```

Open `.env` and choose backends:

```text
REQUEST_BACKEND=ollama
CLASSIFIER_BACKEND=ollama
```

Available backends:

```text
ollama  local Ollama server
aiass   EPFL AIaaS OpenAI-compatible API
local   local Hugging Face model through Transformers
dummy   placeholder backend for tests
```

If either backend is `aiass`, set `AIASS_API_KEY`. The environment value is
spelled `aiass` even though the service is referred to as AIaaS elsewhere.

If either backend is `local`, each node process loads its own LLM. Keep
`NUM_NODES` small, especially with larger models, or use a shared backend such
as Ollama or AIaaS.

Useful `.env` values:

```text
NUM_NODES=8         number of local nodes started by the script
BASE_PORT=8002      first libp2p port
API_BASE_PORT=9002  first node HTTP API port
WEB_PORT=8000       web UI port
```

## Run

Start the local network:

```bash
./scripts/start_network.sh
```

Start a specific number of nodes:

```bash
./scripts/start_network.sh 4
```

Stop the network:

```bash
./scripts/stop_network.sh
```

Runtime files are written under `.runtime/`, especially:

```text
.runtime/nodes/state/known_nodes.txt
.runtime/nodes/logs/node_<i>.log
.runtime/web/config/bootstrap_nodes.txt
```

## Send Queries

Send a query through node 0:

```bash
./scripts/query_any.sh 0 "Solve 2x + 4 = 10."
```

More examples:

```bash
./scripts/query_any.sh 0 "Write a Python function that reverses a list."
./scripts/query_any.sh 0 "Rewrite this sentence to sound professional: send me the file now."
./scripts/query_any.sh 0 "Make me a 3-step study plan for distributed systems."
```

The entry node classifies the query, scores itself and discovered peers, then
answers locally or forwards the request.

Successful responses include the generated answer and routing metadata showing
which node handled the query.

## Web UI

Start DIAL Chat:

```bash
python -m src.ui.web_app
```

Then open:

```text
http://127.0.0.1:8000
```

The web UI reads entry-node API URLs from
`.runtime/web/config/bootstrap_nodes.txt`. The local network script writes the
first node API there automatically when the file is empty.

## Useful Commands

View started nodes:

```bash
cat .runtime/nodes/state/known_nodes.txt
```

Find providers for a capability:

```bash
python -m src.cli.find_nodes \
  --bootstrap "$(awk '$1 == 0 {print $5}' .runtime/nodes/state/known_nodes.txt)" \
  --capability math
```

Run one node manually:

```bash
python -m src.cli.run_node \
  --port 8003 \
  --api-port 9003 \
  --seed 1001 \
  --model-name node-1-math \
  --capabilities math \
  --dht-mode server \
  --bootstrap "<node-0-address>" \
  --system-prompt "You are a concise mathematics specialist."
```

Other CLIs:

```text
python -m src.cli.client_query  send one query to an entry node
python -m src.cli.send_message  low-level ping/query test
```

## Project Structure

```text
src/          node runtime, services, CLI commands, and web UI
scripts/      convenience scripts for local runs and queries
benchmarks/   routing benchmark runner, scenarios, and output files
report/       LaTeX report source and generated PDF
.runtime/     generated local runtime state, logs, and web config
```

## Routing Benchmarks

The benchmark runner evaluates routing quality under configurable network
conditions. It uses the dummy backend and sends `required_capabilities`
directly, so the results focus on routing rather than LLM generation.

Run a baseline scenario:

```bash
python -m benchmarks.run_benchmark \
  --scenario benchmarks/scenarios/01_baseline/baseline_8_nodes.json
```

Run every benchmark campaign in order:

```bash
python -m benchmarks.run_all_benchmarks
```

Preview the full batch without starting nodes:

```bash
python -m benchmarks.run_all_benchmarks --dry-run
```

Validate every scenario without starting nodes:

```bash
python -m benchmarks.run_all_benchmarks --validate-only
```

Results are written under `benchmarks/output/`. Scenario folders are documented
in `benchmarks/scenarios/README.md`.

## Report PDF

The LaTeX report can be built with the uv-installed Tectonic compiler:

```powershell
uv run tecto -X compile report\report.tex
```

The generated PDF is written to `report/report.pdf`. Add
`--print --keep-logs` to the command if you need detailed TeX output
while debugging.

## Troubleshooting

- If startup fails, check `.runtime/nodes/logs/node_<i>.log` first.
- If Ollama requests fail, make sure `ollama serve` is running and
  `curl http://localhost:11434/api/tags` returns a response.
- If a port is already used, change `BASE_PORT`, `API_BASE_PORT`, or `WEB_PORT`
  in `.env`.
- If the `local` backend is slow or runs out of memory, reduce `NUM_NODES` or
  switch to the shared `ollama` or `aiass` backend.
- On Windows, run the shell scripts from Git Bash, WSL, or another
  Bash-compatible shell.

## Notes

- Nodes advertise only their top scored capabilities in the DHT. Routers fetch a
  peer's full profile directly when they need detailed scores.
- GossipSub support exists but is still experimental; leave it disabled for
  normal local runs.
