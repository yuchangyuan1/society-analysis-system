"""
Knowledge Workspace — claim-retrieve + emotion-analyze + persuasion skills.

Responsibilities:
  - Two-stage claim deduplication (embedding similarity → LLM judge)
  - Evidence pack assembly (supporting / contradicting / uncertain)
  - Claim normalization and identity resolution
  - Phase 0: Per-post emotion classification
  - Phase 2: Meme persuasion feature analysis (Task 1.4)
  - Phase 2: Named entity extraction + co-occurrence graph (Task 1.3 supplement)
"""
from __future__ import annotations

import json
import uuid
from typing import Optional, Any

import openai
import structlog

from config import (
    OPENAI_API_KEY, OPENAI_MODEL,
    CLAIM_EMBED_SIM_HIGH, CLAIM_EMBED_SIM_LOW,
)
from models.claim import Claim, ClaimEvidence, DeduplicationResult
from models.persuasion import PersuasionFeatures, NamedEntity, EntityCoOccurrence
from services.chroma_service import ChromaService
from services.embeddings_service import EmbeddingsService
from services.kuzu_service import KuzuService
from services.postgres_service import PostgresService

log = structlog.get_logger(__name__)

_DEDUP_SYSTEM = """You are a semantic claim deduplication expert.
Given two factual claims, respond with exactly one word: SAME, RELATED, or DIFFERENT.
- SAME: the claims assert the same fact (even if worded differently)
- RELATED: the claims are on the same topic but make different assertions
- DIFFERENT: the claims are unrelated
Respond with only the word. No explanation."""

_PERSUASION_SYSTEM = """You are a media manipulation analyst specialising in persuasion tactics.
Given a factual claim from social media, identify the persuasion techniques it employs.

Return a JSON object with exactly these fields:
  "emotional_appeal":   float 0.0-1.0 (overall emotional charge)
  "fear_framing":       float 0.0-1.0 (specifically fear-driven framing)
  "simplicity_score":   float 0.0-1.0 (1.0 = very simple, easily shareable)
  "authority_reference": boolean (true if it cites or implies an authority/expert/official)
  "urgency_markers":    integer (count of urgency words: BREAKING, CONFIRMED, NOW, ALERT, etc.)
  "identity_trigger":   boolean (true if it activates us-vs-them / group identity)
  "top_tactic":         string (the single dominant tactic: fear_framing | simplicity | authority | urgency | identity | emotional_appeal | none)
  "explanation":        string (1 sentence explaining the key persuasion mechanism)

Output ONLY the JSON. No explanation outside the JSON."""


_ENTITY_SYSTEM = """You are a named entity recognition system for fact-checking research.
Given a numbered list of factual claims, extract all named entities across ALL claims.
Normalise name variants (e.g. "Bibi" and "Netanyahu" are the same person — pick the canonical name).
Count how many distinct claims each entity appears in.

Entity types:
  PERSON  — real named individuals
  ORG     — organisations, institutions, agencies, companies
  PLACE   — countries, cities, regions, facilities
  EVENT   — named events (e.g. elections, summits, wars)

Return ONLY a JSON array of objects. Each object:
  {"name": "string", "type": "PERSON|ORG|PLACE|EVENT", "mention_count": <int>}

If no named entities are present, return [].
Do NOT output any explanation outside the JSON array."""


_EMOTION_SYSTEM = """You are an emotion analyst for social media posts.
Given a social media post, identify the primary emotional tone and its intensity.

Emotion categories:
  fear    — posts expressing danger, threat, alarm, worst-case framing
  anger   — posts expressing outrage, blame, hostility, frustration
  hope    — posts expressing optimism, solutions, positive outcome
  disgust — posts expressing revulsion, moral condemnation, loathing
  neutral — factual, informational, or no dominant emotional tone

Return a JSON object with exactly two fields:
  "emotion": one of [fear, anger, hope, disgust, neutral]
  "score": float 0.0–1.0 indicating intensity (1.0 = extremely strong)

Output ONLY the JSON object. No explanation."""


_NORMALIZE_SYSTEM = """You are a misinformation analyst helping a fact-checking system.
Your task is to identify and extract the specific factual assertions made in social media posts
so that each claim can be verified, fact-checked, and countered if false.

Extracting a claim does NOT mean endorsing it — the purpose is precisely to analyse and debunk misinformation.

Given a raw text excerpt, return a JSON array of the factual assertions it makes (true or false).
Rules:
- Include claims even if they are false, misleading, or unverified — the system will fact-check them.
- Each claim should be a single declarative sentence.
- Return ONLY a JSON array of strings, e.g.: ["Claim 1.", "Claim 2."]
- If the text contains no factual assertions (e.g. pure opinion, question, greeting), return [].
- Do NOT add any explanation or preamble — output the JSON array only."""


class KnowledgeAgent:
    def __init__(
        self,
        pg: PostgresService,
        chroma: ChromaService,
        kuzu: KuzuService,
        embedder: EmbeddingsService,
        wikipedia=None,
        news_service=None,
    ) -> None:
        self._pg = pg
        self._chroma = chroma
        self._kuzu = kuzu
        self._embedder = embedder
        self._wikipedia = wikipedia
        self._news_service = news_service
        self._claude = openai.OpenAI(api_key=OPENAI_API_KEY)

    # ── Skill: claim-retrieve ─────────────────────────────────────────────────

    def extract_and_deduplicate_claims(
        self, text: str, post_id: Optional[str] = None
    ) -> list[Claim]:
        """
        1. Extract candidate claims from text via LLM.
        2. For each, run two-stage deduplication against existing claims.
        3. SAME → merge; RELATED → add edge; DIFFERENT → insert new.
        Returns list of resolved Claim objects.
        """
        raw_claims = self._extract_claims(text)
        resolved: list[Claim] = []
        for raw in raw_claims:
            claim = self._resolve_claim(raw, post_id=post_id)
            resolved.append(claim)
        return resolved

    def cluster_claims_into_topics(
        self, claims: list[Claim]
    ) -> list[dict[str, Any]]:
        """
        Group claims into semantic topic clusters using LLM.
        Writes Topic nodes + ClaimBelongsToTopic edges to Kuzu.

        Returns list of:
          { topic_id, label, claim_ids, claims }
        """
        if len(claims) < 2:
            return []

        # Build numbered claim list for LLM
        numbered = "\n".join(
            f"{i}. {c.normalized_text[:120]}"
            for i, c in enumerate(claims)
        )
        prompt = (
            "You are a topic analyst. Group the following claims into "
            "3-8 semantic topics. Claims on the same subject belong together.\n\n"
            f"{numbered}\n\n"
            "Return ONLY a JSON array. Each element: "
            '{"label": "<short topic name>", "indices": [<0-based ints>]}\n'
            "Every claim index must appear exactly once. No other output."
        )
        try:
            response = self._claude.chat.completions.create(
                model=OPENAI_MODEL,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = (response.choices[0].message.content or "[]").strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            start, end = raw.find("["), raw.rfind("]") + 1
            if start != -1 and end > start:
                raw = raw[start:end]
            groups: list[dict] = json.loads(raw) if raw else []
        except Exception as exc:
            log.error("knowledge.cluster_error", error=str(exc))
            return []

        topics: list[dict[str, Any]] = []
        used: set[int] = set()
        for g in groups:
            label = g.get("label", "Unnamed topic")
            indices: list[int] = [
                int(i) for i in g.get("indices", [])
                if isinstance(i, (int, float)) and 0 <= int(i) < len(claims)
            ]
            indices = [i for i in indices if i not in used]
            if not indices:
                continue
            used.update(indices)

            topic_id = str(uuid.uuid4())
            self._kuzu.upsert_topic(topic_id, label)
            cluster_claims = [claims[i] for i in indices]
            for c in cluster_claims:
                self._kuzu.add_claim_to_topic(c.id, topic_id)

            # Write Post→Topic edges so graph queries can reach posts directly
            posts_in_topic: set[str] = set()
            for c in cluster_claims:
                for row in self._kuzu.get_claim_posts(c.id):
                    posts_in_topic.add(row["post_id"])
            for post_id in posts_in_topic:
                self._kuzu.add_belongs_to_topic(post_id, topic_id)

            topics.append({
                "topic_id": topic_id,
                "label": label,
                "claim_ids": [c.id for c in cluster_claims],
                "claims": cluster_claims,
            })
            log.info(
                "knowledge.topic_created",
                topic_id=topic_id,
                label=label,
                claim_count=len(cluster_claims),
                post_count=len(posts_in_topic),
            )

        log.info("knowledge.clustering_done",
                 topic_count=len(topics), claim_count=len(claims))
        return topics

    def fetch_evidence_for_topics(
        self,
        topics: list[dict],
        news_service,
        max_queries_per_topic: int = 2,
        max_articles_per_query: int = 3,
    ) -> int:
        """
        For each topic, generate targeted search queries, fetch articles from
        authoritative news/fact-check sites, and store them in the Chroma
        articles collection so evidence packs can find them.

        If the primary queries return no articles (e.g. the topic label uses
        inflammatory language that wire services don't mirror), a simplified
        fallback query using the first four words of the label is tried.

        Returns the total number of new article chunks stored.
        """
        total_chunks = 0
        stored_article_ids: set[str] = set()

        for topic in topics:
            label = topic.get("label", "")
            topic_id = topic.get("topic_id", "")
            if not label:
                continue

            queries = self._generate_evidence_queries(label, max_queries_per_topic)
            log.info("knowledge.evidence_fetch_start",
                     topic=label[:60], queries=queries)

            topic_new_articles = 0
            for query in queries:
                articles = news_service.search_and_fetch(
                    query, max_results=max_articles_per_query
                )
                for article in articles:
                    aid = article["article_id"]
                    if aid in stored_article_ids:
                        continue
                    stored_article_ids.add(aid)
                    chunks = self._chunk_and_store_article(article, topic_id)
                    total_chunks += chunks
                    topic_new_articles += 1

            # ── Zero-result fallback ──────────────────────────────────────
            # If both primary queries returned nothing (e.g. inflammatory
            # label words not in wire-service headlines), try a simplified
            # query built from the first four neutral words of the label.
            if topic_new_articles == 0:
                # Strip common inflammatory/editorial words
                _STRIP = {
                    "war", "crimes", "illegal", "expansionism", "genocide",
                    "collapse", "catastrophic", "criminal", "evil",
                }
                words = [
                    w for w in label.replace(",", "").split()
                    if w.lower() not in _STRIP
                ][:5]
                fallback_q = " ".join(words) if words else label.split()[0]
                log.info("knowledge.evidence_fetch_fallback",
                         topic=label[:60], fallback=fallback_q)
                articles = news_service.search_and_fetch(
                    fallback_q, max_results=max_articles_per_query
                )
                for article in articles:
                    aid = article["article_id"]
                    if aid in stored_article_ids:
                        continue
                    stored_article_ids.add(aid)
                    chunks = self._chunk_and_store_article(article, topic_id)
                    total_chunks += chunks

        log.info("knowledge.evidence_fetch_done",
                 topics=len(topics),
                 articles=len(stored_article_ids),
                 chunks=total_chunks)
        return total_chunks

    def build_evidence_pack(self, claim: Claim) -> Claim:
        """
        Retrieve supporting and contradicting evidence for a claim
        from Chroma and Kuzu; populate claim.supporting/contradicting/uncertain.

        P0-3: If no evidence is found in the internal Chroma/Kuzu layer, fall
        back to Wikipedia (Tier A) and optionally NewsSearch (Tier B) so the
        proportion of zero-evidence claims drops from ~80% to ≤40%.
        """
        embed = self._embedder.embed(claim.normalized_text)
        # Vector search in articles (Tier: internal_chroma)
        article_hits = self._chroma.query_articles(embed, n_results=8)
        for hit in article_hits:
            article_id = hit["metadata"].get("article_id", hit["id"])
            sim = ChromaService.cosine_similarity(hit["distance"])
            if sim < 0.50:
                continue
            # Use a simple heuristic for stance until a full fact-check DB exists
            stance = self._judge_stance(claim.normalized_text, hit["document"])
            ev = ClaimEvidence(
                article_id=article_id,
                article_title=hit["metadata"].get("title", ""),
                article_url=hit["metadata"].get("url", ""),
                source_name=hit["metadata"].get("source", ""),
                stance=stance,
                snippet=hit["document"][:300],
                source_tier="internal_chroma",
            )
            if stance == "supports":
                claim.supporting_evidence.append(ev)
                # Persist SupportedBy edge in the knowledge graph
                self._kuzu.upsert_article(
                    article_id, ev.article_title, ev.article_url
                )
                self._kuzu.add_supported_by(claim.id, article_id)
            elif stance == "contradicts":
                claim.contradicting_evidence.append(ev)
                # Persist ContradictedBy edge in the knowledge graph
                self._kuzu.upsert_fact_check(
                    article_id, ev.article_title, ev.article_url
                )
                self._kuzu.add_contradicted_by(claim.id, article_id)
            else:
                claim.uncertain_evidence.append(ev)
        # Graph evidence
        graph_ev = self._kuzu.get_claim_evidence(claim.id)
        for row in graph_ev:
            ev = ClaimEvidence(
                article_id=row["id"],
                article_title=row.get("title", ""),
                stance=row["stance"],
                source_tier="internal_chroma",
            )
            if row["stance"] == "supports":
                claim.supporting_evidence.append(ev)
            else:
                claim.contradicting_evidence.append(ev)

        # ── Zero-evidence fallback ────────────────────────────────────────
        if not claim.supporting_evidence and not claim.contradicting_evidence:
            self._augment_with_fallback_evidence(claim)

        log.info(
            "knowledge.evidence_pack",
            claim_id=claim.id,
            **claim.evidence_summary(),
        )
        return claim

    def _augment_with_fallback_evidence(self, claim: Claim) -> None:
        """
        Tier A: Wikipedia REST summary for the claim's topic.
        Tier B: NewsSearch direct query (only if still empty).
        Any evidence found here is marked uncertain unless _judge_stance is
        confident enough to classify it.
        """
        # Tier A — Wikipedia
        if self._wikipedia is not None:
            try:
                wiki = self._wikipedia.fetch_summary(claim.normalized_text)
                if wiki:
                    stance = self._judge_stance(
                        claim.normalized_text, wiki.get("snippet", "")
                    )
                    ev = ClaimEvidence(
                        article_id=wiki["article_id"],
                        article_title=wiki.get("title"),
                        article_url=wiki.get("url"),
                        source_name=wiki.get("source_name", "Wikipedia"),
                        stance=stance,
                        snippet=wiki.get("snippet"),
                        source_tier="wikipedia",
                    )
                    self._append_by_stance(claim, ev, stance)
                    log.info(
                        "knowledge.evidence_fallback_wiki_hit",
                        claim_id=claim.id,
                        title=wiki.get("title"),
                    )
            except Exception as exc:
                log.warning(
                    "knowledge.evidence_fallback_wiki_error",
                    claim_id=claim.id, error=str(exc),
                )

        if claim.supporting_evidence or claim.contradicting_evidence:
            return

        # Tier B — NewsSearch direct query (only if Wikipedia also missed)
        if self._news_service is not None:
            try:
                articles = self._news_service.search_and_fetch(
                    claim.normalized_text[:120], max_results=3
                )
                for article in articles[:3]:
                    text = article.get("content") or article.get("summary") or ""
                    stance = self._judge_stance(claim.normalized_text, text[:600])
                    ev = ClaimEvidence(
                        article_id=article.get("article_id", ""),
                        article_title=article.get("title", ""),
                        article_url=article.get("url", ""),
                        source_name=article.get("source", "news"),
                        stance=stance,
                        snippet=(text or "")[:300],
                        source_tier="news",
                    )
                    self._append_by_stance(claim, ev, stance)
                log.info(
                    "knowledge.evidence_fallback_news_hits",
                    claim_id=claim.id, count=len(articles),
                )
            except Exception as exc:
                log.warning(
                    "knowledge.evidence_fallback_news_error",
                    claim_id=claim.id, error=str(exc),
                )

    @staticmethod
    def _append_by_stance(claim: Claim, ev: ClaimEvidence, stance: str) -> None:
        if stance == "supports":
            claim.supporting_evidence.append(ev)
        elif stance == "contradicts":
            claim.contradicting_evidence.append(ev)
        else:
            claim.uncertain_evidence.append(ev)

    def build_evidence_for_topic(
        self,
        topic: dict,
        claim_lookup: dict[str, int],
        unique_claims: list[Claim],
        n_results: int = 10,
    ) -> int:
        """
        One vector search per topic: embed the topic label, retrieve the top
        matching article chunks from Chroma, then distribute evidence to every
        claim that belongs to this topic.

        Returns the number of claims that gained at least one evidence item.
        """
        label = topic.get("label", "")
        if not label:
            return 0

        topic_embed = self._embedder.embed(label)
        hits = self._chroma.query_articles(topic_embed, n_results=n_results)

        # Filter hits below similarity threshold
        relevant = [
            h for h in hits
            if ChromaService.cosine_similarity(h["distance"]) >= 0.45
        ]
        if not relevant:
            log.info("knowledge.topic_evidence_none", topic=label[:60])
            return 0

        enriched = 0
        for claim in topic.get("claims", []):
            idx = claim_lookup.get(claim.id)
            if idx is None:
                continue
            c = unique_claims[idx]
            before = c.has_sufficient_evidence(min_items=1)
            for hit in relevant:
                article_id = hit["metadata"].get("article_id", hit["id"])
                stance = self._judge_stance(c.normalized_text, hit["document"])
                ev = ClaimEvidence(
                    article_id=article_id,
                    article_title=hit["metadata"].get("title", ""),
                    article_url=hit["metadata"].get("url", ""),
                    source_name=hit["metadata"].get("source", ""),
                    stance=stance,
                    snippet=hit["document"][:300],
                )
                if stance == "supports":
                    c.supporting_evidence.append(ev)
                    self._kuzu.upsert_article(article_id, ev.article_title, ev.article_url)
                    self._kuzu.add_supported_by(c.id, article_id)
                elif stance == "contradicts":
                    c.contradicting_evidence.append(ev)
                    self._kuzu.upsert_fact_check(article_id, ev.article_title, ev.article_url)
                    self._kuzu.add_contradicted_by(c.id, article_id)
                else:
                    c.uncertain_evidence.append(ev)
            unique_claims[idx] = c
            if not before and c.has_sufficient_evidence(min_items=1):
                enriched += 1

        log.info("knowledge.topic_evidence_done",
                 topic=label[:60], hits=len(relevant), enriched=enriched)
        return enriched

    # ── Skill: emotion-analyze (Phase 0) ──────────────────────────────────────

    def classify_post_emotions(self, posts: "list") -> None:
        """
        Classify the primary emotion for each post in-place.
        Sets post.emotion and post.emotion_score.
        Processes up to 50 posts (API cost guard).

        Emotion taxonomy: fear | anger | hope | disgust | neutral
        """
        from models.post import Post  # local import avoids circular dependency
        for post in posts[:50]:
            if not isinstance(post, Post) or post.emotion:
                continue  # already classified
            try:
                resp = self._claude.chat.completions.create(
                    model=OPENAI_MODEL,
                    max_tokens=64,
                    messages=[
                        {"role": "system", "content": _EMOTION_SYSTEM},
                        {"role": "user", "content": post.text[:800]},
                    ],
                )
                raw = (resp.choices[0].message.content or "{}").strip()
                if raw.startswith("```"):
                    raw = raw.split("```")[1]
                    if raw.startswith("json"):
                        raw = raw[4:]
                    raw = raw.strip()
                start, end = raw.find("{"), raw.rfind("}") + 1
                if start != -1 and end > start:
                    raw = raw[start:end]
                data = json.loads(raw) if raw else {}
                emotion = data.get("emotion", "neutral")
                if emotion not in ("fear", "anger", "hope", "disgust", "neutral"):
                    emotion = "neutral"
                post.emotion = emotion
                post.emotion_score = min(1.0, max(0.0, float(data.get("score", 0.5))))
            except Exception as exc:
                log.warning("knowledge.emotion_error",
                            post_id=post.id, error=str(exc)[:80])
                post.emotion = "neutral"
                post.emotion_score = 0.0

    # ── Skill: meme-persuasion-analyze (Phase 2, Task 1.4) ────────────────────

    def analyze_persuasion(self, claims: list[Claim]) -> list[PersuasionFeatures]:
        """
        Analyse persuasion features for each of the provided claims.
        Returns PersuasionFeatures per claim, sorted by virality_score desc.
        Processes up to 10 claims (API cost guard).
        """
        results: list[PersuasionFeatures] = []
        for claim in claims[:10]:
            feat = self._analyze_claim_persuasion(claim)
            results.append(feat)
        results.sort(key=lambda f: f.virality_score, reverse=True)
        log.info("knowledge.persuasion_done", claims_analyzed=len(results))
        return results

    def _analyze_claim_persuasion(self, claim: Claim) -> PersuasionFeatures:
        """Run LLM persuasion analysis on a single claim."""
        try:
            resp = self._claude.chat.completions.create(
                model=OPENAI_MODEL,
                max_tokens=256,
                messages=[
                    {"role": "system", "content": _PERSUASION_SYSTEM},
                    {"role": "user", "content": claim.normalized_text[:600]},
                ],
            )
            raw = (resp.choices[0].message.content or "{}").strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            start, end = raw.find("{"), raw.rfind("}") + 1
            if start != -1 and end > start:
                raw = raw[start:end]
            data = json.loads(raw) if raw else {}

            emotional_appeal = float(data.get("emotional_appeal", 0.0))
            fear_framing = float(data.get("fear_framing", 0.0))
            simplicity = float(data.get("simplicity_score", 0.5))
            authority = bool(data.get("authority_reference", False))
            urgency = int(data.get("urgency_markers", 0))
            identity = bool(data.get("identity_trigger", False))
            tactic = data.get("top_tactic", "none")
            explanation = data.get("explanation", "")

            # Composite virality score (weighted sum)
            virality = min(1.0, (
                fear_framing * 0.30
                + emotional_appeal * 0.20
                + simplicity * 0.20
                + (0.15 if authority else 0.0)
                + min(urgency, 5) * 0.02
                + (0.13 if identity else 0.0)
            ))

            return PersuasionFeatures(
                claim_id=claim.id,
                claim_text=claim.normalized_text[:120],
                emotional_appeal=round(emotional_appeal, 3),
                fear_framing=round(fear_framing, 3),
                simplicity_score=round(simplicity, 3),
                authority_reference=authority,
                urgency_markers=urgency,
                identity_trigger=identity,
                virality_score=round(virality, 3),
                top_persuasion_tactic=tactic,
                explanation=explanation,
            )
        except Exception as exc:
            log.warning("knowledge.persuasion_error",
                        claim_id=claim.id, error=str(exc)[:80])
            return PersuasionFeatures(claim_id=claim.id,
                                      claim_text=claim.normalized_text[:120])

    # ── Skill: entity-extract (Phase 2, Task 1.3 supplement) ─────────────────

    def extract_entities(
        self,
        claims: list[Claim],
    ) -> tuple[list[NamedEntity], list[EntityCoOccurrence]]:
        """
        Extract named entities from all claims in a single batched LLM call so that
        name variants are normalised and mention_count is accurate across claims.
        Builds co-occurrence pairs and persists to Kuzu.
        """
        from collections import defaultdict

        capped = claims[:30]
        # Build numbered claim list for the LLM
        batch_text = "\n".join(
            f"{i+1}. {c.normalized_text[:300]}" for i, c in enumerate(capped)
        )
        raw_items = self._extract_entities_batch(batch_text)

        entity_map: dict[str, NamedEntity] = {}
        for item in raw_items:
            name = item.get("name", "").strip()
            etype = item.get("type", "UNKNOWN").upper()
            count = max(1, int(item.get("mention_count", 1)))
            if not name:
                continue
            key = name.lower()
            if key not in entity_map:
                eid = str(uuid.uuid4())
                entity_map[key] = NamedEntity(
                    entity_id=eid, name=name, entity_type=etype, mention_count=count
                )
                self._kuzu.upsert_entity(eid, name, etype)
            else:
                entity_map[key].mention_count = max(entity_map[key].mention_count, count)
                self._kuzu.upsert_entity(
                    entity_map[key].entity_id, name, etype,
                    mention_count=entity_map[key].mention_count,
                )

        # Build claim→entity edges and co-occurrence map from text matching
        claim_entities: dict[str, list[str]] = {}
        for claim in capped:
            eid_list: list[str] = []
            text_lower = claim.normalized_text.lower()
            for key, ent in entity_map.items():
                if key in text_lower:
                    eid_list.append(ent.entity_id)
                    self._kuzu.add_claim_mentions_entity(claim.id, ent.entity_id)
            claim_entities[claim.id] = eid_list

        co_map: dict[tuple, list[str]] = defaultdict(list)
        for claim_id, eids in claim_entities.items():
            for i in range(len(eids)):
                for j in range(i + 1, len(eids)):
                    pair = (min(eids[i], eids[j]), max(eids[i], eids[j]))
                    co_map[pair].append(claim_id)

        co_occurrences: list[EntityCoOccurrence] = []
        eid_to_entity = {e.entity_id: e for e in entity_map.values()}
        for (ea, eb), cids in co_map.items():
            if len(cids) >= 2:
                ent_a = eid_to_entity.get(ea)
                ent_b = eid_to_entity.get(eb)
                if ent_a and ent_b:
                    co_occurrences.append(EntityCoOccurrence(
                        entity_a_id=ea,
                        entity_a_name=ent_a.name,
                        entity_b_id=eb,
                        entity_b_name=ent_b.name,
                        co_occurrence_count=len(cids),
                        shared_claim_ids=cids[:5],
                    ))
                    self._kuzu.add_entity_co_occurs_with(ea, eb, len(cids))

        co_occurrences.sort(key=lambda c: c.co_occurrence_count, reverse=True)
        log.info(
            "knowledge.entities_extracted",
            entities=len(entity_map),
            co_occurrences=len(co_occurrences),
        )
        return list(entity_map.values()), co_occurrences

    def _extract_entities_batch(self, batch_text: str) -> list[dict]:
        """Batch LLM call: extract entities with mention_count from all claims at once."""
        try:
            resp = self._claude.chat.completions.create(
                model=OPENAI_MODEL,
                max_tokens=512,
                messages=[
                    {"role": "system", "content": _ENTITY_SYSTEM},
                    {"role": "user", "content": batch_text[:3000]},
                ],
            )
            raw = (resp.choices[0].message.content or "[]").strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            start, end = raw.find("["), raw.rfind("]") + 1
            if start != -1 and end > start:
                raw = raw[start:end]
            items: list[dict] = json.loads(raw) if raw else []
            valid_types = {"PERSON", "ORG", "PLACE", "EVENT"}
            return [
                item for item in items
                if isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and item.get("type", "").upper() in valid_types
            ]
        except Exception as exc:
            log.warning("knowledge.entity_batch_error", error=str(exc))
            return []

    def _extract_entities_from_text(self, text: str) -> list[tuple[str, str]]:
        """Legacy single-claim extraction (kept for external callers)."""
        items = self._extract_entities_batch(text)
        valid_types = {"PERSON", "ORG", "PLACE", "EVENT"}
        return [
            (item["name"], item.get("type", "UNKNOWN").upper())
            for item in items
            if item.get("type", "").upper() in valid_types
        ][:10]

    # ── Internal ───────────────────────────────────────────────────────────────

    def _extract_claims(self, text: str) -> list[str]:
        try:
            response = self._claude.chat.completions.create(
                model=OPENAI_MODEL,
                max_tokens=512,
                messages=[
                    {"role": "system", "content": _NORMALIZE_SYSTEM},
                    {"role": "user", "content": text},
                ],
            )
            raw = response.choices[0].message.content or "[]"
            raw = raw.strip()
            # Strip markdown code fences
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            # Extract JSON array if preceded by explanatory text
            start = raw.find("[")
            end = raw.rfind("]") + 1
            if start != -1 and end > start:
                raw = raw[start:end]
            result = json.loads(raw) if raw else []
            return result if isinstance(result, list) else []
        except Exception as exc:
            log.error("knowledge.extract_claims_error", error=str(exc))
            return []

    def _resolve_claim(self, raw_claim: str,
                       post_id: Optional[str] = None) -> Claim:
        embed = self._embedder.embed(raw_claim)
        candidates = self._chroma.query_claims(embed, n_results=5)
        for cand in candidates:
            sim = ChromaService.cosine_similarity(cand["distance"])
            if sim >= CLAIM_EMBED_SIM_HIGH:
                # Stage 2: LLM judge
                result = self._llm_dedup(raw_claim, cand["document"])
                if result == "SAME":
                    # Merge: increment propagation
                    claim_id = cand["id"]
                    prop_count = 1
                    try:
                        self._pg.increment_claim_propagation(claim_id)
                        row = self._pg.get_claim(claim_id)
                        prop_count = row["propagation_count"] if row else 1
                        if post_id:
                            self._pg.link_post_claim(post_id, claim_id)
                    except Exception as exc:
                        log.warning("knowledge.pg_skip", claim_id=claim_id, error=str(exc)[:80])
                    self._kuzu.upsert_claim(claim_id, cand["document"], prop_count)
                    if post_id:
                        self._kuzu.add_contains_claim(post_id, claim_id)
                    return Claim(
                        id=claim_id,
                        normalized_text=cand["document"],
                        propagation_count=prop_count,
                    )
                elif result == "RELATED":
                    # New claim + related edge
                    new_claim = self._create_claim(raw_claim, embed, post_id)
                    self._kuzu.add_related_to(new_claim.id, cand["id"])
                    new_claim.related_claim_ids.append(cand["id"])
                    return new_claim
            elif sim < CLAIM_EMBED_SIM_LOW:
                break  # No plausible match below this threshold
        return self._create_claim(raw_claim, embed, post_id)

    def _create_claim(
        self,
        text: str,
        embed: list[float],
        post_id: Optional[str],
    ) -> Claim:
        claim_id = str(uuid.uuid4())
        # Postgres — non-fatal; Chroma + Kuzu are the primary stores
        try:
            # Omit first_seen_post if the post may not be in PG yet
            self._pg.upsert_claim(claim_id, text, first_seen_post=None)
            if post_id:
                self._pg.link_post_claim(post_id, claim_id)
        except Exception as exc:
            log.warning("knowledge.pg_skip", claim_id=claim_id, error=str(exc)[:80])
        self._chroma.upsert_claim(claim_id, embed, text)
        self._kuzu.upsert_claim(claim_id, text)
        if post_id:
            self._kuzu.add_contains_claim(post_id, claim_id)
        log.info("knowledge.new_claim", claim_id=claim_id, text=text[:80])
        return Claim(id=claim_id, normalized_text=text, first_seen_post=post_id)

    def _llm_dedup(self, claim_a: str, claim_b: str) -> DeduplicationResult:
        try:
            response = self._claude.chat.completions.create(
                model=OPENAI_MODEL,
                max_tokens=10,
                messages=[
                    {"role": "system", "content": _DEDUP_SYSTEM},
                    {"role": "user", "content": f"Claim A: {claim_a}\nClaim B: {claim_b}"},
                ],
            )
            answer = (response.choices[0].message.content or "DIFFERENT").strip().upper()
            if answer not in ("SAME", "RELATED", "DIFFERENT"):
                return "DIFFERENT"
            # answer is guaranteed to be DeduplicationResult at this point
            return answer  # type: ignore[return-value]  # Literal narrowing
        except Exception as exc:
            log.error("knowledge.llm_dedup_error", error=str(exc))
            return "DIFFERENT"

    def _generate_evidence_queries(self, topic_label: str, n: int = 2) -> list[str]:
        """
        Ask Claude to produce n short news-search queries that would surface
        authoritative fact-checks or background articles for this topic.
        """
        system = (
            "You are a fact-checking research assistant. Given a topic label, "
            "generate short search queries (≤8 words each) to find authoritative "
            "coverage on Reuters, AP News, BBC, or The Guardian. "
            "IMPORTANT: Use neutral, journalistic language that wire services "
            "actually use in headlines. Avoid inflammatory or editorial terms "
            "such as 'war crimes', 'genocide', 'illegal', 'expansionism', "
            "'catastrophic', 'collapse' — rephrase to factual equivalents "
            "(e.g. 'Israel Gaza military operations' instead of "
            "'Israel war crimes'). Focus on verifiable who/what/where facts. "
            f"Return ONLY a JSON array of {n} strings, no explanation."
        )
        try:
            resp = self._claude.chat.completions.create(
                model=OPENAI_MODEL,
                max_tokens=128,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": topic_label},
                ],
            )
            raw = (resp.choices[0].message.content or "[]").strip()
            start, end = raw.find("["), raw.rfind("]") + 1
            if start != -1 and end > start:
                raw = raw[start:end]
            queries: list[str] = json.loads(raw) if raw else []
            return [q for q in queries if isinstance(q, str)][:n] or [topic_label[:60]]
        except Exception as exc:
            log.warning("knowledge.evidence_query_gen_error", error=str(exc)[:80])
            return [topic_label[:60]]

    def _chunk_and_store_article(
        self, article: dict, topic_id: str, chunk_size: int = 600
    ) -> int:
        """
        Split article body into overlapping chunks, embed each, and upsert
        into the Chroma articles collection.  Returns number of chunks stored.
        """
        body: str = article.get("body", "").strip()
        if not body:
            return 0

        article_id: str = article["article_id"]
        title: str = article.get("title", "")
        url: str = article.get("url", "")
        source: str = article.get("source", "")

        # Split by paragraphs first, then hard-cut if too long
        paras = [p.strip() for p in body.split("\n") if p.strip()]
        chunks: list[str] = []
        current = ""
        for para in paras:
            if len(current) + len(para) + 1 <= chunk_size:
                current = (current + "\n" + para).strip()
            else:
                if current:
                    chunks.append(current)
                # If single para is too long, hard-cut it
                while len(para) > chunk_size:
                    chunks.append(para[:chunk_size])
                    para = para[chunk_size:]
                current = para
        if current:
            chunks.append(current)

        stored = 0
        for i, chunk in enumerate(chunks):
            try:
                embed = self._embedder.embed(chunk)
                self._chroma.upsert_article(
                    article_id=article_id,
                    chunk_id=str(i),
                    embedding=embed,
                    text=chunk,
                    metadata={
                        "title": title,
                        "url": url,
                        "source": source,
                        "topic_id": topic_id,
                        "authoritative": True,
                    },
                )
                stored += 1
            except Exception as exc:
                log.warning("knowledge.chunk_store_error",
                            article_id=article_id, chunk=i, error=str(exc)[:80])

        _safe = lambda s: s.encode("ascii", "replace").decode("ascii")
        log.info("knowledge.article_stored",
                 article_id=article_id,
                 title=_safe(title[:60]),
                 url=_safe(url[:80]),
                 chunks=stored)
        return stored

    def _judge_stance(self, claim: str, evidence_text: str) -> str:
        """Lightweight LLM stance classification for a (claim, evidence) pair.

        The prompt explicitly teaches the model that authoritative neutral
        descriptions of a subject *rebut* extraordinary claims about that
        subject (e.g. a Wikipedia description of AIPAC contradicts "AIPAC runs
        America"). Without this carve-out the classifier was defaulting to
        `neutral` for ~95% of conspiracy-vs-encyclopedia pairs, leaving the
        actionable_counter_evidence_rate near zero even when evidence was
        retrieved successfully.
        """
        system = (
            "You are a fact-checking analyst. Classify how an evidence passage "
            "relates to a factual claim. Reply with exactly one word: "
            "supports | contradicts | neutral.\n\n"
            "Decision rules:\n"
            "- supports: the evidence directly or indirectly affirms the claim.\n"
            "- contradicts: the evidence (a) directly denies the claim, OR "
            "(b) describes the claim's subject in a way materially inconsistent "
            "with the claim. Extraordinary claims (conspiratorial control, "
            "hidden numbers, 'runs the country', secret manipulation) are "
            "contradicted by authoritative descriptions that present the "
            "subject in ordinary factual terms, because those descriptions "
            "rebut the extraordinary framing.\n"
            "- neutral: the evidence is on a different topic, or genuinely says "
            "nothing about the claim either way.\n\n"
            "Examples:\n"
            "Claim: 'AIPAC runs America.' Evidence: 'AIPAC is a pro-Israel "
            "lobbying group that works to influence U.S. policy.' -> contradicts\n"
            "Claim: 'Rothschild controls global finance.' Evidence: 'Rothschild "
            "is a European banking family founded in the 18th century.' -> "
            "contradicts\n"
            "Claim: 'Tyler Oliveira was banned from Patreon.' Evidence: 'Tyler "
            "Oliveira is an American YouTuber who makes challenge videos.' -> "
            "neutral\n"
            "Claim: 'Chocolate cures cancer.' Evidence: 'Chocolate is made from "
            "cocoa beans harvested in West Africa.' -> neutral\n"
            "Claim: 'Vaccines cause autism.' Evidence: 'Multiple large-scale "
            "studies have found no link between vaccines and autism.' -> "
            "contradicts"
        )
        prompt = (
            f"Claim: {claim}\n\n"
            f"Evidence: {evidence_text[:500]}"
        )
        try:
            response = self._claude.chat.completions.create(
                model=OPENAI_MODEL,
                max_tokens=10,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            )
            ans = (response.choices[0].message.content or "neutral").strip().lower()
            if ans in ("supports", "contradicts"):
                return ans
            return "neutral"
        except Exception:
            return "neutral"
