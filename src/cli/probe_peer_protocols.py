"""
One-shot peer protocol probe.

This script is not a node that stays alive forever.
It starts a temporary host, connects to an existing node,
sends one ping or one query, prints the result, then exits.

That is useful for manually testing low-level peer protocols.
"""

import argparse
import sys

import trio

from src.network_utils import connect_to_peer
from src.node import Node
from src.env_config import env_float, load_project_env


async def async_main(args):
    node = Node(port=args.port, seed=args.seed)

    async with (
        node.host.run(listen_addrs=node.listen_addrs),
        trio.open_nursery() as nursery,
    ):
        nursery.start_soon(node.host.get_peerstore().start_cleanup_task, 60)

        info = await connect_to_peer(node.host, args.destination)
        print(f"Connected to peer: {info.peer_id}")

        if args.mode == "ping":
            result = await node.ping_service.ping_peer(
                node.host, info.peer_id, timeout_s=args.timeout
            )
            if result.ok:
                print(f"PING OK  peer={result.peer_id}  rtt_ms={result.rtt_ms:.2f}")
            else:
                print(f"PING FAILED  peer={result.peer_id}  error={result.error}")

        elif args.mode == "query":
            result = await node.query_service.query_peer(
                info.peer_id,
                prompt=args.prompt,
                timeout_s=args.query_timeout,
            )
            if result.ok:
                print(f"QUERY OK  peer={result.peer_id}")
                print(f"Answer: {result.answer}")
            else:
                print(f"QUERY FAILED  peer={result.peer_id}  error={result.error}")


def main():
    try:
        load_project_env()
        ping_timeout = env_float("PING_TIMEOUT")
        query_timeout = env_float("QUERY_TIMEOUT")
    except RuntimeError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="Probe one node with a low-level ping or query protocol call."
    )
    parser.add_argument("--mode", choices=["ping", "query"], required=True)
    parser.add_argument(
        "-d", "--destination", required=True, help="Full remote multiaddr /p2p/..."
    )
    parser.add_argument(
        "-p", "--port", type=int, default=0, help="Temporary local port"
    )
    parser.add_argument(
        "--prompt", type=str, default="Hello node", help="Prompt for query mode"
    )
    parser.add_argument(
        "-s", "--seed", type=int, default=None, help="Optional deterministic seed"
    )
    args = parser.parse_args()
    args.timeout = ping_timeout
    args.query_timeout = query_timeout

    try:
        trio.run(async_main, args)
    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception as exc:
        print(f"Fatal error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
