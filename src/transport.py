"""
Generic transport helpers.

This file hides the low-level details of:
- opening a stream
- sending JSON
- receiving JSON
"""

import json
from typing import Any

from libp2p.network.stream.net_stream import INetStream
from libp2p.abc import IHost
from libp2p.peer.id import ID
from libp2p.custom_types import TProtocol

from src.logging_utils import log
from src.benchmark_simulator import BenchmarkNetworkSimulator

MAX_READ_LEN = 2**32 - 1


class TransportService:
    def __init__(self, benchmark_simulator: BenchmarkNetworkSimulator | None = None) -> None:
        self.benchmark_simulator = benchmark_simulator

    async def open_stream(self, host: IHost, peer_id: ID, protocol_id: TProtocol):
        """
        Open a stream to a remote peer for one specific protocol.
        """
        if self.benchmark_simulator is not None:
            await self.benchmark_simulator.wait("transport.open_stream")

        log("CLIENT", f"Opening stream to peer={peer_id} with protocol={protocol_id}")
        stream = await host.new_stream(peer_id, [protocol_id])
        log("CLIENT", f"Stream opened to peer={peer_id} with protocol={protocol_id}")
        return stream

    async def send_message(
        self, stream: INetStream, payload: dict[str, Any], *, role: str = "SEND"
    ) -> None:
        """
        Send one JSON message followed by a newline.
        """
        if self.benchmark_simulator is not None:
            await self.benchmark_simulator.wait("transport.send_message")

        data = (json.dumps(payload) + "\n").encode("utf-8")
        log(role, f"SEND -> {payload}")
        await stream.write(data)

    async def receive_message(
        self, stream: INetStream, *, role: str = "RECV"
    ) -> dict[str, Any]:
        """
        Read one message from the stream and decode JSON.

        WARNING: For this starter version we assume one full JSON message is read at once.
        NOTE: Implement proper message framing when needed, if needed.
        """
        if self.benchmark_simulator is not None:
            await self.benchmark_simulator.wait("transport.receive_message")

        raw = await stream.read(MAX_READ_LEN)
        if not raw:
            raise RuntimeError("received empty payload")

        message = json.loads(raw.decode("utf-8").strip())
        log(role, f"RECV <- {message}")
        return message
