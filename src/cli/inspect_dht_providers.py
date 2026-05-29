"""
Inspect DHT capability providers.

This debug CLI:
- starts a temporary libp2p host
- connects to one or more bootstrap peers
- asks the DHT for providers of a capability
- fetches and prints their profiles directly from the providers
- exits
"""

import argparse
import sys
import traceback

import trio
from libp2p.kad_dht.kad_dht import DHTMode

from src.node import Node
from src.network_utils import connect_to_bootstrap_peers
from src.env_config import load_project_env, require_env


async def async_main(args) -> None:
    node = Node(
        port=args.port,
        model_name="dht-debug-client",
        advertised_capabilities=["debug"],
        dht_mode=DHTMode.CLIENT,
        enable_gossip=False,
        advertise_address_mode=args.advertise_address_mode,
    )

    async with node.host.run(listen_addrs=node.listen_addrs):
        async with node.dht_service.run():
            await connect_to_bootstrap_peers(node.host, args.bootstrap)

            providers = await node.dht_service.find_capability_providers(args.capability)

            print()
            print(
                f"Found {len(providers)} provider id(s) "
                f"for capability={args.capability!r}"
            )

            for provider in providers:
                peer_id = provider.peer_id.to_string()
                addresses = [
                    f"{address}/p2p/{peer_id}"
                    for address in getattr(provider, "addrs", [])
                ]
                print()
                print(f"peer_id: {peer_id}")

                profile = await node.profile_service.request_profile(
                    provider.peer_id,
                    addresses=addresses,
                )
                if profile is None:
                    print("profile: <direct request failed>")
                    continue

                print(f"model: {profile.model_name}")
                print(f"advertised_capabilities: {profile.advertised_capabilities}")
                print(f"is_available: {profile.is_available}")
                print(f"timestamp_ms: {profile.timestamp_ms}")
                print("addresses:")
                for address in profile.addresses:
                    print(f"  {address}")


def main() -> None:
    try:
        load_project_env()
        advertise_address_mode = require_env("ADVERTISE_ADDRESS_MODE")
        if advertise_address_mode not in {"ipv6_loopback"}:
            raise RuntimeError("ADVERTISE_ADDRESS_MODE must be one of: ipv6_loopback")
    except RuntimeError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="Inspect nodes advertising a capability through the DHT."
    )
    parser.add_argument(
        "--bootstrap",
        nargs="+",
        required=True,
        help="One or more bootstrap peer multiaddrs.",
    )
    parser.add_argument(
        "--capability",
        required=True,
        help="Capability to search for, for example: general, math, code.",
    )
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        default=0,
        help="Temporary local port for the debug host.",
    )

    args = parser.parse_args()
    args.advertise_address_mode = advertise_address_mode

    try:
        trio.run(async_main, args)
    except KeyboardInterrupt:
        print("\nStopped.")
    except BaseException as exc:
        print("\n=== FULL EXCEPTION ===", file=sys.stderr, flush=True)
        traceback.print_exception(type(exc), exc, exc.__traceback__)
        sys.exit(1)


if __name__ == "__main__":
    main()
