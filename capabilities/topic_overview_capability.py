"""TopicOverviewCapability — 'What's being discussed? What's trending?'

Thin wrapper around get_topics + get_run_summary. Does not call the LLM
in the first version; an optional LLM one-liner can be added later.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field

from capabilities.base import (
    Capability, CapabilityInput, CapabilityOutput, register_capability
)
from tools.run_query_tools import (
    get_run_summary, GetRunSummaryInput,
    get_topics, GetTopicsInput, TopicBrief,
)


class TopicOverviewInput(CapabilityInput):
    run_id: str = "latest"
    top_k: int = 5
    sort_by: Literal["post_count", "velocity", "misinfo_risk"] = "post_count"


class TopicOverviewOutput(CapabilityOutput):
    run_id: str
    source: str
    query_text: Optional[str] = None
    total_post_count: Optional[int] = None
    topics: list[TopicBrief] = Field(default_factory=list)


class TopicOverviewCapability(Capability):
    name = "topic_overview"
    description = (
        "Return the top-K trending topics in a run, ranked by post_count / "
        "velocity / misinfo_risk. Answers 'what are people discussing', "
        "'what's hot today', or 'show me the topics in run X'."
    )
    example_utterances = [
        "今天大家在讨论什么",
        "什么话题最热",
        "show me the top topics in the latest run",
        "list the trending discussions",
    ]
    tags = ["topic", "overview", "trending"]
    Input = TopicOverviewInput
    Output = TopicOverviewOutput

    def run(self, input: TopicOverviewInput) -> TopicOverviewOutput:
        summary = get_run_summary(GetRunSummaryInput(run_id=input.run_id))
        topics_out = get_topics(
            GetTopicsInput(
                run_id=input.run_id,
                top_k=input.top_k,
                sort_by=input.sort_by,
            )
        )
        return TopicOverviewOutput(
            run_id=topics_out.run_id,
            source=topics_out.source,
            query_text=summary.manifest.get("query_text"),
            total_post_count=summary.manifest.get("post_count"),
            topics=topics_out.topics,
        )


register_capability(TopicOverviewCapability())
