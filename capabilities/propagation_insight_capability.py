"""PropagationInsightCapability — 'Who is driving this? Is it coordinated?'

Reads propagation_summary + community_analysis via Tools. No LLM call in
this first version — AnswerComposer handles the natural-language rendering.
"""

from __future__ import annotations

from typing import Optional

from pydantic import Field

from capabilities.base import (
    Capability, CapabilityInput, CapabilityOutput, register_capability,
)
from tools.graph_tools import (
    query_topic_graph, QueryTopicGraphInput, CommunityView,
    get_social_metrics, GetSocialMetricsInput,
    get_propagation_summary, GetPropagationSummaryInput,
)


class PropagationInsightInput(CapabilityInput):
    run_id: str = "latest"
    topic_id: Optional[str] = None
    top_k_communities: int = 5


class PropagationInsightOutput(CapabilityOutput):
    run_id: str
    source: str
    topic_id: Optional[str] = None
    post_count: int = 0
    unique_accounts: int = 0
    velocity: float = 0.0
    coordinated_pairs: int = 0
    bridge_influence_ratio: float = 0.0
    account_role_summary: dict[str, int] = Field(default_factory=dict)
    community_count: int = 0
    echo_chamber_count: int = 0
    modularity: Optional[float] = None
    communities: list[CommunityView] = Field(default_factory=list)
    anomaly_detected: bool = False
    anomaly_description: Optional[str] = None


class PropagationInsightCapability(Capability):
    name = "propagation_analysis"
    Input = PropagationInsightInput
    Output = PropagationInsightOutput

    def run(self, input: PropagationInsightInput) -> PropagationInsightOutput:
        graph = query_topic_graph(
            QueryTopicGraphInput(
                run_id=input.run_id,
                topic_id=input.topic_id,
                top_k_communities=input.top_k_communities,
            )
        )
        prop_out = get_propagation_summary(
            GetPropagationSummaryInput(run_id=input.run_id)
        )
        propagation = prop_out.propagation_summary
        metrics = get_social_metrics(
            GetSocialMetricsInput(run_id=input.run_id)
        ).metrics or {}

        return PropagationInsightOutput(
            run_id=graph.run_id,
            source=graph.source,
            topic_id=input.topic_id,
            post_count=propagation.get("post_count", 0),
            unique_accounts=propagation.get("unique_accounts")
                or graph.unique_accounts,
            velocity=propagation.get("velocity", 0.0),
            coordinated_pairs=propagation.get("coordinated_pairs")
                or graph.coordinated_pairs,
            bridge_influence_ratio=propagation.get("bridge_influence_ratio", 0.0)
                or metrics.get("bridge_influence_ratio", 0.0),
            account_role_summary=propagation.get("account_role_summary") or {},
            community_count=graph.community_count,
            echo_chamber_count=graph.echo_chamber_count,
            modularity=graph.modularity,
            communities=graph.communities,
            anomaly_detected=propagation.get("anomaly_detected", False),
            anomaly_description=propagation.get("anomaly_description"),
        )


register_capability(PropagationInsightCapability())
