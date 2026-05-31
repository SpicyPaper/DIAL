from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RoutingPolicy:
    name: str
    quality_weight: float
    freshness_weight: float
    failure_weight: float
    latency_weight: float

    @property
    def absolute_weight_sum(self) -> float:
        return (
            abs(self.quality_weight)
            + abs(self.freshness_weight)
            + abs(self.failure_weight)
            + abs(self.latency_weight)
        )


ROUTING_POLICIES: dict[str, RoutingPolicy] = {
    "current": RoutingPolicy(
        name="current",
        quality_weight=0.70,
        freshness_weight=0.10,
        failure_weight=-0.10,
        latency_weight=-0.10,
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
        quality_weight=0.60,
        freshness_weight=0.05,
        failure_weight=-0.05,
        latency_weight=-0.30,
    ),
    "reliability_aware": RoutingPolicy(
        name="reliability_aware",
        quality_weight=0.60,
        freshness_weight=0.05,
        failure_weight=-0.30,
        latency_weight=-0.05,
    ),
    "balanced": RoutingPolicy(
        name="balanced",
        quality_weight=0.55,
        freshness_weight=0.15,
        failure_weight=-0.15,
        latency_weight=-0.15,
    ),
}


def _validate_routing_policies() -> None:
    for policy in ROUTING_POLICIES.values():
        if policy.quality_weight < 0.0 or policy.freshness_weight < 0.0:
            raise ValueError(f"Routing policy {policy.name!r} has a negative reward weight")
        if policy.failure_weight > 0.0 or policy.latency_weight > 0.0:
            raise ValueError(f"Routing policy {policy.name!r} has a positive penalty weight")
        if abs(policy.absolute_weight_sum - 1.0) > 1e-9:
            raise ValueError(
                f"Routing policy {policy.name!r} weights must sum to 1 by magnitude; "
                f"got {policy.absolute_weight_sum:.4f}"
            )


_validate_routing_policies()


def get_routing_policy(name: str | None) -> RoutingPolicy:
    selected = (name or "current").strip().lower()
    if selected not in ROUTING_POLICIES:
        allowed = ", ".join(sorted(ROUTING_POLICIES))
        raise ValueError(f"Unknown ROUTING_POLICY={name!r}. Allowed values: {allowed}")
    return ROUTING_POLICIES[selected]
