# Benchmark Scenarios

Each JSON file is one fixed benchmark scenario. Folders are campaigns: groups of
scenarios that vary one factor while keeping the rest comparable.

## Common Runs

```bash
# Start with one small baseline.
python -m benchmarks.run_benchmark --scenario benchmarks/scenarios/01_baseline/baseline_8_nodes.json

# Run one campaign folder.
python -m benchmarks.run_benchmark --scenario benchmarks/scenarios/06_policy_comparison/01_clean

# Run every campaign in sorted order.
python -m benchmarks.run_all_benchmarks

# Preview or validate without starting nodes.
python -m benchmarks.run_all_benchmarks --dry-run
python -m benchmarks.run_all_benchmarks --validate-only

# Run only one campaign.
python -m benchmarks.run_all_benchmarks --campaign 05_mixed_capabilities
```

## Campaigns

- `01_baseline`: clean reference scenarios.
- `02_latency`: same network size, different simulated network latency.
- `03_scalability`: same latency, different network sizes.
- `04_dropout`: same network size, different fixed dropout rates.
- `05_mixed_capabilities`: weighted multi-capability queries. Start with
  `mixed_clean_24_nodes.json` to validate mixed routing in ideal conditions,
  then compare latency/dropout variants.
- `06_policy_comparison`: same workload, different routing policies, split by context:
  - `01_clean`: no simulated network latency, no dropout.
  - `02_high_latency`: 100-500 ms simulated network latency.
  - `03_dropout_50`: 50% real process dropout followed by recovery warmup.
  - `04_mixed`: weighted multi-capability queries.

## Outputs

Results are written under `benchmarks/output/`:

```text
results.jsonl           raw per-query results with full routing traces
per_query_metrics.csv   one row per query with scalar metrics and scenario params
per_round_summary.csv   aggregate metrics for each round
summary.csv             final aggregate metrics across all rounds
```

## Scenario Notes

- `--scenario` accepts either one JSON file or a folder of JSON files.
- Benchmark parameters come from the scenario JSON. The runner injects per-node
  environment variables itself.
- `.env` is still used for base ports, API host, and normal node configuration.
- `rounds` repeats the same parameter combination and averages the results.
- `capability_advertise_interval_s` controls DHT capability republishing during
  the benchmark. Current scenarios use `5s`.
- `warmup_s` is the initial DHT stabilization time before any benchmark query.
- `post_dropout_warmup_s` is the recovery time after process dropout and before
  queries. Dropout scenarios use this to measure self-healing after churn.
- `entry_node_strategy` controls where queries enter the network. Supported
  values are `round_robin`, `fixed_node_0`, and `random`.

```json
{
  "name": "baseline_8_nodes",
  "num_nodes": 8,
  "rounds": 5,
  "network_latency_ms": [0, 0],
  "routing_policy": "current",
  "entry_node_strategy": "round_robin"
}
```

## Multi-Capability Queries

For multi-capability routing, start with
`05_mixed_capabilities/mixed_clean_24_nodes.json`: it uses 24 nodes, no
simulated latency, no dropout, and the current policy. That gives a clean
reference before adding latency or failures.

`query_capability` can be a list of single capabilities or richer weighted
query specs:

```json
{
  "query_capability": [
    "math",
    {
      "name": "math_creative_general",
      "prompt": "Design a creative math puzzle and explain its solution briefly.",
      "required_capabilities": {
        "math": 0.6,
        "creative": 0.5,
        "general": 0.4
      }
    }
  ]
}
```

## Policy Comparison

Inside each `06_policy_comparison/` context, run the five policy files to
compare `current`, `capability_only`, `latency_aware`, `reliability_aware`, and
`balanced` under the same conditions.
