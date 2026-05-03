"""
Topic clusterer - redesign-2026-05 Phase 2.7.

Replaces v1's claim-based clustering. v2 clusters POSTS directly by
embedding their merged_text (post + folded image OCR/caption) and assigns
`post.topic_id` in place. The first representative post of each cluster
becomes the topic centroid label.

Algorithm: K-Means via scikit-learn (`KMeans` from sklearn.cluster).
- k = max(min_clusters, min(max_clusters, ceil(n_posts / target_per_cluster)))
- Falls back gracefully when sklearn is unavailable or n_posts < min_posts.

Output is a `list[TopicCluster]`; the v2 pipeline then:
  - calls `pg.upsert_topic_v2` for each cluster
  - calls `kuzu.upsert_topic` + `add_belongs_to_topic` for each (post, topic)
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

import openai
import structlog

from config import OPENAI_API_KEY, OPENAI_MODEL
from models.post import Post
from services.embeddings_service import EmbeddingsService

log = structlog.get_logger(__name__)


_LABEL_SYSTEM = """You name a cluster of social-media posts.
Given 3-8 sample post texts from one cluster, output STRICT JSON:
  {"label": "<3-7 word topic title in Title Case>"}

Rules:
- Describe the THEME / NARRATIVE, not the most-mentioned entity.
- Bad: "BBC", "Reuters" (these are media outlets, not topics).
- Good: "Vaccine Misinformation", "Climate Summit Outcome",
        "Macroeconomic Recovery News".
- Keep the label concise (<= 7 words). No quotes. No prefix like "Topic:".
"""


@dataclass
class TopicCluster:
    topic_id: str
    label: str
    post_ids: list[str] = field(default_factory=list)
    dominant_emotion: Optional[str] = None
    centroid_text: str = ""

    def post_count(self) -> int:
        return len(self.post_ids)


class TopicClusterer:
    def __init__(
        self,
        embeddings: Optional[EmbeddingsService] = None,
        *,
        min_posts: int = 4,
        target_per_cluster: int = 8,
        min_clusters: int = 2,
        max_clusters: int = 20,
        client: Optional[openai.OpenAI] = None,
        model: str = OPENAI_MODEL,
        use_llm_labels: bool = True,
    ) -> None:
        self._embeddings = embeddings or EmbeddingsService()
        self._min_posts = min_posts
        self._target_per_cluster = target_per_cluster
        self._min_clusters = min_clusters
        self._max_clusters = max_clusters
        self._client = client or openai.OpenAI(api_key=OPENAI_API_KEY)
        self._model = model
        self._use_llm_labels = use_llm_labels

    # ── Public ─────────────────────────────────────────────────────────────────

    def cluster(self, posts: list[Post]) -> list[TopicCluster]:
        """Cluster posts and assign `post.topic_id` in-place."""
        if len(posts) < self._min_posts:
            log.info("topic_clusterer.skip_too_few", n=len(posts))
            return []

        try:
            from sklearn.cluster import KMeans  # type: ignore
            import numpy as np  # type: ignore
        except ImportError:
            log.error(
                "topic_clusterer.sklearn_missing",
                hint="pip install scikit-learn numpy",
            )
            return []

        texts = [p.merged_text() or p.text or "" for p in posts]
        vectors = self._embeddings.embed_batch(texts)
        if not vectors or len(vectors) != len(posts):
            log.warning("topic_clusterer.embed_failed")
            return []

        k = max(
            self._min_clusters,
            min(self._max_clusters,
                math.ceil(len(posts) / self._target_per_cluster)),
        )
        k = min(k, len(posts))  # k cannot exceed n

        try:
            arr = np.array(vectors)
            km = KMeans(n_clusters=k, n_init=4, random_state=42)
            labels = km.fit_predict(arr)
        except Exception as exc:
            log.error("topic_clusterer.kmeans_error", error=str(exc)[:120])
            return []

        # Pass 1: group posts by cluster index (topic_id assigned later)
        clusters: dict[int, TopicCluster] = {}
        for post, label in zip(posts, labels):
            label_int = int(label)
            cluster = clusters.get(label_int)
            if cluster is None:
                cluster = TopicCluster(topic_id="",  # backfilled below
                                       label=f"Topic {label_int + 1}")
                clusters[label_int] = cluster
            cluster.post_ids.append(post.id)

        # Pass 2: derive a stable, content-addressed topic_id from the
        # sorted member post ids. Same set of posts -> same topic_id
        # across pipeline runs. This lets users follow trends over days
        # without random UUIDs breaking continuity.
        for cluster in clusters.values():
            tid_hash = hashlib.sha256(
                "|".join(sorted(cluster.post_ids)).encode("utf-8")
            ).hexdigest()[:12]
            cluster.topic_id = f"topic_{tid_hash}"
        # Pass 3: assign topic_id to each post
        for post, label in zip(posts, labels):
            post.topic_id = clusters[int(label)].topic_id

        # Compute centroid text + dominant emotion per cluster
        post_by_id = {p.id: p for p in posts}
        for cluster in clusters.values():
            cluster_posts = [post_by_id[pid] for pid in cluster.post_ids]
            cluster.centroid_text = self._pick_centroid_text(cluster_posts)
            cluster.dominant_emotion = self._dominant_emotion(cluster_posts)
            cluster.label = self._make_label(cluster_posts)
            # LLM relabel if enabled (one cheap call per cluster)
            if self._use_llm_labels:
                better = self._llm_label(cluster_posts)
                if better:
                    cluster.label = better

        results = list(clusters.values())
        log.info("topic_clusterer.done",
                 posts=len(posts), topics=len(results), k=k)
        return results

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _pick_centroid_text(posts: list[Post]) -> str:
        """Return the longest text as a cheap centroid representative."""
        if not posts:
            return ""
        return max((p.text or "" for p in posts), key=len)[:300]

    @staticmethod
    def _dominant_emotion(posts: list[Post]) -> Optional[str]:
        emotions = [p.emotion for p in posts if p.emotion]
        if not emotions:
            return None
        most = Counter(emotions).most_common(1)
        return most[0][0] if most else None

    @staticmethod
    def _make_label(posts: list[Post]) -> str:
        """Cheap fallback label: first 6 words of the longest post.

        Only used when the LLM relabel call fails. We deliberately do NOT
        pick the most-mentioned entity here, because entity names like
        "BBC" or "Reuters" describe the source, not the topic narrative.
        """
        if not posts:
            return "Untitled Topic"
        longest = max((p.text or "" for p in posts), key=len)
        words = longest.split()[:6]
        return " ".join(words) if words else "Untitled Topic"

    def _llm_label(self, posts: list[Post]) -> Optional[str]:
        """One LLM call -> short narrative label. Returns None on failure."""
        if not posts:
            return None
        sample = posts[: max(3, min(8, len(posts)))]
        body = "\n".join(
            f"- {(p.text or '').strip()[:240]}" for p in sample
        )
        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                max_tokens=64,
                temperature=0.0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": _LABEL_SYSTEM},
                    {"role": "user", "content": body[:2000]},
                ],
            )
            raw = (resp.choices[0].message.content or "{}").strip()
            label = (json.loads(raw).get("label") or "").strip()
            if not label:
                return None
            # Strip a leading "Topic:" if the LLM added one anyway
            if label.lower().startswith("topic:"):
                label = label.split(":", 1)[1].strip()
            return label[:80] or None
        except Exception as exc:
            log.warning("topic_clusterer.llm_label_error",
                        error=str(exc)[:120])
            return None
