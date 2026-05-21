from contextlib import asynccontextmanager

import trio
from libp2p.abc import IHost
from libp2p.kad_dht.kad_dht import DHTMode, KadDHT
from libp2p.peer.peerinfo import PeerInfo
from libp2p.tools.async_service import background_trio_service

from src.logging_utils import log
from src.models import NodeProfile
from src.benchmark_simulator import BenchmarkNetworkSimulator


DHT_OPERATION_TIMEOUT_S = 30.0


class DHTService:
    """
    Kad-DHT service for:
    - advertising capability providers
    - finding providers for a capability
    """

    def __init__(
        self,
        host: IHost,
        mode: DHTMode = DHTMode.SERVER,
        benchmark_simulator: BenchmarkNetworkSimulator | None = None,
    ) -> None:
        self.host = host
        self.mode = mode
        self.benchmark_simulator = benchmark_simulator
        self.dht = KadDHT(host, mode, enable_random_walk=(mode == DHTMode.SERVER))

    def capability_key(self, capability: str) -> str:
        return f"capability:{capability}"

    @asynccontextmanager
    async def run(self):
        # Seed the DHT with any peers that were already known before startup.
        for peer_id in self.host.get_peerstore().peer_ids():
            added = await self.dht.add_peer(peer_id)
            log("DHT", f"Seeded known peer peer_id={peer_id} added={added}")

        # Start DHT in background manually
        async with background_trio_service(self.dht):
            log("DHT", f"DHT service started in mode={self.mode}")
            yield self

    async def add_bootstrap_peers(self, bootstrap_peers: list[PeerInfo]) -> None:
        """
        Seed the DHT routing table with peers we successfully bootstrapped to.
        """
        for info in bootstrap_peers:
            added = await self.dht.add_peer(info.peer_id)
            log("DHT", f"Added bootstrap peer peer_id={info.peer_id} added={added}")

        if bootstrap_peers:
            await self.refresh_routing_table()

    async def refresh_routing_table(
        self,
        timeout_s: float = DHT_OPERATION_TIMEOUT_S,
    ) -> bool:
        with trio.move_on_after(timeout_s) as cancel_scope:
            await self.dht.refresh_routing_table()

        if cancel_scope.cancelled_caught:
            log("DHT", f"Routing table refresh timed out after {timeout_s:.1f}s")
            return False

        return True

    async def advertise_capabilities(
        self,
        profile: NodeProfile,
        timeout_s: float = DHT_OPERATION_TIMEOUT_S,
    ) -> dict[str, bool]:
        results: dict[str, bool] = {}

        for capability in profile.advertised_capabilities:
            key = self.capability_key(capability)
            ok = False

            with trio.move_on_after(timeout_s) as cancel_scope:
                if self.benchmark_simulator is not None:
                    await self.benchmark_simulator.wait("dht.advertise_capability")
                ok = await self.dht.provide(key)

            if cancel_scope.cancelled_caught:
                log(
                    "DHT",
                    f"Advertise capability timed out capability={capability} "
                    f"after {timeout_s:.1f}s",
                )
                results[capability] = False
                continue

            log("DHT", f"Advertised capability={capability} ok={ok}")
            results[capability] = ok

        return results

    async def find_capability_providers(
        self,
        capability: str,
        timeout_s: float = DHT_OPERATION_TIMEOUT_S,
    ):
        key = self.capability_key(capability)
        providers = []

        with trio.move_on_after(timeout_s) as cancel_scope:
            if self.benchmark_simulator is not None:
                await self.benchmark_simulator.wait("dht.find_capability_providers")
            providers = await self.dht.find_providers(key)

        if cancel_scope.cancelled_caught:
            log(
                "DHT",
                f"Find providers timed out capability={capability} "
                f"after {timeout_s:.1f}s",
            )
            return []

        return providers

    async def find_capability_provider_ids(self, capability: str) -> list[str]:
        """
        Return unique provider peer IDs as strings for a given capability.
        """
        providers = await self.find_capability_providers(capability)

        peer_ids: list[str] = []
        for provider in providers:
            try:
                if hasattr(provider.peer_id, "to_string"):
                    peer_ids.append(provider.peer_id.to_string())
                else:
                    peer_ids.append(str(provider.peer_id))
            except Exception:
                continue

        peer_ids = list(dict.fromkeys(peer_ids))

        log(
            "DHT",
            f"Provider ID list for capability={capability}: peer_ids={peer_ids}",
        )
        return peer_ids
