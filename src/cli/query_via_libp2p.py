"""
One-shot libp2p query client.

This client is NOT a network node.
It only:
- starts a temporary host
- connects to one selected entry node
- opens one /tsadai/query/1.0.0 stream
- sends one query
- waits for one response
- exits

It does not run:
- DHT
- PubSub
- ping/query handlers
- routing logic
"""

import argparse
import random
import secrets
import sys
import traceback
import uuid

import multiaddr
import trio
from libp2p import new_host
from libp2p.crypto.secp256k1 import create_new_key_pair

from src.network_utils import connect_to_peer
from src.protocols import QUERY_PROTOCOL
from src.transport import TransportService
from src.env_config import env_float, load_project_env


def make_temp_host(seed: int | None = None):
    if seed is not None:
        random.seed(seed)
        secret_number = random.getrandbits(32 * 8)
        secret = secret_number.to_bytes(length=32, byteorder="big")
    else:
        secret = secrets.token_bytes(32)

    return new_host(key_pair=create_new_key_pair(secret))


async def async_main(args):
    host = make_temp_host(seed=args.seed)
    transport = TransportService()

    # Listen on one clean local loopback address only.
    listen_addrs = [multiaddr.Multiaddr("/ip6/::1/tcp/0")]

    async with host.run(listen_addrs=listen_addrs):
        info = await connect_to_peer(host, args.entry_node)

        query_id = str(uuid.uuid4())
        stream = None

        try:
            with trio.fail_after(args.timeout):
                stream = await transport.open_stream(
                    host,
                    info.peer_id,
                    QUERY_PROTOCOL,
                )

                payload = {
                    "type": "query",
                    "query_id": query_id,
                    "prompt": args.prompt,
                    "query_context": {
                        # Do not expose the real temporary client peer id
                        # as a routable network participant.
                        "origin_peer_id": "external-client",
                    },
                }

                await transport.send_message(stream, payload, role="CLIENT")
                reply = await transport.receive_message(stream, role="CLIENT")

            if reply.get("type") != "response":
                print("\nQUERY FAILED", flush=True)
                print(f"unexpected response type: {reply.get('type')}", flush=True)
                return

            if reply.get("query_id") != query_id:
                print("\nQUERY FAILED", flush=True)
                print("query_id mismatch", flush=True)
                return

            print("\nQUERY OK", flush=True)
            print(reply.get("answer"), flush=True)

        except trio.TooSlowError:
            print("\nQUERY FAILED", flush=True)
            print(f"timeout after {args.timeout:.2f}s", flush=True)
        except Exception as exc:
            print("\nQUERY FAILED", flush=True)
            print(str(exc), flush=True)
        finally:
            if stream is not None:
                try:
                    await stream.close()
                except Exception:
                    pass


def main():
    try:
        load_project_env()
        timeout = env_float("CLIENT_QUERY_TIMEOUT")
    except RuntimeError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="Send one query to the network through a libp2p query stream."
    )
    parser.add_argument(
        "--entry-node",
        required=True,
        help="Full multiaddr of the entry node",
    )
    parser.add_argument(
        "--prompt",
        required=True,
        help="Prompt to send",
    )
    parser.add_argument(
        "-s",
        "--seed",
        type=int,
        default=None,
        help="Optional deterministic seed",
    )
    args = parser.parse_args()
    args.timeout = timeout

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
