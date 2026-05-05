"""
seed_emotion_nl2sql_examples.py

Inject canonical NL2SQL success exemplars + durable guidance into Chroma 2
so the NL2SQL agent stops claiming "predominant fear" / "prevailing anger"
on topics where 97%+ of posts have dominant_emotion = NULL.

Idempotent on re-run: each guide has a stable id; success exemplars are
deduped by Chroma 2's similarity-based conflict policy
(NL2SQL_CONFLICT_SIM_LOW / HIGH).

Usage:
    docker exec society-analysis-project-update-api-1 \\
        python -m scripts.seed_emotion_nl2sql_examples
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

import structlog

from services.embeddings_service import EmbeddingsService
from services.nl2sql_memory import NL2SQLMemory

log = structlog.get_logger(__name__)


@dataclass
class _SuccessSeed:
    nl: str
    sql: str
    table_hints: list[str]


@dataclass
class _GuideSeed:
    rule_id: str
    text: str
    category: str = "rule"
    priority: int = 60


# Use a literal placeholder that the NL2SQL planner replaces from
# topic_id_hints. Real topic_ids look like "topic_70f1b88b8816".
SUCCESS_SEEDS: list[_SuccessSeed] = [
    _SuccessSeed(
        nl="What is the dominant emotion in topic <TOPIC_ID>? Include the unclassified share so the picture is honest.",
        sql=(
            "SELECT COALESCE(dominant_emotion, 'unclassified') AS emotion,\n"
            "       COUNT(*) AS post_count,\n"
            "       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct\n"
            "FROM posts_v2\n"
            "WHERE topic_id = '<TOPIC_ID>'\n"
            "GROUP BY 1\n"
            "ORDER BY post_count DESC"
        ),
        table_hints=["posts_v2"],
    ),
    _SuccessSeed(
        nl="Show the emotion distribution for topic <TOPIC_ID> only among posts that were actually classified.",
        sql=(
            "SELECT dominant_emotion,\n"
            "       COUNT(*) AS post_count,\n"
            "       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct_of_classified\n"
            "FROM posts_v2\n"
            "WHERE topic_id = '<TOPIC_ID>'\n"
            "  AND dominant_emotion IS NOT NULL\n"
            "GROUP BY dominant_emotion\n"
            "ORDER BY post_count DESC"
        ),
        table_hints=["posts_v2"],
    ),
    _SuccessSeed(
        nl="How prevalent is fear in topic <TOPIC_ID>? Give me the count and the share of total posts.",
        sql=(
            "SELECT\n"
            "  COUNT(*) FILTER (WHERE dominant_emotion = 'fear') AS fear_count,\n"
            "  COUNT(*) FILTER (WHERE dominant_emotion IS NULL) AS unclassified_count,\n"
            "  COUNT(*) AS total_posts,\n"
            "  ROUND(100.0 * COUNT(*) FILTER (WHERE dominant_emotion = 'fear')\n"
            "        / NULLIF(COUNT(*), 0), 2) AS fear_pct_of_total\n"
            "FROM posts_v2\n"
            "WHERE topic_id = '<TOPIC_ID>'"
        ),
        table_hints=["posts_v2"],
    ),
    _SuccessSeed(
        nl="Across the entire dataset, what fraction of posts have a non-null dominant_emotion?",
        sql=(
            "SELECT\n"
            "  COUNT(*) FILTER (WHERE dominant_emotion IS NOT NULL) AS classified,\n"
            "  COUNT(*) AS total,\n"
            "  ROUND(100.0 * COUNT(*) FILTER (WHERE dominant_emotion IS NOT NULL)\n"
            "        / NULLIF(COUNT(*), 0), 2) AS classified_pct\n"
            "FROM posts_v2"
        ),
        table_hints=["posts_v2"],
    ),
    _SuccessSeed(
        nl="Top 5 topics by fear-density: which topics have the highest share of fear posts (computed against TOTAL posts in the topic, not just classified)?",
        sql=(
            "SELECT t.topic_id, t.label, t.post_count,\n"
            "       COUNT(p.*) FILTER (WHERE p.dominant_emotion = 'fear') AS fear_count,\n"
            "       ROUND(100.0 * COUNT(p.*) FILTER (WHERE p.dominant_emotion = 'fear')\n"
            "             / NULLIF(t.post_count, 0), 2) AS fear_share_pct\n"
            "FROM topics_v2 t\n"
            "JOIN posts_v2 p ON p.topic_id = t.topic_id\n"
            "GROUP BY t.topic_id, t.label, t.post_count\n"
            "HAVING t.post_count >= 10\n"
            "ORDER BY fear_share_pct DESC NULLS LAST\n"
            "LIMIT 5"
        ),
        table_hints=["topics_v2", "posts_v2"],
    ),
]


GUIDE_SEEDS: list[_GuideSeed] = [
    _GuideSeed(
        rule_id="emotion_null_handling",
        text=(
            "EMOTION-NULL HANDLING (hard rule, posts_v2.dominant_emotion):\n"
            "About 97% of rows in posts_v2 have dominant_emotion IS NULL. When the "
            "user asks about emotion distribution, sentiment, or 'what people feel':\n"
            "  1. NEVER write a query that silently drops NULLs without showing the "
            "     unclassified share. Either include 'COALESCE(dominant_emotion, "
            "     ''unclassified'')' in the SELECT, or compute pct against the FULL "
            "     row count (not just non-null).\n"
            "  2. Use 'COUNT(*) FILTER (WHERE dominant_emotion = ''X'')' over a "
            "     blanket WHERE filter when you need both the X count and the "
            "     overall total in the same row.\n"
            "  3. topics_v2.dominant_emotion is a cluster-time MODE label, not a "
            "     ground-truth aggregate. Do not aggregate emotion from topics_v2; "
            "     compute it from posts_v2.dominant_emotion at query time."
        ),
        priority=80,
    ),
    _GuideSeed(
        rule_id="emotion_predominant_claim",
        text=(
            "WHEN NOT TO CLAIM 'predominant' / 'prevailing' EMOTION:\n"
            "Do not produce SQL that supports a 'topic feels X' answer unless the "
            "result row makes the claim defensible. Specifically the answer should "
            "be allowed only when emotion X's share of TOTAL posts in the topic "
            "(not non-null only) is >= 30%, OR when the user explicitly asked "
            "'among classified posts, which emotion dominates'. Otherwise return "
            "the full distribution including the unclassified bucket and let the "
            "report writer interpret it. The MODE on topics_v2.dominant_emotion is "
            "NOT sufficient evidence."
        ),
        priority=70,
    ),
]


def main() -> int:
    embeddings = EmbeddingsService()
    memory = NL2SQLMemory()

    for guide in GUIDE_SEEDS:
        emb = embeddings.embed(guide.text)
        rid = memory.upsert_guidance(
            rule_id=guide.rule_id,
            text=guide.text,
            embedding=emb,
            category=guide.category,
            priority=guide.priority,
        )
        print(f"guide:{guide.rule_id} -> {rid}")

    for seed in SUCCESS_SEEDS:
        text_for_embedding = f"NL: {seed.nl}\nSQL: {seed.sql}"
        emb = embeddings.embed(text_for_embedding)
        rid = memory.upsert_success(
            nl_query=seed.nl,
            sql_query=seed.sql,
            embedding=emb,
            table_hints=seed.table_hints,
        )
        print(f"success:{seed.nl[:60]}... -> {rid}")

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
