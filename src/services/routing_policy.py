from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RoutingPolicy:
    name: str
    quality_weight: float
    freshness_weight: float
    failure_weight: float
    latency_weight: float


ROUTING_POLICIES: dict[str, RoutingPolicy] = {
    "current": RoutingPolicy(
        name="current",
        quality_weight=0.75,
        freshness_weight=0.15,
        failure_weight=-0.07,
        latency_weight=-0.08,
    ),
    "capability_only": RoutingPolicy(
        name="capability_only",
        quality_weight=1.0,
        freshness_weight=0.0,
        failure_weight=0.0,
        latency_weight=0.0,
    ),
    "latency_aware": RoutingPolicy(
        name="latency_aware",
        quality_weight=0.70,
        freshness_weight=0.10,
        failure_weight=-0.05,
        latency_weight=-0.15,
    ),
    "reliability_aware": RoutingPolicy(
        name="reliability_aware",
        quality_weight=0.70,
        freshness_weight=0.10,
        failure_weight=-0.15,
        latency_weight=-0.05,
    ),
    "balanced": RoutingPolicy(
        name="balanced",
        quality_weight=0.80,
        freshness_weight=0.05,
        failure_weight=-0.05,
        latency_weight=-0.10,
    ),
}


def get_routing_policy(name: str | None) -> RoutingPolicy:
    selected = (name or "current").strip().lower()
    if selected not in ROUTING_POLICIES:
        allowed = ", ".join(sorted(ROUTING_POLICIES))
        raise ValueError(f"Unknown ROUTING_POLICY={name!r}. Allowed values: {allowed}")
    return ROUTING_POLICIES[selected]

