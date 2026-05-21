from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.env_config import load_project_env, optional_env
from src.services.routing_policy import get_routing_policy


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CAPS_POOL = [
    "general",
    "math",
    "programming",
    "writing",
    "summarization",
    "research",
    "planning",
    "creative",
]
ENTRY_CAPABILITY = "general"
DEFAULT_PROMPTS = {
    "general": "Answer a short general-purpose benchmark query.",
    "math": "Solve a short algebra problem.",
    "programming": "Write a small Python helper function.",
    "writing": "Rewrite a sentence to make it clearer.",
    "summarization": "Summarize a short technical paragraph.",
    "research": "Compare evidence for two technical options.",
    "planning": "Create a concise implementation plan.",
    "creative": "Generate a constrained creative idea.",
}
DEFAULT_MIXED_PROMPT = "Answer a query that requires several capabilities."


@dataclass
class NodeProcess:
    index: int
    port: int
    api_port: int
    capability: str
    model_name: str
    log_path: Path
    process: subprocess.Popen
    peer_id: str | None = None
    api_url: str | None = None


def main() -> None:
    parser = argparse.ArgumentParser(description="Run TSADAI routing benchmarks.")
    parser.add_argument(
        "--scenario",
        required=True,
        help="Path to a scenario JSON file or a directory of scenario JSON files.",
    )
    parser.add_argument(
        "--output-dir",
        default="benchmarks/output",
        help="Directory where run outputs are written.",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate the scenario and exit without starting nodes.",
    )
    args = parser.parse_args()

    try:
        load_project_env()
    except RuntimeError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc

    scenario_path = Path(args.scenario)
    scenarios = load_scenarios(scenario_path)
    if args.validate_only:
        for path, scenario in scenarios:
            validate_scenario(scenario)
            print(f"Scenario valid: {path}", flush=True)
        return

    for _, scenario in scenarios:
        run_campaign(scenario, Path(args.output_dir))


def load_scenarios(path: Path) -> list[tuple[Path, dict[str, Any]]]:
    if path.is_dir():
        scenario_paths = sorted(path.rglob("*.json"))
        if not scenario_paths:
            raise SystemExit(f"ERROR: no scenario JSON files found in {path}")
        return [(scenario_path, load_json(scenario_path)) for scenario_path in scenario_paths]

    if not path.exists():
        raise SystemExit(f"ERROR: scenario path does not exist: {path}")
    return [(path, load_json(path))]


def run_campaign(scenario: dict[str, Any], output_root: Path) -> None:
    campaign_name = scenario.get("name") or Path("scenario").stem
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    campaign_dir = output_root / f"{timestamp}-{campaign_name}"
    campaign_dir.mkdir(parents=True, exist_ok=True)

    raw_path = campaign_dir / "results.jsonl"
    per_query_path = campaign_dir / "per_query_metrics.csv"
    per_round_summary_path = campaign_dir / "per_round_summary.csv"
    summary_path = campaign_dir / "summary.csv"

    runs = expand_runs(scenario)
    all_rows: list[dict[str, Any]] = []

    with raw_path.open("w", encoding="utf-8") as raw_file:
        for run_index, run_config in enumerate(runs):
            rows = run_one_network(run_config, campaign_dir, run_index)
            all_rows.extend(rows)
            for row in rows:
                raw_file.write(json.dumps(row, separators=(",", ":")) + "\n")
                raw_file.flush()

    write_per_query_metrics(per_query_path, all_rows)
    write_summary(per_round_summary_path, all_rows, include_round=True)
    write_summary(summary_path, all_rows, include_round=False)
    print(f"Benchmark complete: {campaign_dir}", flush=True)
    print(f"Raw results: {raw_path}", flush=True)
    print(f"Per-query metrics: {per_query_path}", flush=True)
    print(f"Per-round summary: {per_round_summary_path}", flush=True)
    print(f"Final summary: {summary_path}", flush=True)


def expand_runs(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    num_nodes_values = as_list(scenario.get("num_nodes", 5))
    dropout_values = as_list(scenario.get("dropout_rate", 0.0))
    policy_values = as_list(scenario.get("routing_policy", "current"))
    rounds = int(scenario.get("rounds", 1))
    runs: list[dict[str, Any]] = []

    for num_nodes in num_nodes_values:
        for dropout_rate in dropout_values:
            for policy in policy_values:
                for round_index in range(rounds):
                    run = dict(scenario)
                    run["num_nodes"] = int(num_nodes)
                    run["dropout_rate"] = float(dropout_rate)
                    run["routing_policy"] = str(policy)
                    run["round"] = round_index + 1
                    run["rounds"] = rounds
                    runs.append(run)

    return runs


def validate_scenario(scenario: dict[str, Any]) -> None:
    runs = expand_runs(scenario)
    if not runs:
        raise ValueError("Scenario expands to zero runs.")

    for config in runs:
        parse_latency(config.get("network_latency_ms", [0, 0]))
        select_entry_node(
            entry_nodes=[NodeProcess(0, 0, 0, ENTRY_CAPABILITY, "validation", Path("."), process=None)],  # type: ignore[arg-type]
            strategy=str(config.get("entry_node_strategy", "round_robin")),
            query_index=0,
            rng=random.Random(round_seed(config)),
        )
        for query_spec in as_list(config.get("query_capability", CAPS_POOL)):
            normalize_query_spec(query_spec)


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def run_one_network(
    config: dict[str, Any],
    campaign_dir: Path,
    run_index: int,
) -> list[dict[str, Any]]:
    nodes: list[NodeProcess] = []
    run_name = run_label(config, run_index)
    run_dir = campaign_dir / run_name
    log_dir = run_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    try:
        nodes = start_network(config, log_dir, run_index)
        warmup_s = float(config.get("warmup_s", 8.0))
        print(f"[{run_name}] warmup {warmup_s:.1f}s", flush=True)
        time.sleep(warmup_s)

        dropped = apply_dropout(nodes, config)
        profiles = fetch_all_profiles(nodes)
        active_profiles = {
            profile["peer_id"]: profile
            for profile in profiles
            if profile["peer_id"] not in {node.peer_id for node in dropped}
        }

        rows = execute_queries(config, nodes, active_profiles, dropped, run_name)
        for row in rows:
            row["run_name"] = run_name
            row["round"] = int(config.get("round", 1))
            row["rounds"] = int(config.get("rounds", 1))
            row["run_config"] = compact_run_config(config)
        return rows
    finally:
        stop_nodes(nodes)


def start_network(
    config: dict[str, Any],
    log_dir: Path,
    run_index: int,
) -> list[NodeProcess]:
    num_nodes = int(config["num_nodes"])
    api_host = str(config.get("api_host", optional_env("API_HOST", "127.0.0.1")))
    base_port = int(config.get("base_port", optional_env("BASE_PORT", "8002")))
    api_base_port = int(config.get("api_base_port", optional_env("API_BASE_PORT", "9002")))
    port_offset = int(config.get("port_offset", 100 * run_index))
    seed_base = round_seed(config)
    latency = config.get("network_latency_ms", [0, 0])
    latency_min_ms, latency_max_ms = parse_latency(latency)
    slow_nodes = select_slow_nodes(num_nodes, config, seed_base)

    nodes: list[NodeProcess] = []
    bootstrap_addr = None

    for index in range(num_nodes):
        port = base_port + port_offset + index
        api_port = api_base_port + port_offset + index
        seed = 1000 + index + 10000 * (int(config.get("round", 1)) - 1)
        capability = ENTRY_CAPABILITY if index == 0 else CAPS_POOL[index % len(CAPS_POOL)]
        model_name = "node-0-entry" if index == 0 else f"node-{index}-{capability}"
        log_path = log_dir / f"node_{index}.log"
        extra_latency = (
            float(config.get("slow_node_extra_latency_ms", 0.0))
            if index in slow_nodes
            else 0.0
        )

        command = [
            sys.executable,
            "-m",
            "src.cli.run_node",
            "--port",
            str(port),
            "--api-port",
            str(api_port),
            "--seed",
            str(seed),
            "--capabilities",
            capability,
            "--model-name",
            model_name,
            "--system-prompt",
            benchmark_system_prompt(capability),
        ]
        if bootstrap_addr is not None:
            command.extend(["--bootstrap", bootstrap_addr])

        env = os.environ.copy()
        env.update(
            {
                "REQUEST_BACKEND": "dummy",
                "CLASSIFIER_BACKEND": "dummy",
                "ROUTING_POLICY": str(config.get("routing_policy", "current")),
                "BENCHMARK_MODE": "true",
                "BENCHMARK_NETWORK_LATENCY_MIN_MS": str(latency_min_ms),
                "BENCHMARK_NETWORK_LATENCY_MAX_MS": str(latency_max_ms),
                "BENCHMARK_NODE_NETWORK_EXTRA_LATENCY_MS": str(extra_latency),
                "BENCHMARK_SEED": str(seed_base),
                "API_HOST": api_host,
            }
        )

        log_file = log_path.open("w", encoding="utf-8")
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )
        node = NodeProcess(
            index=index,
            port=port,
            api_port=api_port,
            capability=capability,
            model_name=model_name,
            log_path=log_path,
            process=process,
            api_url=f"http://{api_host}:{api_port}",
        )
        nodes.append(node)

        node.peer_id = wait_for_peer_id(node, timeout_s=float(config.get("startup_timeout_s", 45.0)))
        if index == 0:
            bootstrap_addr = f"/ip6/::1/tcp/{port}/p2p/{node.peer_id}"

        wait_for_profile(node.api_url, timeout_s=float(config.get("startup_timeout_s", 45.0)))
        print(f"started node {index}: {node.peer_id} {node.api_url}", flush=True)

    return nodes


def execute_queries(
    config: dict[str, Any],
    nodes: list[NodeProcess],
    active_profiles: dict[str, dict[str, Any]],
    dropped: list[NodeProcess],
    run_name: str,
) -> list[dict[str, Any]]:
    query_capabilities = as_list(config.get("query_capability", CAPS_POOL))
    queries_per_capability = int(config.get("queries_per_capability", 3))
    top_k = int(config.get("top_k", 3))
    entry_node_strategy = str(config.get("entry_node_strategy", "round_robin"))
    entry_nodes = available_entry_nodes(nodes, dropped)
    entry_rng = random.Random(round_seed(config) + 2029)
    rows: list[dict[str, Any]] = []
    query_index = 0

    for query_spec in query_capabilities:
        query_label, required_capabilities, prompt = normalize_query_spec(query_spec)
        for repeat in range(queries_per_capability):
            entry_node = select_entry_node(
                entry_nodes,
                entry_node_strategy,
                query_index,
                entry_rng,
            )
            query_id = f"{run_name}-{query_label}-{repeat}"
            started_at = time.perf_counter()
            reply, error = post_json(
                f"{entry_node.api_url}/api/query",
                {
                    "prompt": prompt,
                    "query_id": query_id,
                    "required_capabilities": required_capabilities,
                },
                timeout_s=float(config.get("query_timeout_s", 90.0)),
            )
            end_to_end_ms = (time.perf_counter() - started_at) * 1000.0
            row = build_result_row(
                query_id=query_id,
                capability=query_label,
                required_capabilities=required_capabilities,
                reply=reply,
                error=error,
                end_to_end_ms=end_to_end_ms,
                profiles=active_profiles,
                dropped=dropped,
                top_k=top_k,
                entry_node=entry_node,
                entry_node_strategy=entry_node_strategy,
            )
            rows.append(row)
            query_index += 1

    return rows


def available_entry_nodes(
    nodes: list[NodeProcess],
    dropped: list[NodeProcess],
) -> list[NodeProcess]:
    dropped_peer_ids = {node.peer_id for node in dropped}
    candidates = [
        node
        for node in nodes
        if node.peer_id not in dropped_peer_ids
        and node.api_url is not None
        and node.process.poll() is None
    ]
    if not candidates:
        raise RuntimeError("No active entry node available for benchmark queries")
    return candidates


def select_entry_node(
    entry_nodes: list[NodeProcess],
    strategy: str,
    query_index: int,
    rng: random.Random,
) -> NodeProcess:
    selected = strategy.strip().lower()
    if selected in {"fixed", "fixed_node_0", "node_0"}:
        for node in entry_nodes:
            if node.index == 0:
                return node
        raise RuntimeError("entry_node_strategy=fixed_node_0 but node 0 is not active")

    if selected == "round_robin":
        return entry_nodes[query_index % len(entry_nodes)]

    if selected == "random":
        return rng.choice(entry_nodes)

    raise ValueError(
        "entry_node_strategy must be one of: random, round_robin, fixed_node_0"
    )


def normalize_query_spec(query_spec: Any) -> tuple[str, dict[str, float], str]:
    if isinstance(query_spec, str):
        capability = query_spec.strip().lower()
        return (
            capability,
            {capability: 1.0},
            DEFAULT_PROMPTS.get(capability, f"Benchmark query for {capability}."),
        )

    if isinstance(query_spec, dict):
        raw_capabilities = query_spec.get("required_capabilities", query_spec)
        if not isinstance(raw_capabilities, dict):
            raise ValueError(f"Invalid query capability spec: {query_spec!r}")

        required_capabilities = {
            str(capability).strip().lower(): max(0.0, min(1.0, float(score)))
            for capability, score in raw_capabilities.items()
            if float(score) > 0.0
        }
        if not required_capabilities:
            raise ValueError(f"Query spec has no positive capabilities: {query_spec!r}")

        label = str(query_spec.get("name") or capability_label(required_capabilities))
        prompt = str(query_spec.get("prompt") or DEFAULT_MIXED_PROMPT)
        return label, required_capabilities, prompt

    raise ValueError(f"Unsupported query capability spec: {query_spec!r}")


def capability_label(required_capabilities: dict[str, float]) -> str:
    return "_".join(
        f"{capability}{int(score * 100)}"
        for capability, score in sorted(
            required_capabilities.items(),
            key=lambda item: item[1],
            reverse=True,
        )
    )


def build_result_row(
    query_id: str,
    capability: str,
    required_capabilities: dict[str, float],
    reply: dict[str, Any] | None,
    error: str | None,
    end_to_end_ms: float,
    profiles: dict[str, dict[str, Any]],
    dropped: list[NodeProcess],
    top_k: int,
    entry_node: NodeProcess,
    entry_node_strategy: str,
) -> dict[str, Any]:
    routing_trace = reply.get("routing_trace") if isinstance(reply, dict) else None
    selected_peer_id = selected_peer_from_trace(routing_trace)
    quality_scores = {
        peer_id: weighted_quality(profile, required_capabilities)
        for peer_id, profile in profiles.items()
    }
    ranked = sorted(quality_scores.items(), key=lambda item: item[1], reverse=True)
    oracle_peer_id = ranked[0][0] if ranked else None
    oracle_score = ranked[0][1] if ranked else 0.0
    selected_score = quality_scores.get(selected_peer_id, 0.0)
    rank = selected_rank(selected_peer_id, ranked)
    utility_oracle = utility_oracle_from_trace(routing_trace)

    return {
        "query_id": query_id,
        "query_capability": capability,
        "entry_node_strategy": entry_node_strategy,
        "entry_node_index": entry_node.index,
        "entry_peer_id": entry_node.peer_id,
        "entry_api_url": entry_node.api_url,
        "status": reply.get("status") if isinstance(reply, dict) else "error",
        "error": error,
        "selected_peer_id": selected_peer_id,
        "quality_oracle_peer_id": oracle_peer_id,
        "quality_oracle_score": round(oracle_score, 6),
        "selected_quality_score": round(selected_score, 6),
        "quality_regret": round(max(0.0, oracle_score - selected_score), 6),
        "selected_quality_ratio": (
            round(selected_score / oracle_score, 6) if oracle_score > 0.0 else None
        ),
        "top_1_accuracy": selected_peer_id == oracle_peer_id if oracle_peer_id else False,
        "top_k": top_k,
        "top_k_accuracy": rank is not None and rank <= top_k,
        f"top_{top_k}_accuracy": rank is not None and rank <= top_k,
        "selected_rank": rank,
        "utility_oracle_peer_id": utility_oracle.get("peer_id"),
        "utility_oracle_score": utility_oracle.get("score"),
        "routing_latency_ms": round(routing_latency_ms(routing_trace), 3),
        "end_to_end_latency_ms": round(end_to_end_ms, 3),
        "candidate_count": candidate_count(routing_trace),
        "selected_node_latency_ms": selected_node_latency_ms(routing_trace),
        "no_suitable_node": (
            reply.get("status") == "no_suitable_node" if isinstance(reply, dict) else False
        ),
        "success": reply.get("status") == "ok" if isinstance(reply, dict) else False,
        "dropped_peer_ids": [node.peer_id for node in dropped],
        "routing_trace": routing_trace,
    }


def weighted_quality(profile: dict[str, Any], required_capabilities: dict[str, float]) -> float:
    scores = profile.get("capability_scores") or {}
    total = sum(float(value) for value in required_capabilities.values())
    if total <= 0.0:
        return 0.0
    value = sum(
        float(demand) * float(scores.get(capability, 0.0))
        for capability, demand in required_capabilities.items()
    ) / total
    return max(0.0, min(1.0, value))


def selected_peer_from_trace(routing_trace: dict[str, Any] | None) -> str | None:
    if not isinstance(routing_trace, dict):
        return None
    answered_by = routing_trace.get("answered_by")
    if isinstance(answered_by, dict) and answered_by.get("peer_id"):
        return str(answered_by["peer_id"])

    for hop in routing_trace.get("hops", []):
        selected = hop.get("selected") if isinstance(hop, dict) else None
        peer = selected.get("peer") if isinstance(selected, dict) else None
        if isinstance(peer, dict) and peer.get("peer_id"):
            return str(peer["peer_id"])
        node = hop.get("node") if isinstance(hop, dict) else None
        if hop.get("action") == "execute_local" and isinstance(node, dict):
            return node.get("peer_id")
    return None


def selected_rank(peer_id: str | None, ranked: list[tuple[str, float]]) -> int | None:
    if peer_id is None:
        return None
    for index, (candidate_peer_id, _) in enumerate(ranked, start=1):
        if candidate_peer_id == peer_id:
            return index
    return None


def routing_latency_ms(routing_trace: dict[str, Any] | None) -> float:
    if not isinstance(routing_trace, dict):
        return 0.0
    total = 0.0
    for hop in routing_trace.get("hops", []):
        if isinstance(hop, dict):
            total += float(hop.get("routing_duration_ms") or 0.0)
    return total


def candidate_count(routing_trace: dict[str, Any] | None) -> int:
    if not isinstance(routing_trace, dict):
        return 0
    total = 0
    for hop in routing_trace.get("hops", []):
        for stage in hop.get("stages", []) if isinstance(hop, dict) else []:
            total += int(stage.get("candidate_count") or 0)
    return total


def selected_node_latency_ms(routing_trace: dict[str, Any] | None) -> float | None:
    if not isinstance(routing_trace, dict):
        return None
    for hop in routing_trace.get("hops", []):
        selected = hop.get("selected") if isinstance(hop, dict) else None
        breakdown = selected.get("score_breakdown") if isinstance(selected, dict) else None
        latency = breakdown.get("latency") if isinstance(breakdown, dict) else None
        if isinstance(latency, dict) and latency.get("value") is not None:
            return round(float(latency["value"]) * 1000.0, 3)
    return None


def utility_oracle_from_trace(routing_trace: dict[str, Any] | None) -> dict[str, Any]:
    best_peer_id = None
    best_score = -math.inf
    if not isinstance(routing_trace, dict):
        return {"peer_id": None, "score": None}

    for hop in routing_trace.get("hops", []):
        for stage in hop.get("stages", []) if isinstance(hop, dict) else []:
            for candidate in stage.get("candidates", []):
                score = candidate.get("routing_score")
                peer = candidate.get("peer") or {}
                peer_id = peer.get("peer_id")
                if candidate.get("kind") == "local":
                    node = hop.get("node") or {}
                    peer_id = node.get("peer_id")
                if score is not None and peer_id and float(score) > best_score:
                    best_score = float(score)
                    best_peer_id = str(peer_id)

    return {
        "peer_id": best_peer_id,
        "score": round(best_score, 6) if best_peer_id is not None else None,
    }


def apply_dropout(nodes: list[NodeProcess], config: dict[str, Any]) -> list[NodeProcess]:
    dropout_rate = float(config.get("dropout_rate", 0.0))
    if dropout_rate <= 0.0:
        return []

    candidates = nodes[1:]
    count = min(len(candidates), int(round(len(candidates) * dropout_rate)))
    rng = random.Random(round_seed(config) + 991)
    dropped = rng.sample(candidates, count) if count > 0 else []
    for node in dropped:
        terminate_process(node.process)
        print(f"dropped node {node.index}: {node.peer_id}", flush=True)
    settle_s = float(config.get("dropout_settle_s", 2.0))
    if dropped and settle_s > 0.0:
        time.sleep(settle_s)
    return dropped


def select_slow_nodes(num_nodes: int, config: dict[str, Any], seed: int) -> set[int]:
    fraction = float(config.get("slow_node_fraction", 0.0))
    extra = float(config.get("slow_node_extra_latency_ms", 0.0))
    if fraction <= 0.0 or extra <= 0.0:
        return set()

    candidates = list(range(1, num_nodes))
    count = min(len(candidates), int(round(len(candidates) * fraction)))
    return set(random.Random(seed + 313).sample(candidates, count))


def fetch_all_profiles(nodes: list[NodeProcess]) -> list[dict[str, Any]]:
    profiles = []
    for node in nodes:
        if node.process.poll() is not None or node.api_url is None:
            continue
        profile, error = get_json(f"{node.api_url}/api/profile", timeout_s=5.0)
        if error is None and isinstance(profile, dict):
            profiles.append(profile)
    return profiles


def wait_for_peer_id(node: NodeProcess, timeout_s: float) -> str:
    deadline = time.time() + timeout_s
    pattern = re.compile(r"\[NODE\] I am ([^ \r\n]+)")
    while time.time() < deadline:
        if node.process.poll() is not None:
            raise RuntimeError(f"node {node.index} exited early; see {node.log_path}")
        if node.log_path.exists():
            text = node.log_path.read_text(encoding="utf-8", errors="replace")
            match = pattern.search(text)
            if match:
                return match.group(1)
        time.sleep(0.25)
    raise TimeoutError(f"timed out waiting for peer id in {node.log_path}")


def wait_for_profile(api_url: str | None, timeout_s: float) -> None:
    if api_url is None:
        raise RuntimeError("node api_url is missing")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        _, error = get_json(f"{api_url}/api/profile", timeout_s=1.0)
        if error is None:
            return
        time.sleep(0.5)
    raise TimeoutError(f"timed out waiting for {api_url}/api/profile")


def post_json(
    url: str,
    payload: dict[str, Any],
    timeout_s: float,
) -> tuple[dict[str, Any] | None, str | None]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return json.loads(response.read().decode("utf-8")), None
    except Exception as exc:
        return None, str(exc)


def get_json(url: str, timeout_s: float) -> tuple[dict[str, Any] | None, str | None]:
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            return json.loads(response.read().decode("utf-8")), None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return None, str(exc)


def write_summary(
    path: Path,
    rows: list[dict[str, Any]],
    *,
    include_round: bool,
) -> None:
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row in rows:
        config = row.get("run_config", {})
        key_parts = [
            str(config.get("num_nodes")),
            str(config.get("network_latency_ms")),
            str(config.get("dropout_rate")),
            str(config.get("routing_policy")),
            str(config.get("entry_node_strategy", "round_robin")),
            str(config.get("slow_node_fraction")),
            str(config.get("slow_node_extra_latency_ms")),
        ]
        if include_round:
            key_parts.append(str(row.get("round", 1)))
        key = tuple(key_parts)
        groups.setdefault(key, []).append(row)

    fields = [
        "num_nodes",
        "network_latency_ms",
        "dropout_rate",
        "routing_policy",
        "entry_node_strategy",
        "slow_node_fraction",
        "slow_node_extra_latency_ms",
        "round",
        "query_count",
        "success_rate",
        "no_suitable_node_rate",
        "top_1_accuracy",
        "avg_quality_regret",
        "avg_quality_ratio",
        "avg_routing_latency_ms",
        "p95_end_to_end_latency_ms",
        "avg_candidate_count",
    ]
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for key, group in sorted(groups.items()):
            (
                num_nodes,
                network_latency_ms,
                dropout_rate,
                routing_policy,
                entry_node_strategy,
                slow_node_fraction,
                slow_node_extra_latency_ms,
                *round_part,
            ) = key
            writer.writerow(
                {
                    "num_nodes": num_nodes,
                    "network_latency_ms": network_latency_ms,
                    "dropout_rate": dropout_rate,
                    "routing_policy": routing_policy,
                    "entry_node_strategy": entry_node_strategy,
                    "slow_node_fraction": slow_node_fraction,
                    "slow_node_extra_latency_ms": slow_node_extra_latency_ms,
                    "round": round_part[0] if include_round and round_part else "all",
                    "query_count": len(group),
                    "success_rate": avg_bool(row["success"] for row in group),
                    "no_suitable_node_rate": avg_bool(
                        row["no_suitable_node"] for row in group
                    ),
                    "top_1_accuracy": avg_bool(row["top_1_accuracy"] for row in group),
                    "avg_quality_regret": avg(row["quality_regret"] for row in group),
                    "avg_quality_ratio": avg_optional(
                        row["selected_quality_ratio"] for row in group
                    ),
                    "avg_routing_latency_ms": avg(
                        row["routing_latency_ms"] for row in group
                    ),
                    "p95_end_to_end_latency_ms": percentile(
                        [row["end_to_end_latency_ms"] for row in group],
                        95,
                    ),
                    "avg_candidate_count": avg(row["candidate_count"] for row in group),
                }
            )


def write_per_query_metrics(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "run_name",
        "round",
        "rounds",
        "num_nodes",
        "network_latency_ms",
        "dropout_rate",
        "routing_policy",
        "entry_node_strategy",
        "slow_node_fraction",
        "slow_node_extra_latency_ms",
        "query_id",
        "query_capability",
        "entry_node_index",
        "entry_peer_id",
        "entry_api_url",
        "status",
        "success",
        "no_suitable_node",
        "error",
        "selected_peer_id",
        "quality_oracle_peer_id",
        "quality_oracle_score",
        "selected_quality_score",
        "quality_regret",
        "selected_quality_ratio",
        "top_1_accuracy",
        "top_k",
        "top_k_accuracy",
        "top_3_accuracy",
        "selected_rank",
        "utility_oracle_peer_id",
        "utility_oracle_score",
        "routing_latency_ms",
        "end_to_end_latency_ms",
        "candidate_count",
        "selected_node_latency_ms",
        "dropped_peer_ids",
    ]

    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            config = row.get("run_config", {})
            output = {
                **row,
                "num_nodes": config.get("num_nodes"),
                "network_latency_ms": config.get("network_latency_ms"),
                "dropout_rate": config.get("dropout_rate"),
                "routing_policy": config.get("routing_policy"),
                "entry_node_strategy": config.get("entry_node_strategy", "round_robin"),
                "slow_node_fraction": config.get("slow_node_fraction"),
                "slow_node_extra_latency_ms": config.get(
                    "slow_node_extra_latency_ms"
                ),
                "dropped_peer_ids": ",".join(
                    peer_id
                    for peer_id in row.get("dropped_peer_ids", [])
                    if peer_id is not None
                ),
            }
            writer.writerow(output)


def avg(values) -> float:
    values = [float(value) for value in values]
    return round(sum(values) / len(values), 6) if values else 0.0


def avg_optional(values) -> float | None:
    selected = [float(value) for value in values if value is not None]
    return round(sum(selected) / len(selected), 6) if selected else None


def avg_bool(values) -> float:
    selected = [1.0 if value else 0.0 for value in values]
    return round(sum(selected) / len(selected), 6) if selected else 0.0


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = min(len(ordered) - 1, math.ceil((p / 100.0) * len(ordered)) - 1)
    return round(ordered[index], 6)


def parse_latency(value: Any) -> tuple[float, float]:
    if isinstance(value, list) and len(value) == 2:
        return float(value[0]), float(value[1])
    if isinstance(value, (int, float)):
        return float(value), float(value)
    if isinstance(value, str) and "-" in value:
        lower, upper = value.split("-", 1)
        return float(lower), float(upper)
    return 0.0, 0.0


def benchmark_system_prompt(capability: str) -> str:
    return (
        f"You are a benchmark dummy node for {capability}. "
        "Return concise placeholder answers."
    )


def compact_run_config(config: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "num_nodes",
        "network_latency_ms",
        "dropout_rate",
        "routing_policy",
        "entry_node_strategy",
        "slow_node_fraction",
        "slow_node_extra_latency_ms",
        "query_capability",
        "round",
        "rounds",
    ]
    return {key: config.get(key) for key in keys if key in config}


def run_label(config: dict[str, Any], run_index: int) -> str:
    latency_min, latency_max = parse_latency(config.get("network_latency_ms", [0, 0]))
    return (
        f"run{run_index:03d}_n{config['num_nodes']}"
        f"_r{int(config.get('round', 1)):02d}"
        f"_drop{int(float(config.get('dropout_rate', 0.0)) * 100)}"
        f"_{config.get('routing_policy', 'current')}"
        f"_lat{int(latency_min)}-{int(latency_max)}"
    )


def round_seed(config: dict[str, Any]) -> int:
    base_seed = int(config.get("seed", 42))
    round_index = int(config.get("round", 1)) - 1
    return base_seed + 1009 * round_index


def stop_nodes(nodes: list[NodeProcess]) -> None:
    for node in reversed(nodes):
        terminate_process(node.process)


def terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5.0)


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    policy_values = as_list(data.get("routing_policy", "current"))
    for policy in policy_values:
        get_routing_policy(str(policy))
    return data


if __name__ == "__main__":
    main()
