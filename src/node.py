import random
import secrets

import time
import trio
from libp2p import new_host
from libp2p.crypto.secp256k1 import create_new_key_pair
from libp2p.kad_dht.kad_dht import DHTMode
from libp2p.utils.address_validation import (
    find_free_port,
    get_available_interfaces,
)

from src.local_agent import AIAssAgent, DummyAgent, LocalTransformersAgent, OllamaAgent
from src.network_utils import connect_to_bootstrap_peers
from src.logging_utils import log
from src.models import NodeProfile, QueryContext
from src.peer_registry import PeerRegistry
from src.protocols import (
    PING_PROTOCOL,
    PROFILE_PROTOCOL,
    QUERY_PROTOCOL,
    RECOMMEND_PROTOCOL,
)
from src.services.dht_service import DHTService
from src.services.health_service import HealthService
from src.services.ping_service import PingService
from src.services.profile_service import ProfileService
from src.services.query_service import QueryService
from src.services.routing_service import RoutingService
from src.services.recommendation_service import RecommendationService
from src.services.routing_policy import get_routing_policy
from src.services.capability_classifier import (
    CAPABILITIES,
    CLASSIFIER_SYSTEM_PROMPT,
    CapabilityClassifier,
)
from src.services.capability_scorer import CapabilityScorer
from src.transport import TransportService
from src.benchmark_simulator import (
    BenchmarkNetworkConfig,
    BenchmarkNetworkSimulator,
)


CAPABILITY_ADVERTISE_INTERVAL_S = 30 * 60.0
MAX_ADVERTISED_CAPABILITIES = 3
class Node:
    """
    Center of logic.

    Manage the services (transport, routing, ping, query, etc.).
    Register and query the local agent LLM.
    Stores peers through the peer registery.
    """

    @staticmethod
    def resolve_profile_capabilities(
        advertised_capabilities: list[str] | None,
        capability_scores: dict[str, float] | None,
    ) -> tuple[list[str], dict[str, float]]:
        """
        Return the DHT-advertised capabilities and the complete score profile.

        The DHT indexes only the strongest capabilities. The full score profile
        is served directly through the profile protocol when a peer needs it.
        """
        explicit_capabilities = advertised_capabilities or (
            [] if capability_scores else ["general"]
        )
        scores = capability_scores or {
            capability: 1.0 for capability in explicit_capabilities
        }

        if not scores:
            explicit_capabilities = ["general"]
            scores = {"general": 1.0}

        ranked_capabilities = [
            capability
            for capability, score in sorted(
                scores.items(),
                key=lambda item: item[1],
                reverse=True,
            )
            if score > 0.0
        ]
        advertised_capabilities = list(
            dict.fromkeys(
                [*explicit_capabilities, *ranked_capabilities]
            )
        )[:MAX_ADVERTISED_CAPABILITIES]

        return advertised_capabilities, scores

    def __init__(
        self,
        port: int = 0,
        seed: int | None = None,
        model_name: str = "dummy-model",
        advertised_capabilities: list[str] | None = None,
        capability_scores: dict[str, float] | None = None,
        dht_mode: DHTMode = DHTMode.SERVER,
        advertise_address_mode: str = "ipv6_loopback",
        enable_gossip: bool = False,
        agent_backend: str = "dummy",
        classifier_backend: str = "dummy",
        local_model_id: str = "Qwen/Qwen3-1.7B",
        local_classifier_model_id: str | None = None,
        local_max_new_tokens: int = 512,
        local_enable_thinking: bool = False,
        local_timeout: float = 40.0,
        local_system_prompt: str | None = None,
        ollama_model: str = "qwen3:1.7b",
        ollama_classifier_model: str | None = None,
        ollama_host: str = "http://localhost:11434",
        ollama_timeout: float = 300.0,
        ollama_num_predict: int = 512,
        ollama_system_prompt: str | None = None,
        aiass_model: str = "Qwen/Qwen3-1.7B",
        aiass_classifier_model: str | None = None,
        aiass_base_url: str = "https://inference-rcp.epfl.ch/v1",
        aiass_api_key: str = "",
        aiass_timeout: float = 300.0,
        aiass_max_tokens: int = 512,
        aiass_system_prompt: str | None = None,
        classifier_timeout: float = 60.0,
        query_timeout: float = 60.0,
        query_connect_timeout: float = 3.0,
        routing_policy: str = "current",
        benchmark_mode: bool = False,
        benchmark_network_latency_min_ms: float = 0.0,
        benchmark_network_latency_max_ms: float = 0.0,
        benchmark_node_network_extra_latency_ms: float = 0.0,
        benchmark_seed: int = 42,
    ) -> None:
        self.port = port if port > 0 else find_free_port()
        # List of addresses from which the host will accept incoming connection
        self.listen_addrs = get_available_interfaces(self.port)
        self.advertise_address_mode = advertise_address_mode

        self.enable_gossip = enable_gossip
        self.agent_backend = agent_backend
        self.classifier_backend = classifier_backend
        self.local_model_id = local_model_id
        self.local_classifier_model_id = local_classifier_model_id or local_model_id
        self.ollama_model = ollama_model
        self.ollama_classifier_model = ollama_classifier_model or ollama_model
        self.ollama_host = ollama_host
        self.ollama_timeout = ollama_timeout
        self.local_timeout = local_timeout
        self.aiass_model = aiass_model
        self.aiass_classifier_model = aiass_classifier_model or aiass_model
        self.aiass_base_url = aiass_base_url
        self.aiass_timeout = aiass_timeout
        self.classifier_timeout = classifier_timeout
        self.query_timeout = query_timeout
        self.query_connect_timeout = query_connect_timeout
        self.routing_policy = get_routing_policy(routing_policy)
        self._capability_scored = False

        # Seed is useful for tests
        if seed is not None:
            random.seed(seed)
            secret_number = random.getrandbits(32 * 8)
            secret = secret_number.to_bytes(length=32, byteorder="big")
        else:
            secret = secrets.token_bytes(32)

        self.host = new_host(key_pair=create_new_key_pair(secret))
        benchmark_config = BenchmarkNetworkConfig(
            enabled=benchmark_mode,
            latency_min_ms=benchmark_network_latency_min_ms,
            latency_max_ms=benchmark_network_latency_max_ms,
            node_extra_latency_ms=benchmark_node_network_extra_latency_ms,
            seed=benchmark_seed,
            node_id=self.host.get_id().to_string(),
        )
        self.benchmark_simulator = BenchmarkNetworkSimulator(benchmark_config)
        self.transport = TransportService(self.benchmark_simulator)

        advertised_capabilities, resolved_scores = self.resolve_profile_capabilities(
            advertised_capabilities,
            capability_scores,
        )

        self.local_profile = NodeProfile(
            peer_id=self.host.get_id().to_string(),
            addresses=[],
            model_name=model_name,
            advertised_capabilities=advertised_capabilities,
            capability_scores=resolved_scores,
            is_available=True,
        )
        self.local_profile.addresses = self.all_shareable_addresses()

        self.peer_registry = PeerRegistry()

        # Init the model backend
        if agent_backend == "dummy":
            self.local_agent = DummyAgent()
        elif agent_backend == "ollama":
            self.local_agent = OllamaAgent(
                model=ollama_model,
                host=ollama_host,
                timeout_s=ollama_timeout,
                system_prompt=ollama_system_prompt,
                num_predict=ollama_num_predict,
            )
        elif agent_backend == "aiass":
            self.local_agent = AIAssAgent(
                model=aiass_model,
                base_url=aiass_base_url,
                api_key=aiass_api_key,
                timeout_s=aiass_timeout,
                system_prompt=aiass_system_prompt,
                max_tokens=aiass_max_tokens,
            )
        elif agent_backend == "local":
            self.local_agent = LocalTransformersAgent(
                model_id=local_model_id,
                max_new_tokens=local_max_new_tokens,
                system_prompt=local_system_prompt,
                enable_thinking=local_enable_thinking,
                timeout_s=local_timeout,
            )
        else:
            raise ValueError(f"Unsupported agent_backend: {agent_backend}")

        self.ping_service = PingService(self.transport)
        self.profile_service = ProfileService(
            self.host,
            self.transport,
            self.current_local_profile,
        )

        self.dht_service = DHTService(
            self.host,
            mode=dht_mode,
            benchmark_simulator=self.benchmark_simulator,
        )

        self.health_service = HealthService(
            self.peer_registry,
            self.ping_service,
            self.local_profile.peer_id,
        )

        self.recommendation_service = RecommendationService(
            self.host,
            self.transport,
            self.peer_registry,
            self.local_profile.peer_id,
            self.dht_service,
            self.profile_service,
        )

        if classifier_backend == "aiass":
            classifier_agent = AIAssAgent(
                model=self.aiass_classifier_model,
                base_url=aiass_base_url,
                api_key=aiass_api_key,
                timeout_s=classifier_timeout,
                system_prompt=CLASSIFIER_SYSTEM_PROMPT,
                max_tokens=96,
                temperature=0.0,
            )
            capability_classifier = CapabilityClassifier(agent=classifier_agent)
        elif classifier_backend == "local":
            local_answer_agent = (
                self.local_agent
                if agent_backend == "local"
                and self.local_classifier_model_id == self.local_model_id
                else None
            )
            classifier_agent = LocalTransformersAgent(
                model_id=self.local_classifier_model_id,
                max_new_tokens=96,
                system_prompt=CLASSIFIER_SYSTEM_PROMPT,
                enable_thinking=False,
                timeout_s=classifier_timeout,
                temperature=0.0,
                tokenizer=getattr(local_answer_agent, "tokenizer", None),
                model=getattr(local_answer_agent, "model", None),
            )
            capability_classifier = CapabilityClassifier(agent=classifier_agent)
        elif classifier_backend == "dummy":
            capability_classifier = CapabilityClassifier(agent=DummyAgent())
        elif classifier_backend == "ollama":
            capability_classifier = CapabilityClassifier(
                model=self.ollama_classifier_model,
                host=ollama_host,
                timeout_s=classifier_timeout,
            )
        else:
            raise ValueError(f"Unsupported classifier_backend: {classifier_backend}")

        self.capability_scorer = CapabilityScorer(
            role_hint_capabilities=advertised_capabilities,
            node_name=model_name,
        )

        self.routing_service = RoutingService(
            self.host,
            self.local_profile,
            self.peer_registry,
            self.dht_service,
            self.profile_service,
            300 * 1000,  # 5min
            self.health_service,
            capability_classifier,
            self.recommendation_service,
            self.routing_policy,
        )

        self.query_service = QueryService(
            self.host,
            self.transport,
            self.local_agent,
            self.routing_service,
            query_timeout_s=query_timeout,
            query_connect_timeout_s=query_connect_timeout,
        )

        self.pubsub_service = None
        self._trio_token = None
        self.api_url: str | None = None

        if self.enable_gossip:
            from src.services.pubsub_service import PubSubService

            self.pubsub_service = PubSubService(
                self.host,
                self.profile_service,
                self.peer_registry,
                self.local_profile.peer_id,
            )

    def advertised_addresses(self) -> list[str]:
        """
        Return the list of addresses that this node should advertise to others.

        For now we only support one explicit mode:
        - ipv6_loopback -> advertise only /ip6/::1/tcp/<port>/p2p/<peer_id>

        Fail fast if the requested address is not present in the listen addresses.
        """
        if self.advertise_address_mode == "ipv6_loopback":
            matches = [
                f"{addr}/p2p/{self.host.get_id()}"
                for addr in self.listen_addrs
                if str(addr).startswith("/ip6/::1/")
            ]
            if not matches:
                raise RuntimeError(
                    "advertise_address_mode='ipv6_loopback' was requested, "
                    "but no /ip6/::1 address is available in listen_addrs"
                )
            return matches

        raise ValueError(
            f"Unsupported advertise_address_mode: {self.advertise_address_mode}"
        )

    async def periodically_advertise_capabilities_to_dht(
        self,
        interval_s: float = CAPABILITY_ADVERTISE_INTERVAL_S,
    ) -> None:
        while True:
            await trio.sleep(interval_s)

            try:
                await self.advertise_capabilities_to_dht()
            except Exception as exc:
                log("DHT", f"Periodic capability advertise failed: {exc}")

    def all_shareable_addresses(self) -> list[str]:
        """
        Returns an array containing all the addresses (address + p2p + peer id)
        on which the local node can be reached.
        """
        return [
            f"{addr}/p2p/{self.local_profile.peer_id}" for addr in self.listen_addrs
        ]

    def print_addresses(self) -> None:
        """
        Prints node information.
        """
        log("NODE", f"I am {self.local_profile.peer_id}")
        log("NODE", f"Model: {self.local_profile.model_name}")
        log(
            "NODE",
            f"DHT advertised capabilities: {self.local_profile.advertised_capabilities}",
        )
        log("NODE", f"Capability scores: {self.local_profile.capability_scores}")
        log("NODE", f"Request backend: {self.agent_backend}")
        log("NODE", f"Classifier backend: {self.classifier_backend}")
        if self.agent_backend == "ollama":
            log(
                "NODE",
                f"Ollama answer backend model={self.ollama_model} "
                f"host={self.ollama_host} timeout={self.ollama_timeout:.0f}s",
            )
            classifier_description = (
                f"ollama model={self.ollama_classifier_model} host={self.ollama_host}"
            )
        elif self.agent_backend == "aiass":
            log(
                "NODE",
                f"AIaaS answer backend model={self.aiass_model} "
                f"base_url={self.aiass_base_url} timeout={self.aiass_timeout:.0f}s",
            )
            classifier_description = (
                f"aiass model={self.aiass_classifier_model} "
                f"base_url={self.aiass_base_url}"
            )
        elif self.agent_backend == "local":
            log(
                "NODE",
                f"Local Transformers answer backend model={self.local_model_id} "
                f"timeout={self.local_timeout:.0f}s",
            )
            classifier_description = f"local model={self.local_classifier_model_id}"
        else:
            log("NODE", "Dummy answer backend")

        if self.classifier_backend == "ollama":
            classifier_description = (
                f"ollama model={self.ollama_classifier_model} host={self.ollama_host}"
            )
        elif self.classifier_backend == "aiass":
            classifier_description = (
                f"aiass model={self.aiass_classifier_model} "
                f"base_url={self.aiass_base_url}"
            )
        elif self.classifier_backend == "local":
            classifier_description = f"local model={self.local_classifier_model_id}"
        else:
            classifier_description = "dummy"
        log(
            "ROUTING",
            f"Capability classifier: {classifier_description} "
            f"timeout={self.classifier_timeout:.0f}s "
            f"allowed_capabilities={CAPABILITIES}",
        )
        log(
            "SCORES",
            "Capability scoring: simulated "
            "one high score in [0.8, 1.0], "
            "one medium score in [0.5, 0.75], "
            "one low score in [0.1, 0.45]",
        )
        log("NODE", f"Advertise address mode: {self.advertise_address_mode}")
        log("NODE", f"Peer query response timeout: {self.query_timeout:.0f}s")
        log("NODE", f"Peer query connect timeout: {self.query_connect_timeout:.0f}s")
        log("ROUTING", f"Routing policy: {self.routing_policy.name}")
        log(
            "BENCH",
            f"Benchmark network simulator: {self.benchmark_simulator.describe()}",
        )
        if self.api_url is not None:
            log("NODE", f"HTTP API: {self.api_url}")
            log("NODE", f"HTTP query endpoint: POST {self.api_url}/api/query")
        else:
            log("NODE", "HTTP API: disabled")
        log("NODE", "Listening on:")
        for addr in self.local_profile.addresses:
            print(f"  {addr}", flush=True)

        log("NODE", "Advertised addresses:")
        for addr in self.advertised_addresses():
            print(f"  {addr}", flush=True)

    def register_protocol_handlers(self) -> None:
        """
        Register all stream handlers.
        """
        self.host.set_stream_handler(PING_PROTOCOL, self.ping_service.handle_stream)
        log("NODE", f"Registered protocol handler: {PING_PROTOCOL}")
        self.host.set_stream_handler(
            PROFILE_PROTOCOL,
            self.profile_service.handle_stream,
        )
        log("NODE", f"Registered protocol handler: {PROFILE_PROTOCOL}")
        self.host.set_stream_handler(QUERY_PROTOCOL, self.query_service.handle_stream)
        log("NODE", f"Registered protocol handler: {QUERY_PROTOCOL}")
        self.host.set_stream_handler(
            RECOMMEND_PROTOCOL, self.recommendation_service.handle_stream
        )
        log("NODE", f"Registered protocol handler: {RECOMMEND_PROTOCOL}")

    async def refresh_local_profile(self) -> None:
        self.local_profile.addresses = self.advertised_addresses()
        self.local_profile.timestamp_ms = int(time.time() * 1000)

        log(
            "NODE",
            f"Refreshed local profile peer_id={self.local_profile.peer_id} "
            f"model={self.local_profile.model_name} "
            f"dht_caps={self.local_profile.advertised_capabilities} "
            f"capability_scores={self.local_profile.capability_scores} "
            f"addresses={self.local_profile.addresses} "
            f"ts={self.local_profile.timestamp_ms}",
        )

    async def current_local_profile(self) -> NodeProfile:
        await self.refresh_local_profile()
        return self.local_profile

    async def advertise_capabilities_to_dht(self) -> None:
        await self.refresh_local_profile()
        log(
            "DHT",
            f"Advertising DHT capability indexes peer_id={self.local_profile.peer_id} "
            f"dht_caps={self.local_profile.advertised_capabilities}",
        )
        await self.dht_service.advertise_capabilities(self.local_profile)

    async def publish_self_to_dht(self) -> None:
        await self.advertise_capabilities_to_dht()

    async def score_capabilities_before_advertising(self) -> None:
        if self._capability_scored:
            return

        log(
            "SCORES",
            "Generating simulated capability scores before advertising this node.",
        )
        scores = await self.capability_scorer.score_all()
        advertised_capabilities, resolved_scores = self.resolve_profile_capabilities(
            None,
            scores,
        )
        self.local_profile.capability_scores = resolved_scores
        self.local_profile.advertised_capabilities = advertised_capabilities
        self._capability_scored = True
        log(
            "SCORES",
            f"Updated node profile after simulated scoring "
            f"dht_caps={advertised_capabilities} scores={resolved_scores}",
        )

    async def announce_self_via_gossip(self) -> None:
        if self.pubsub_service is None:
            return

        await self.pubsub_service.publish_profile_update(
            self.local_profile.peer_id,
            self.local_profile.timestamp_ms,
        )

    async def _start_node_logic(
        self,
        bootstrap_addrs: list[str],
    ) -> None:
        log("NODE", f"I am {self.local_profile.peer_id}")
        bootstrap_peers = await connect_to_bootstrap_peers(
            self.host,
            bootstrap_addrs,
        )
        await self.dht_service.add_bootstrap_peers(bootstrap_peers)

        await self.score_capabilities_before_advertising()
        await self.publish_self_to_dht()

        if self.enable_gossip:
            await self.announce_self_via_gossip()

        self.print_addresses()
        log("NODE", "Node started. Waiting for incoming streams...")
        await trio.sleep_forever()

    async def run_forever(
        self,
        bootstrap_addrs: list[str] | None = None,
        api_host: str | None = None,
        api_port: int | None = None,
    ) -> None:
        bootstrap_addrs = bootstrap_addrs or []
        self._trio_token = trio.lowlevel.current_trio_token()
        if api_port is not None:
            self.api_url = f"http://{api_host or '127.0.0.1'}:{api_port}"
        else:
            self.api_url = None
        self.local_profile.addresses = self.advertised_addresses()
        self.register_protocol_handlers()

        async with (
            self.host.run(listen_addrs=self.listen_addrs),
            trio.open_nursery() as nursery,
        ):
            async with self.dht_service.run():
                nursery.start_soon(self.periodically_advertise_capabilities_to_dht)
                if api_port is not None:
                    from src.services.node_api_service import NodeAPIService

                    node_api = NodeAPIService(
                        self,
                        api_host or "127.0.0.1",
                        api_port,
                    )
                    nursery.start_soon(node_api.run)

                if self.enable_gossip:
                    if self.pubsub_service is None:
                        raise RuntimeError(
                            "Gossip is enabled but PubSubService was not initialized"
                        )

                    async with self.pubsub_service.run():
                        nursery.start_soon(
                            self.pubsub_service.run_profile_update_listener
                        )
                        await self._start_node_logic(bootstrap_addrs)
                else:
                    log("GOSSIP", "Gossip disabled")
                    await self._start_node_logic(bootstrap_addrs)

    def answer_query_from_api(
        self,
        prompt: str,
        query_id: str | None = None,
        required_capabilities: dict[str, float] | None = None,
        progress_callback=None,
    ) -> dict:
        if self._trio_token is None:
            raise RuntimeError("Node is not running yet")

        context = QueryContext(
            origin_peer_id="http-api",
            required_capabilities=required_capabilities,
        )

        return trio.from_thread.run(
            self.query_service.answer_query,
            prompt,
            query_id,
            context,
            progress_callback,
            trio_token=self._trio_token,
        )

    def benchmark_profile_from_api(self) -> dict:
        known_peers = []
        for profile in self.peer_registry.all_profiles():
            status = self.peer_registry.get_status(profile.peer_id)
            known_peers.append(
                {
                    "peer_id": profile.peer_id,
                    "addresses": profile.addresses,
                    "model_name": profile.model_name,
                    "advertised_capabilities": profile.advertised_capabilities,
                    "capability_scores": profile.capability_scores,
                    "is_available": profile.is_available,
                    "status": {
                        "is_alive": status.is_alive if status is not None else False,
                        "last_rtt_ms": (
                            status.last_rtt_ms if status is not None else None
                        ),
                        "consecutive_failures": (
                            status.consecutive_failures
                            if status is not None
                            else 0
                        ),
                        "last_checked_ts_ms": (
                            status.last_checked_ts_ms
                            if status is not None
                            else None
                        ),
                    },
                }
            )

        return {
            "peer_id": self.local_profile.peer_id,
            "addresses": self.local_profile.addresses,
            "model_name": self.local_profile.model_name,
            "advertised_capabilities": self.local_profile.advertised_capabilities,
            "capability_scores": self.local_profile.capability_scores,
            "is_available": self.local_profile.is_available,
            "routing_policy": self.routing_policy.name,
            "benchmark_network": self.benchmark_simulator.describe(),
            "known_peers": known_peers,
        }
