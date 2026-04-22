"""EmotionInsightCapability — 'What's the emotional tone? Which topics are angriest?'

Aggregates per-topic emotion_distribution into an overall view and
optionally filters to one topic. Uses LLM only for a short
`interpretation` line, at temperature 0.3.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

import openai
from config import OPENAI_API_KEY, OPENAI_MODEL

from capabilities.base import (
    Capability, CapabilityInput, CapabilityOutput, register_capability
)
from tools.run_query_tools import get_topics, GetTopicsInput


class TopicEmotionBrief(BaseModel):
    topic_id: str
    label: str
    dominant_emotion: str = ""
    emotion_distribution: dict[str, float] = Field(default_factory=dict)


class EmotionInsightInput(CapabilityInput):
    run_id: str = "latest"
    topic_id: Optional[str] = None


class EmotionInsightOutput(CapabilityOutput):
    run_id: str
    source: str
    overall_emotion_distribution: dict[str, float] = Field(default_factory=dict)
    dominant_emotion: str = ""
    topic_emotions: list[TopicEmotionBrief] = Field(default_factory=list)
    interpretation: str = ""


_INTERPRET_SYS = (
    "You are a social media emotion analyst. In ONE short sentence "
    "(Chinese or English matching the user's language; <=30 words), "
    "summarize the emotional state of this run. Do not add caveats. "
    "Do not recommend actions."
)


def _interpret(distribution: dict[str, float], dominant: str) -> str:
    if not distribution:
        return ""
    try:
        client = openai.OpenAI(api_key=OPENAI_API_KEY)
        payload = (
            f"dominant_emotion={dominant}; "
            f"distribution={distribution}"
        )
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            max_tokens=120,
            temperature=0.3,
            messages=[
                {"role": "system", "content": _INTERPRET_SYS},
                {"role": "user", "content": payload},
            ],
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        # Fall back to a deterministic template.
        top = sorted(distribution.items(), key=lambda kv: kv[1], reverse=True)[:2]
        joined = ", ".join(f"{k}={v:.2f}" for k, v in top)
        return f"Dominant emotion: {dominant}; leading shares: {joined}."


class EmotionInsightCapability(Capability):
    name = "emotion_analysis"
    description = (
        "Summarize the emotional profile of a topic or an entire run: "
        "dominant emotion, emotion distribution (anger / fear / joy / etc.), "
        "and an interpretation. Answers 'how's the mood', "
        "'which topics are angriest', or 'emotion distribution'."
    )
    example_utterances = [
        "这个话题的情绪怎么样",
        "how angry is the discussion",
        "what's the emotional profile of topic X",
        "情绪分布",
    ]
    tags = ["emotion", "sentiment"]
    Input = EmotionInsightInput
    Output = EmotionInsightOutput

    def run(self, input: EmotionInsightInput) -> EmotionInsightOutput:
        topics_out = get_topics(
            GetTopicsInput(run_id=input.run_id, sort_by="post_count")
        )

        if input.topic_id is not None:
            filtered = [t for t in topics_out.topics if t.topic_id == input.topic_id]
        else:
            filtered = topics_out.topics

        # Aggregate emotion distribution weighted by post_count.
        total_posts = sum(max(t.post_count, 0) for t in filtered) or 0
        overall: dict[str, float] = {}
        if total_posts > 0:
            for t in filtered:
                w = t.post_count / total_posts
                for emo, share in (t.emotion_distribution or {}).items():
                    overall[emo] = overall.get(emo, 0.0) + share * w

        dominant = (
            max(overall.items(), key=lambda kv: kv[1])[0]
            if overall else ""
        )

        topic_emotions = [
            TopicEmotionBrief(
                topic_id=t.topic_id,
                label=t.label,
                dominant_emotion=t.dominant_emotion,
                emotion_distribution=t.emotion_distribution,
            )
            for t in filtered
        ]

        interpretation = _interpret(overall, dominant)

        return EmotionInsightOutput(
            run_id=topics_out.run_id,
            source=topics_out.source,
            overall_emotion_distribution=overall,
            dominant_emotion=dominant,
            topic_emotions=topic_emotions,
            interpretation=interpretation,
        )


register_capability(EmotionInsightCapability())
