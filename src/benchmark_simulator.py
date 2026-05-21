from __future__ import annotations

import random
from dataclasses import dataclass

import trio

from src.logging_utils import log


@dataclass(frozen=True)
class BenchmarkNetworkConfig:
    enabled: bool = False
    latency_min_ms: float = 0.0
    latency_max_ms: float = 0.0
    node_extra_latency_ms: float = 0.0
    seed: int = 42
    node_id: str = ""


class BenchmarkNetworkSimulator:
    """
    Small deterministic-ish network delay injector for local benchmarks.

    It simulates application-visible network latency only. It does not simulate
    packet loss or LLM generation latency.
    """

    def __init__(self, config: BenchmarkNetworkConfig) -> None:
        self.config = config
        self._rng = random.Random(self._seed_for_node(config.seed, config.node_id))

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def describe(self) -> str:
        if not self.enabled:
            return "disabled"

        return (
            "enabled "
            f"latency=[{self.config.latency_min_ms:.1f}, "
            f"{self.config.latency_max_ms:.1f}]ms "
            f"extra={self.config.node_extra_latency_ms:.1f}ms "
            f"seed={self.config.seed}"
        )

    async def wait(self, operation: str) -> float:
        if not self.enabled:
            return 0.0

        delay_ms = self._sample_delay_ms()
        if delay_ms <= 0.0:
            return 0.0

        log("BENCH", f"Simulated network delay operation={operation} delay_ms={delay_ms:.1f}")
        await trio.sleep(delay_ms / 1000.0)
        return delay_ms

    def _sample_delay_ms(self) -> float:
        lower = max(0.0, self.config.latency_min_ms)
        upper = max(lower, self.config.latency_max_ms)
        sampled = self._rng.uniform(lower, upper) if upper > 0.0 else 0.0
        return sampled + max(0.0, self.config.node_extra_latency_ms)

    def _seed_for_node(self, seed: int, node_id: str) -> int:
        if not node_id:
            return seed

        value = seed
        for char in node_id:
            value = ((value * 131) + ord(char)) & 0xFFFFFFFF
        return value

