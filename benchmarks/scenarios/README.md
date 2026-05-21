# Benchmark Scenarios

Each JSON file is a fixed scenario. Folders are campaigns: groups of scenarios
that vary one main factor while keeping the rest comparable.

Recommended first runs:

```bash
python -m benchmarks.run_benchmark --scenario benchmarks/scenarios/01_baseline/baseline_8_nodes.json
python -m benchmarks.run_benchmark --scenario benchmarks/scenarios/02_latency/latency_50_150_8_nodes.json
python -m benchmarks.run_benchmark --scenario benchmarks/scenarios/05_mixed_capabilities/mixed_clean_24_nodes.json
python -m benchmarks.run_benchmark --scenario benchmarks/scenarios/06_policy_comparison/01_clean/policy_clean_current_24_nodes.json
```

Run all scenarios in one campaign folder:

```bash
python -m benchmarks.run_benchmark --scenario benchmarks/scenarios/06_policy_comparison/01_clean
```

Run every campaign folder in order:

```bash
python -m benchmarks.run_all_benchmarks
```

Campaigns:

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
  - `03_dropout_50`: 50% real process dropout after discovery warmup.
  - `04_mixed`: weighted multi-capability queries.

The policy comparison campaign has one fixed scenario per file. To compare
policies in a context, run the five files in the corresponding subfolder.
