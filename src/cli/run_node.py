"""
Start one node and keep it alive.
"""

import argparse
import sys
import traceback
import json

import trio
from libp2p.kad_dht.kad_dht import DHTMode

from src.env_config import (
    env_bool,
    env_float,
    env_int,
    load_project_env,
    require_env,
)
from src.ollama_utils import OllamaError, check_ollama_ready


def configure_from_env(args) -> None:
    args.advertise_address_mode = require_env("ADVERTISE_ADDRESS_MODE")
    if args.advertise_address_mode not in {"ipv6_loopback"}:
        raise RuntimeError(
            "ADVERTISE_ADDRESS_MODE must be one of: ipv6_loopback"
        )

    args.agent_backend = require_env("REQUEST_BACKEND")
    if args.agent_backend not in {"dummy", "ollama", "local", "aiass"}:
        raise RuntimeError("REQUEST_BACKEND must be one of: dummy, ollama, local, aiass")

    args.classifier_backend = require_env("CLASSIFIER_BACKEND")
    if args.classifier_backend not in {"dummy", "ollama", "local", "aiass"}:
        raise RuntimeError(
            "CLASSIFIER_BACKEND must be one of: dummy, ollama, local, aiass"
        )

    args.query_timeout = env_float("QUERY_TIMEOUT")
    args.query_connect_timeout = env_float("QUERY_CONNECT_TIMEOUT")
    args.classifier_timeout = env_float("CLASSIFIER_TIMEOUT")
    args.api_host = require_env("API_HOST")
    args.routing_policy = require_env("ROUTING_POLICY")
    args.capability_advertise_interval_s = env_float(
        "CAPABILITY_ADVERTISE_INTERVAL_S"
    )
    args.benchmark_mode = env_bool("BENCHMARK_MODE")
    args.benchmark_network_latency_min_ms = env_float(
        "BENCHMARK_NETWORK_LATENCY_MIN_MS"
    )
    args.benchmark_network_latency_max_ms = env_float(
        "BENCHMARK_NETWORK_LATENCY_MAX_MS"
    )
    args.benchmark_node_network_extra_latency_ms = env_float(
        "BENCHMARK_NODE_NETWORK_EXTRA_LATENCY_MS"
    )
    args.benchmark_seed = env_int("BENCHMARK_SEED")

    args.local_model_id = require_env("LOCAL_MODEL_ID")
    args.local_classifier_model_id = require_env("LOCAL_CLASSIFIER_MODEL_ID")
    args.local_max_new_tokens = env_int("LOCAL_MAX_NEW_TOKENS")
    args.local_enable_thinking = env_bool("LOCAL_ENABLE_THINKING")
    args.local_timeout = env_float("LOCAL_TIMEOUT")
    args.local_system_prompt = args.system_prompt

    args.ollama_model = require_env("OLLAMA_MODEL")
    args.ollama_classifier_model = require_env("OLLAMA_CLASSIFIER_MODEL")
    args.ollama_host = require_env("OLLAMA_HOST")
    args.ollama_timeout = env_float("OLLAMA_TIMEOUT")
    args.ollama_num_predict = env_int("OLLAMA_NUM_PREDICT")
    args.ollama_check_timeout = env_float("OLLAMA_CHECK_TIMEOUT")
    args.ollama_system_prompt = args.system_prompt

    args.aiass_model = require_env("AIASS_MODEL")
    args.aiass_classifier_model = require_env("AIASS_CLASSIFIER_MODEL")
    args.aiass_base_url = require_env("AIASS_BASE_URL")
    args.aiass_api_key = require_env("AIASS_API_KEY")
    args.aiass_timeout = env_float("AIASS_TIMEOUT")
    args.aiass_max_tokens = env_int("AIASS_MAX_TOKENS")
    args.aiass_system_prompt = args.system_prompt


async def async_main(args):
    advertised_capabilities = [
        c.strip() for c in args.capabilities.split(",") if c.strip()
    ]
    capability_scores = (
        json.loads(args.capability_scores) if args.capability_scores else None
    )

    if args.agent_backend == "ollama":
        try:
            check_ollama_ready(
                args.ollama_host,
                args.ollama_model,
                timeout_s=args.ollama_check_timeout,
            )
        except OllamaError as exc:
            raise RuntimeError(str(exc)) from exc
    if args.classifier_backend == "ollama":
        try:
            check_ollama_ready(
                args.ollama_host,
                args.ollama_classifier_model,
                timeout_s=args.ollama_check_timeout,
            )
        except OllamaError as exc:
            raise RuntimeError(str(exc)) from exc
    if (
        args.agent_backend == "aiass"
        or args.classifier_backend == "aiass"
    ) and (
        not args.aiass_api_key
        or args.aiass_api_key == "YOUR_AIASS_API_KEY_HERE"
    ):
        raise RuntimeError(
            "AIaaS API key is missing. Set AIASS_API_KEY in .env."
        )

    dht_mode = DHTMode.SERVER if args.dht_mode == "server" else DHTMode.CLIENT

    from src.node import Node

    node = Node(
        port=args.port,
        seed=args.seed,
        model_name=args.model_name,
        advertised_capabilities=advertised_capabilities,
        capability_scores=capability_scores,
        dht_mode=dht_mode,
        advertise_address_mode=args.advertise_address_mode,
        enable_gossip=args.enable_gossip,
        agent_backend=args.agent_backend,
        classifier_backend=args.classifier_backend,
        local_model_id=args.local_model_id,
        local_classifier_model_id=args.local_classifier_model_id,
        local_max_new_tokens=args.local_max_new_tokens,
        local_enable_thinking=args.local_enable_thinking,
        local_timeout=args.local_timeout,
        local_system_prompt=args.local_system_prompt,
        ollama_model=args.ollama_model,
        ollama_classifier_model=args.ollama_classifier_model,
        ollama_host=args.ollama_host,
        ollama_timeout=args.ollama_timeout,
        ollama_num_predict=args.ollama_num_predict,
        ollama_system_prompt=args.ollama_system_prompt,
        aiass_model=args.aiass_model,
        aiass_classifier_model=args.aiass_classifier_model,
        aiass_base_url=args.aiass_base_url,
        aiass_api_key=args.aiass_api_key,
        aiass_timeout=args.aiass_timeout,
        aiass_max_tokens=args.aiass_max_tokens,
        aiass_system_prompt=args.aiass_system_prompt,
        classifier_timeout=args.classifier_timeout,
        query_timeout=args.query_timeout,
        query_connect_timeout=args.query_connect_timeout,
        routing_policy=args.routing_policy,
        capability_advertise_interval_s=args.capability_advertise_interval_s,
        benchmark_mode=args.benchmark_mode,
        benchmark_network_latency_min_ms=args.benchmark_network_latency_min_ms,
        benchmark_network_latency_max_ms=args.benchmark_network_latency_max_ms,
        benchmark_node_network_extra_latency_ms=(
            args.benchmark_node_network_extra_latency_ms
        ),
        benchmark_seed=args.benchmark_seed,
    )

    await node.run_forever(
        bootstrap_addrs=args.bootstrap,
        api_host=args.api_host,
        api_port=args.api_port,
    )


def main():
    try:
        load_project_env()
    except RuntimeError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Run one libp2p node.")
    # General args
    parser.add_argument("-p", "--port", type=int, default=0)
    parser.add_argument("-s", "--seed", type=int, default=None)
    parser.add_argument("--model-name", type=str, default="dummy-model")
    parser.add_argument("--capabilities", type=str, default="general")
    parser.add_argument("--dht-mode", choices=["server", "client"], default="server")
    parser.add_argument("--bootstrap", nargs="*", default=[])
    parser.add_argument(
        "--enable-gossip",
        action="store_true",
        help="Run the node with GossipSub. DHT discovery still works.",
    )
    parser.add_argument(
        "--capability-scores",
        type=str,
        default="",
        help=(
            'Initial JSON scores like {"math":0.85}. '
            "Normal node startup replaces these with simulated scores before DHT advertising."
        ),
    )
    parser.add_argument(
        "--api-port",
        type=int,
        default=None,
        help="Expose this node query API on the given HTTP port.",
    )
    parser.add_argument(
        "--system-prompt",
        type=str,
        default=None,
        help="Node-specific system prompt for the selected backend.",
    )

    args = parser.parse_args()
    try:
        configure_from_env(args)
    except RuntimeError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)

    try:
        trio.run(async_main, args)
    except RuntimeError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
    except BaseException as exc:
        print("\n=== FULL EXCEPTION ===", file=sys.stderr, flush=True)
        traceback.print_exception(type(exc), exc, exc.__traceback__)
        sys.exit(1)


if __name__ == "__main__":
    main()
