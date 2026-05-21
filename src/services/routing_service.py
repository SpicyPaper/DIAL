import time
from collections.abc import Callable

from libp2p.abc import IHost
from libp2p.peer.id import ID

from src.logging_utils import log
from src.models import NodeProfile, QueryContext
from src.peer_registry import PeerRegistry
from src.services.dht_service import DHTService
from src.services.health_service import HealthService
from src.services.capability_classifier import CAPABILITIES, CapabilityClassifier
from src.services.profile_service import ProfileService
from src.services.recommendation_service import RecommendationService
from src.services.routing_policy import RoutingPolicy, get_routing_policy

MIN_ROUTING_SCORE_THRESHOLD = 0.65
DHT_DISCOVERY_MIN_DEMAND = 0.3
MAX_RECOMMENDATIONS_PER_PEER = 3


class RoutingDecision:
    def __init__(
        self,
        execute_locally: bool,
        target_peer_id: str | None = None,
        reason: str = "",
        no_suitable_node: bool = False,
        routing_trace: dict | None = None,
    ) -> None:
        self.execute_locally = execute_locally
        self.target_peer_id = target_peer_id
        self.reason = reason
        self.no_suitable_node = no_suitable_node
        self.routing_trace = routing_trace or {}


class RoutingService:
    """
    Routing service.

    Treats the local registry as a cache, asks the DHT for providers of the
    important requested capabilities, then scores every reachable node against
    the whole capability mix for the query.
    """

    def __init__(
        self,
        host: IHost,
        local_profile: NodeProfile,
        peer_registry: PeerRegistry,
        dht_service: DHTService,
        profile_service: ProfileService,
        live_ttl_ms: int,
        health_service: HealthService | None = None,
        capability_classifier: CapabilityClassifier | None = None,
        recommendation_service: RecommendationService | None = None,
        routing_policy: RoutingPolicy | str | None = None,
    ) -> None:
        self.host = host
        self.local_profile = local_profile
        self.peer_registry = peer_registry
        self.dht_service = dht_service
        self.profile_service = profile_service
        self.health_service = health_service
        self.live_ttl_ms = live_ttl_ms
        self.capability_classifier = capability_classifier
        self.recommendation_service = recommendation_service
        if isinstance(routing_policy, RoutingPolicy):
            self.routing_policy = routing_policy
        else:
            self.routing_policy = get_routing_policy(routing_policy)

    async def refresh_candidates_from_dht(self, capability: str) -> None:
        """
        Ask the DHT for providers of a capability, fetch profiles directly from
        those peers, and store them in the local cache.
        """
        if self.dht_service is None:
            return

        provider_infos = await self.dht_service.find_capability_providers(capability)

        for provider in provider_infos:
            peer_id = provider.peer_id.to_string()
            if peer_id == self.local_profile.peer_id:
                continue

            profile = await self._request_provider_profile(provider)
            if profile is None:
                log("ROUTING", f"Could not fetch direct profile peer_id={peer_id}")
                continue

            if not profile.is_available:
                log(
                    "ROUTING",
                    f"Ignored unavailable direct profile peer_id={profile.peer_id}",
                )
                continue

            old_profile = self.peer_registry.get_profile(profile.peer_id)
            self.peer_registry.upsert_profile(profile)

            if old_profile is None:
                log(
                    "ROUTING",
                    f"Discovered new peer from DHT provider peer_id={profile.peer_id} "
                    f"dht_caps={profile.advertised_capabilities} "
                    f"addresses={profile.addresses}",
                )

    async def route_query(
        self,
        prompt: str,
        context: QueryContext,
        progress_callback: Callable[[dict], None] | None = None,
    ) -> RoutingDecision:
        """
        Routing rule:
        1. get the scored capability needs from the query or forwarded context
        2. refresh DHT candidates for important requested capabilities
        3. score local and direct DHT candidates against the whole capability mix
        4. if no direct candidate is good enough, ask direct peers for recommendations
        5. if no recommended candidate is good enough, use the best candidate seen
        6. if no candidate exists, retry with the general capability
        7. otherwise return no_suitable_node
        """
        def emit(event: str, message: str, **data) -> None:
            if progress_callback is not None:
                progress_callback({"event": event, "message": message, **data})

        # Resolve scored capabilities once and carry them through forwarding.
        if context.required_capabilities is None:
            if self.capability_classifier is None:
                raise RuntimeError("Routing requires a capability classifier")

            emit(
                "classification_started",
                "The entry node is classifying what capabilities the request needs.",
            )
            required_capabilities = await self.capability_classifier.classify_scores(
                prompt
            )
            context.required_capabilities = required_capabilities
            emit(
                "classification_completed",
                "The entry node classified the required capabilities.",
                required_capabilities=required_capabilities,
            )
            log(
                "ROUTING",
                f"LLM classified query capability_scores={required_capabilities}",
            )
        else:
            required_capabilities = self._normalize_capability_scores(
                context.required_capabilities
            )
            context.required_capabilities = required_capabilities
            emit(
                "classification_reused",
                "The required capabilities were already known, so classification was skipped.",
                required_capabilities=required_capabilities,
            )

        excluded = set(context.excluded_peer_ids)
        excluded.add(self.local_profile.peer_id)

        candidate, trace = await self._search_scored_capabilities(
            required_capabilities,
            excluded,
            progress_callback,
        )
        selected_capabilities = required_capabilities

        if candidate is None and "general" not in required_capabilities:
            log(
                "ROUTING",
                f"No candidate found for capability_scores={required_capabilities}, "
                f"retrying with general",
            )
            initial_trace = trace
            general_candidate, general_trace = await self._search_scored_capabilities(
                {"general": 0.5},
                excluded,
                progress_callback,
            )
            general_trace["previous_attempt"] = initial_trace
            trace = general_trace
            candidate = general_candidate
            selected_capabilities = {"general": 0.5}

        if candidate is None:
            log(
                "ROUTING",
                f"No candidate found for capability_scores={required_capabilities} "
                f"and no candidate found for general retry",
            )
            return RoutingDecision(
                execute_locally=False,
                target_peer_id=None,
                reason=(
                    f"no valid candidate found for capability_scores="
                    f"{required_capabilities} "
                    f"or general"
                ),
                no_suitable_node=True,
                routing_trace=trace,
            )

        kind, profile, routing_score, weighted_quality, source, selected_trace = candidate
        trace["selected"] = {**selected_trace, "selected": True}
        trace["decision_reason"] = ""

        if kind == "local":
            reason = (
                f"local chosen for capability_scores={selected_capabilities} "
                f"weighted_quality={weighted_quality:.2f} "
                f"routing_score={routing_score:.2f}"
            )
            trace["decision_reason"] = reason
            return RoutingDecision(
                execute_locally=True,
                reason=reason,
                routing_trace=trace,
            )

        reason = (
            f"forward to peer={profile.peer_id} "
            f"capability_scores={selected_capabilities} "
            f"node_scores={self._profile_scores_for(profile, selected_capabilities)} "
            f"weighted_quality={weighted_quality:.2f} "
            f"routing_score={routing_score:.2f}"
        )
        trace["decision_reason"] = reason
        return RoutingDecision(
            execute_locally=False,
            target_peer_id=profile.peer_id,
            reason=reason,
            routing_trace=trace,
        )

    def _normalize_capability_scores(self, scores: dict[str, float]) -> dict[str, float]:
        """Keep at most three known capabilities with scores in [0, 1]."""
        if not isinstance(scores, dict):
            return {"general": 0.6}

        normalized: dict[str, float] = {}
        for capability, score in scores.items():
            capability = str(capability).strip().lower()
            if capability not in CAPABILITIES:
                continue

            try:
                value = float(score)
            except (TypeError, ValueError):
                continue

            value = max(0.0, min(1.0, value))
            if value > 0.0:
                normalized[capability] = value

        return dict(
            sorted(
                normalized.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:3]
        ) or {"general": 0.6}

    def _discovery_capabilities(self, capability_scores: dict[str, float]) -> list[str]:
        """
        Use only meaningful needs for DHT lookups, but keep all needs for scoring.

        If every score is below the discovery threshold, use the top capability
        so routing still has at least one DHT entry point.
        """
        capabilities = [
            capability
            for capability, score in capability_scores.items()
            if score >= DHT_DISCOVERY_MIN_DEMAND
        ]

        if capabilities:
            return capabilities

        return [next(iter(capability_scores))]

    async def _search_scored_capabilities(
        self,
        capability_scores: dict[str, float],
        excluded_peer_ids: set[str],
        progress_callback: Callable[[dict], None] | None = None,
    ) -> tuple[tuple[str, NodeProfile | None, float, float, str, dict] | None, dict]:
        """
        Refresh DHT candidates for important requested capabilities, then choose
        the node with the best routing score.

        The routing keeps the original two-stage behavior: first try local and
        direct DHT candidates, then ask recommendations only if no direct
        candidate is good enough.
        """
        capability_scores = self._normalize_capability_scores(capability_scores)
        discovery_capabilities = self._discovery_capabilities(capability_scores)
        routing_started_at = time.perf_counter()
        trace = {
            "local_peer_id": self.local_profile.peer_id,
            "required_capabilities": capability_scores,
            "discovery_capabilities": discovery_capabilities,
            "dht_discovery_min_demand": DHT_DISCOVERY_MIN_DEMAND,
            "routing_score_threshold": MIN_ROUTING_SCORE_THRESHOLD,
            "excluded_peer_ids": sorted(excluded_peer_ids),
            "recommendation_status": "",
            "recommendation_reason": "",
            "selection_reason": "",
            "routing_policy": self.routing_policy.name,
            "stages": [],
        }

        def elapsed_ms(started_at: float) -> float:
            return round((time.perf_counter() - started_at) * 1000, 1)

        def emit(event: str, message: str, **data) -> None:
            if progress_callback is not None:
                progress_callback({"event": event, "message": message, **data})

        def finish_trace() -> None:
            trace["routing_duration_ms"] = elapsed_ms(routing_started_at)

        direct_started_at = time.perf_counter()
        emit(
            "dht_lookup_started",
            "The entry node is asking the DHT for direct candidates.",
            required_capabilities=capability_scores,
            discovery_capabilities=discovery_capabilities,
        )
        for capability in discovery_capabilities:
            await self.refresh_candidates_from_dht(capability)

        direct_remote_candidates = [
            profile
            for profile in self.peer_registry.all_profiles()
            if profile.peer_id not in excluded_peer_ids
            and profile.is_available
            and self._has_any_requested_capability(profile, capability_scores)
        ]

        local_available = self._has_any_requested_capability(
            self.local_profile,
            capability_scores,
        )

        log(
            "ROUTING",
            f"Scoring cumulative candidates capability_scores={capability_scores} "
            f"discovery_capabilities={discovery_capabilities} "
            f"local_available={local_available} "
            f"direct_count={len(direct_remote_candidates)}",
        )

        direct_candidates = await self._evaluate_candidate_pool(
            direct_remote_candidates,
            capability_scores,
            include_local=True,
            source="direct",
        )
        direct_duration_ms = elapsed_ms(direct_started_at)
        trace["stages"].append(
            {
                "name": "direct",
                "duration_ms": direct_duration_ms,
                "candidate_count": len(direct_candidates),
                "candidates": [candidate[-1] for candidate in direct_candidates],
                "skipped_candidates": []
                if local_available
                else [
                    {
                        "kind": "local",
                        "peer": self._profile_summary(None),
                        "reason": (
                            "The local node was not evaluated because none of "
                            "its capability scores matched the requested "
                            "capabilities."
                        ),
                    }
                ],
            }
        )
        emit(
            "direct_candidates_evaluated",
            "The entry node evaluated itself and the direct DHT candidates.",
            candidate_count=len(direct_candidates),
            duration_ms=direct_duration_ms,
        )
        best_direct = self._best_candidate(direct_candidates)

        if best_direct is not None and best_direct[2] >= MIN_ROUTING_SCORE_THRESHOLD:
            trace["recommendation_status"] = "skipped"
            trace["recommendation_reason"] = (
                "A direct candidate met the minimum routing score, so no "
                "recommendation requests were needed."
            )
            trace["selection_reason"] = "direct_candidate_above_threshold"
            log(
                "ROUTING",
                f"Selected direct candidate routing_score={best_direct[2]:.2f} "
                f"threshold={MIN_ROUTING_SCORE_THRESHOLD:.2f}",
            )
            finish_trace()
            emit(
                "routing_candidate_selected",
                "A direct candidate met the routing threshold.",
                routing_score=best_direct[2],
                threshold=MIN_ROUTING_SCORE_THRESHOLD,
            )
            return self._candidate_from_evaluation(best_direct), trace

        if best_direct is None:
            log("ROUTING", "No direct candidates available")
        else:
            log(
                "ROUTING",
                f"No direct candidate above threshold "
                f"best_routing_score={best_direct[2]:.2f} "
                f"threshold={MIN_ROUTING_SCORE_THRESHOLD:.2f}",
            )

        recommendation_started_at = time.perf_counter()
        emit(
            "recommendations_started",
            "No direct candidate was strong enough, so the entry node is asking peers for recommendations.",
        )
        recommended_remote_candidates = await self._get_recommended_candidates(
            direct_remote_candidates,
            capability_scores,
            excluded_peer_ids,
            trace,
        )
        recommendation_request_count = len(trace.get("recommendation_requests", []))
        trace["recommendation_request_count"] = recommendation_request_count
        if recommendation_request_count > 0:
            trace["recommendation_duration_ms"] = elapsed_ms(recommendation_started_at)
        trace["recommendation_status"] = "used"
        if recommended_remote_candidates:
            trace["recommendation_reason"] = (
                "No direct candidate reached the minimum routing score, so "
                "direct peers were asked for recommendations."
            )
        elif self.recommendation_service is None:
            trace["recommendation_status"] = "disabled"
            trace["recommendation_reason"] = (
                "No direct candidate reached the minimum routing score, but "
                "the recommendation service is not enabled on this node."
            )
        elif recommendation_request_count == 0:
            trace["recommendation_status"] = "no_requests"
            trace["recommendation_reason"] = (
                "No direct candidate reached the minimum routing score, but "
                "there was no direct peer suitable to ask for recommendations."
            )
        else:
            trace["recommendation_reason"] = (
                "No direct candidate reached the minimum routing score. "
                "Recommendation requests were tried, but did not return any "
                "additional reachable candidate."
            )
        log(
            "ROUTING",
            f"Recommendation candidate count={len(recommended_remote_candidates)}",
        )
        emit(
            "recommendations_completed",
            "The recommendation step completed.",
            candidate_count=len(recommended_remote_candidates),
            request_count=recommendation_request_count,
            status=trace["recommendation_status"],
        )

        recommended_evaluation_started_at = time.perf_counter()
        recommended_candidates = await self._evaluate_candidate_pool(
            recommended_remote_candidates,
            capability_scores,
            include_local=False,
            source="recommended",
            recommended_sources=trace.get("recommended_sources", {}),
        )
        recommended_evaluation_duration_ms = elapsed_ms(
            recommended_evaluation_started_at
        )
        trace["stages"].append(
            {
                "name": "recommended",
                "duration_ms": recommended_evaluation_duration_ms,
                "candidate_count": len(recommended_candidates),
                "candidates": [candidate[-1] for candidate in recommended_candidates],
            }
        )
        emit(
            "recommended_candidates_evaluated",
            "The entry node evaluated the recommended candidates.",
            candidate_count=len(recommended_candidates),
            duration_ms=recommended_evaluation_duration_ms,
        )
        best_recommended = self._best_candidate(recommended_candidates)

        if (
            best_recommended is not None
            and best_recommended[2] >= MIN_ROUTING_SCORE_THRESHOLD
        ):
            trace["selection_reason"] = "recommended_candidate_above_threshold"
            log(
                "ROUTING",
                f"Selected recommended candidate routing_score={best_recommended[2]:.2f} "
                f"threshold={MIN_ROUTING_SCORE_THRESHOLD:.2f}",
            )
            finish_trace()
            emit(
                "routing_candidate_selected",
                "A recommended candidate met the routing threshold.",
                routing_score=best_recommended[2],
                threshold=MIN_ROUTING_SCORE_THRESHOLD,
            )
            return self._candidate_from_evaluation(best_recommended), trace

        best_seen = self._best_candidate(direct_candidates + recommended_candidates)
        if best_seen is not None:
            trace["selection_reason"] = "best_seen_below_threshold"
            log(
                "ROUTING",
                f"No candidate above threshold, using best seen "
                f"routing_score={best_seen[2]:.2f} "
                f"threshold={MIN_ROUTING_SCORE_THRESHOLD:.2f}",
            )

        finish_trace()
        if best_seen is not None:
            emit(
                "routing_candidate_selected",
                "No candidate met the threshold, so the best evaluated candidate was selected.",
                routing_score=best_seen[2],
                threshold=MIN_ROUTING_SCORE_THRESHOLD,
            )
        return (
            self._candidate_from_evaluation(best_seen) if best_seen is not None else None
        ), trace

    async def _evaluate_candidate_pool(
        self,
        candidates: list[NodeProfile],
        capability_scores: dict[str, float],
        include_local: bool,
        source: str,
        recommended_sources: dict[str, list[dict]] | None = None,
    ) -> list[tuple[str, NodeProfile | None, float, float, str, dict]]:
        evaluated: list[tuple[str, NodeProfile | None, float, float, str, dict]] = []
        recommended_sources = recommended_sources or {}

        if include_local and self._has_any_requested_capability(
            self.local_profile,
            capability_scores,
        ):
            (
                local_routing_score,
                local_weighted_quality,
                local_score_breakdown,
            ) = self._scored_candidate_routing_score(
                self.local_profile,
                capability_scores,
            )
            local_trace = self._candidate_trace(
                "local",
                None,
                local_routing_score,
                local_weighted_quality,
                "local",
                capability_scores,
                local_score_breakdown,
            )
            evaluated.append(
                (
                    "local",
                    None,
                    local_routing_score,
                    local_weighted_quality,
                    "local",
                    local_trace,
                )
            )

        for profile in candidates:
            candidate = await self._evaluate_scored_candidate(
                profile,
                capability_scores,
                source,
                recommended_by=recommended_sources.get(profile.peer_id, []),
            )
            if candidate is not None:
                evaluated.append(candidate)

        return evaluated

    def _best_candidate(
        self,
        candidates: list[tuple[str, NodeProfile | None, float, float, str, dict]],
    ) -> tuple[str, NodeProfile | None, float, float, str, dict] | None:
        if not candidates:
            return None

        return max(candidates, key=lambda candidate: candidate[2])

    def _candidate_from_evaluation(
        self,
        candidate: tuple[str, NodeProfile | None, float, float, str, dict],
    ) -> tuple[str, NodeProfile | None, float, float, str, dict]:
        return candidate

    async def _get_recommended_candidates(
        self,
        sources: list[NodeProfile],
        capability_scores: dict[str, float],
        excluded_peer_ids: set[str],
        trace: dict,
    ) -> list[NodeProfile]:
        """Ask direct candidates for extra peers, without changing the ranking rule."""
        if self.recommendation_service is None:
            return []

        recommended_by_id: dict[str, NodeProfile] = {}
        recommended_sources: dict[str, list[dict]] = {}
        recommendation_requests: list[dict] = []
        recommendation_excluded_peer_ids = set(excluded_peer_ids)
        recommendation_excluded_peer_ids.update(profile.peer_id for profile in sources)

        for capability in capability_scores:
            capable_sources = [
                profile
                for profile in sources
                if capability in profile.advertised_capabilities
            ]

            log(
                "ROUTING",
                f"Asking recommendations capability={capability} "
                f"source_peer_ids={[profile.peer_id for profile in capable_sources]} "
                f"excluded_peer_ids={sorted(recommendation_excluded_peer_ids)}",
            )

            for source in capable_sources:
                recommended_peer_ids = (
                    await self.recommendation_service.request_recommendations(
                        peer_id=ID.from_base58(source.peer_id),
                        capability=capability,
                        limit=MAX_RECOMMENDATIONS_PER_PEER,
                        exclude_peer_ids=list(recommendation_excluded_peer_ids),
                    )
                )
                recommendation_requests.append(
                    {
                        "source_peer_id": source.peer_id,
                        "source_peer": self._profile_summary(source),
                        "capability": capability,
                        "returned_peer_ids": recommended_peer_ids,
                    }
                )

                for peer_id in recommended_peer_ids:
                    if peer_id in recommendation_excluded_peer_ids:
                        continue

                    profile = self.peer_registry.get_profile(peer_id)
                    if profile is None:
                        profile = await self._fetch_profile_from_dht_provider(
                            peer_id,
                            capability,
                        )
                        if profile is not None:
                            self.peer_registry.upsert_profile(profile)

                    if (
                        profile is not None
                        and profile.is_available
                        and self._has_any_requested_capability(
                            profile,
                            capability_scores,
                        )
                    ):
                        recommended_by_id[profile.peer_id] = profile
                        recommended_sources.setdefault(profile.peer_id, []).append(
                            {
                                "source_peer_id": source.peer_id,
                                "source_peer": self._profile_summary(source),
                                "capability": capability,
                            }
                        )
                        recommendation_excluded_peer_ids.add(profile.peer_id)

        trace["recommendation_requests"] = recommendation_requests
        trace["recommended_sources"] = recommended_sources
        return list(recommended_by_id.values())

    async def _request_provider_profile(self, provider) -> NodeProfile | None:
        peer_id = provider.peer_id.to_string()
        addresses = [
            f"{address}/p2p/{peer_id}"
            for address in getattr(provider, "addrs", [])
        ]
        return await self.profile_service.request_profile(
            provider.peer_id,
            addresses=addresses,
        )

    async def _fetch_profile_from_dht_provider(
        self,
        peer_id: str,
        capability: str,
    ) -> NodeProfile | None:
        if self.dht_service is None:
            return await self.profile_service.request_profile(ID.from_base58(peer_id))

        providers = await self.dht_service.find_capability_providers(capability)
        for provider in providers:
            if provider.peer_id.to_string() == peer_id:
                return await self._request_provider_profile(provider)

        return await self.profile_service.request_profile(ID.from_base58(peer_id))

    def _has_any_requested_capability(
        self,
        profile: NodeProfile,
        capability_scores: dict[str, float],
    ) -> bool:
        return any(
            profile.capability_scores.get(capability, 0.0) > 0.0
            for capability in capability_scores
        )

    def _profile_scores_for(
        self,
        profile: NodeProfile,
        capability_scores: dict[str, float],
    ) -> dict[str, float]:
        return {
            capability: profile.capability_scores.get(capability, 0.0)
            for capability in capability_scores
        }

    def _profile_summary(self, profile: NodeProfile | None) -> dict | None:
        if profile is None:
            profile = self.local_profile

        return {
            "peer_id": profile.peer_id,
            "model_name": profile.model_name,
            "advertised_capabilities": profile.advertised_capabilities,
            "capability_scores": profile.capability_scores,
            "addresses": profile.addresses,
            "is_available": profile.is_available,
        }

    def _candidate_trace(
        self,
        kind: str,
        profile: NodeProfile | None,
        routing_score: float,
        weighted_quality: float,
        source: str,
        capability_scores: dict[str, float],
        score_breakdown: dict,
        selected: bool = False,
        recommended_by: list[dict] | None = None,
    ) -> dict:
        profile_for_scores = self.local_profile if profile is None else profile
        return {
            "kind": kind,
            "source": source,
            "selected": selected,
            "peer": self._profile_summary(profile),
            "node_scores": self._profile_scores_for(
                profile_for_scores,
                capability_scores,
            ),
            "weighted_quality": round(weighted_quality, 4),
            "routing_score": round(routing_score, 4),
            "score_breakdown": score_breakdown,
            "recommended_by": recommended_by or [],
        }

    def _weighted_capability_quality(
        self,
        profile: NodeProfile,
        capability_scores: dict[str, float],
    ) -> float:
        """
        Weighted average of node quality for the requested capabilities.

        Missing node scores count as 0, and the final quality is clamped to
        [0, 1] so it stays comparable with the rest of the routing score formula.
        """
        total_demand = sum(capability_scores.values())
        if total_demand <= 0.0:
            return 0.0

        weighted_quality = sum(
            demand_score * profile.capability_scores.get(capability, 0.0)
            for capability, demand_score in capability_scores.items()
        ) / total_demand

        return max(0.0, min(1.0, weighted_quality))

    def _scored_candidate_routing_score(
        self,
        profile: NodeProfile,
        capability_scores: dict[str, float],
    ) -> tuple[float, float, dict]:
        """
        Combine static multi-capability quality with local health observations.
        """
        weighted_quality = self._weighted_capability_quality(profile, capability_scores)

        status = self.peer_registry.get_status(profile.peer_id)

        freshness_score = 0.0
        failure_score = 0.0
        latency_score = 0.0

        if profile.peer_id == self.local_profile.peer_id:
            freshness_score = 1.0
        elif status is not None:
            if self.peer_registry.is_peer_fresh_live(profile.peer_id, self.live_ttl_ms):
                freshness_score = 1.0

            failure_score = min(status.consecutive_failures / 3.0, 1.0)

            if status.last_rtt_ms is not None:
                latency_score = min(status.last_rtt_ms / 1000.0, 1.0)

        policy = self.routing_policy
        routing_score = (
            policy.quality_weight * weighted_quality
            + policy.freshness_weight * freshness_score
            + policy.failure_weight * failure_score
            + policy.latency_weight * latency_score
        )
        routing_score = max(0.0, min(1.0, routing_score))
        score_breakdown = {
            "capability_match": {
                "value": round(weighted_quality, 4),
                "weight": policy.quality_weight,
                "contribution": round(policy.quality_weight * weighted_quality, 4),
            },
            "fresh_profile": {
                "value": round(freshness_score, 4),
                "weight": policy.freshness_weight,
                "contribution": round(policy.freshness_weight * freshness_score, 4),
            },
            "recent_failures": {
                "value": round(failure_score, 4),
                "weight": policy.failure_weight,
                "contribution": round(policy.failure_weight * failure_score, 4),
            },
            "latency": {
                "value": round(latency_score, 4),
                "weight": policy.latency_weight,
                "contribution": round(policy.latency_weight * latency_score, 4),
            },
        }

        log(
            "ROUTING",
            f"Routing score breakdown peer_id={profile.peer_id} "
            f"capability_scores={capability_scores} "
            f"node_scores={self._profile_scores_for(profile, capability_scores)} "
            f"weighted_quality={weighted_quality:.2f} "
            f"freshness={freshness_score:.2f} "
            f"failure={failure_score:.2f} latency={latency_score:.2f} "
            f"policy={policy.name} routing_score={routing_score:.2f}",
        )

        return routing_score, weighted_quality, score_breakdown

    async def _evaluate_scored_candidate(
        self,
        profile: NodeProfile,
        capability_scores: dict[str, float],
        source: str,
        recommended_by: list[dict] | None = None,
        timeout_s: float = 2.0,
    ) -> tuple[str, NodeProfile, float, float, str, dict] | None:
        if self.health_service is not None:
            result = await self.health_service.check_peer(
                self.host,
                ID.from_base58(profile.peer_id),
                timeout_s=timeout_s,
            )

            if not result.ok:
                return None

        routing_score, weighted_quality, score_breakdown = self._scored_candidate_routing_score(
            profile,
            capability_scores,
        )

        trace = self._candidate_trace(
            "remote",
            profile,
            routing_score,
            weighted_quality,
            source,
            capability_scores,
            score_breakdown,
            recommended_by=recommended_by,
        )

        return "remote", profile, routing_score, weighted_quality, source, trace
