"""
Kuzu knowledge graph — claims, posts, accounts, topics, evidence relations.
Kuzu is embedded (zero-deployment), Cypher-compatible.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import kuzu
import structlog

from config import KUZU_DB_DIR

log = structlog.get_logger(__name__)


class KuzuService:
    def __init__(self, db_dir: str = KUZU_DB_DIR) -> None:
        # Ensure the *parent* directory exists; Kuzu creates db_dir itself.
        Path(db_dir).parent.mkdir(parents=True, exist_ok=True)
        self._db = kuzu.Database(db_dir)
        self._conn = kuzu.Connection(self._db)
        self._init_schema()
        log.info("kuzu.initialized", db_dir=db_dir)

    def _init_schema(self) -> None:
        stmts = [
            # Node tables
            "CREATE NODE TABLE IF NOT EXISTS Account(id STRING, username STRING, PRIMARY KEY(id))",
            "CREATE NODE TABLE IF NOT EXISTS Post(id STRING, text STRING, PRIMARY KEY(id))",
            "CREATE NODE TABLE IF NOT EXISTS Claim(id STRING, text STRING, propagation_count INT64, PRIMARY KEY(id))",
            "CREATE NODE TABLE IF NOT EXISTS Topic(id STRING, label STRING, PRIMARY KEY(id))",
            "CREATE NODE TABLE IF NOT EXISTS ImageAsset(id STRING, image_type STRING, PRIMARY KEY(id))",
            "CREATE NODE TABLE IF NOT EXISTS Article(id STRING, title STRING, url STRING, PRIMARY KEY(id))",
            "CREATE NODE TABLE IF NOT EXISTS FactCheck(id STRING, title STRING, url STRING, PRIMARY KEY(id))",
            "CREATE NODE TABLE IF NOT EXISTS Community(id STRING, label STRING, PRIMARY KEY(id))",
            # Relationship tables
            "CREATE REL TABLE IF NOT EXISTS Posted(FROM Account TO Post)",
            "CREATE REL TABLE IF NOT EXISTS ContainsClaim(FROM Post TO Claim)",
            "CREATE REL TABLE IF NOT EXISTS BelongsToTopic(FROM Post TO Topic)",
            "CREATE REL TABLE IF NOT EXISTS UsesImage(FROM Post TO ImageAsset)",
            "CREATE REL TABLE IF NOT EXISTS SupportedBy(FROM Claim TO Article)",
            "CREATE REL TABLE IF NOT EXISTS ContradictedBy(FROM Claim TO FactCheck)",
            "CREATE REL TABLE IF NOT EXISTS BelongsToCommunity(FROM Account TO Community)",
            "CREATE REL TABLE IF NOT EXISTS VariantOf(FROM ImageAsset TO ImageAsset)",
            "CREATE REL TABLE IF NOT EXISTS SameAs(FROM Claim TO Claim)",
            "CREATE REL TABLE IF NOT EXISTS RelatedTo(FROM Claim TO Claim)",
            "CREATE REL TABLE IF NOT EXISTS ClaimBelongsToTopic(FROM Claim TO Topic)",
            # Phase 1: cross-community coordination
            "CREATE REL TABLE IF NOT EXISTS CoordinatedWith(FROM Account TO Account)",
            # Phase 2: named entity graph
            "CREATE NODE TABLE IF NOT EXISTS Entity(id STRING, name STRING, entity_type STRING, PRIMARY KEY(id))",
            "CREATE REL TABLE IF NOT EXISTS Mentions(FROM Claim TO Entity)",
            "CREATE REL TABLE IF NOT EXISTS CoOccursWith(FROM Entity TO Entity)",
        ]
        for stmt in stmts:
            try:
                self._conn.execute(stmt)
            except Exception as exc:
                # Ignore "already exists" errors from Kuzu
                if "already exist" not in str(exc).lower():
                    log.warning("kuzu.schema_error", stmt=stmt, error=str(exc))

        # Phase 2: extend Entity with mention_count
        alter_stmts2 = [
            "ALTER TABLE Entity ADD mention_count INT64 DEFAULT 1",
        ]
        for stmt in alter_stmts2:
            try:
                self._conn.execute(stmt)
            except Exception:
                pass

        # Phase 0: extend tables with new columns (idempotent — ignore if already added)
        alter_stmts = [
            # Account.role — ORIGINATOR / AMPLIFIER / BRIDGE / PASSIVE
            "ALTER TABLE Account ADD role STRING DEFAULT 'PASSIVE'",
            # Post.emotion — fear / anger / hope / disgust / neutral
            "ALTER TABLE Post ADD emotion STRING DEFAULT 'neutral'",
            "ALTER TABLE Post ADD emotion_score DOUBLE DEFAULT 0.0",
            # Community: add isolation_score + size
            "ALTER TABLE Community ADD isolation_score DOUBLE DEFAULT 0.0",
            "ALTER TABLE Community ADD size INT64 DEFAULT 0",
        ]
        for stmt in alter_stmts:
            try:
                self._conn.execute(stmt)
            except Exception:
                # Column already exists — expected on subsequent runs
                pass

    # ── Node upserts ───────────────────────────────────────────────────────────

    def upsert_account(self, account_id: str, username: str) -> None:
        self._safe_execute(
            "MERGE (a:Account {id: $id}) SET a.username = $username",
            {"id": account_id, "username": username},
        )

    def upsert_post(self, post_id: str, text: str) -> None:
        self._safe_execute(
            "MERGE (p:Post {id: $id}) SET p.text = $text",
            {"id": post_id, "text": text},
        )

    def upsert_claim(self, claim_id: str, text: str,
                     propagation_count: int = 1) -> None:
        self._safe_execute(
            "MERGE (c:Claim {id: $id}) SET c.text = $text, "
            "c.propagation_count = $pc",
            {"id": claim_id, "text": text, "pc": propagation_count},
        )

    def upsert_topic(self, topic_id: str, label: str) -> None:
        self._safe_execute(
            "MERGE (t:Topic {id: $id}) SET t.label = $label",
            {"id": topic_id, "label": label},
        )

    def upsert_article(self, article_id: str, title: str, url: str = "") -> None:
        self._safe_execute(
            "MERGE (a:Article {id: $id}) SET a.title = $title, a.url = $url",
            {"id": article_id, "title": title, "url": url},
        )

    def upsert_fact_check(self, fc_id: str, title: str, url: str = "") -> None:
        self._safe_execute(
            "MERGE (f:FactCheck {id: $id}) SET f.title = $title, f.url = $url",
            {"id": fc_id, "title": title, "url": url},
        )

    # ── Relationship creation ──────────────────────────────────────────────────

    def add_posted(self, account_id: str, post_id: str) -> None:
        self._safe_execute(
            "MATCH (a:Account {id: $aid}), (p:Post {id: $pid}) "
            "MERGE (a)-[:Posted]->(p)",
            {"aid": account_id, "pid": post_id},
        )

    def add_contains_claim(self, post_id: str, claim_id: str) -> None:
        self._safe_execute(
            "MATCH (p:Post {id: $pid}), (c:Claim {id: $cid}) "
            "MERGE (p)-[:ContainsClaim]->(c)",
            {"pid": post_id, "cid": claim_id},
        )

    def add_belongs_to_topic(self, post_id: str, topic_id: str) -> None:
        self._safe_execute(
            "MATCH (p:Post {id: $pid}), (t:Topic {id: $tid}) "
            "MERGE (p)-[:BelongsToTopic]->(t)",
            {"pid": post_id, "tid": topic_id},
        )

    def add_claim_to_topic(self, claim_id: str, topic_id: str) -> None:
        self._safe_execute(
            "MATCH (c:Claim {id: $cid}), (t:Topic {id: $tid}) "
            "MERGE (c)-[:ClaimBelongsToTopic]->(t)",
            {"cid": claim_id, "tid": topic_id},
        )

    def add_supported_by(self, claim_id: str, article_id: str) -> None:
        self._safe_execute(
            "MATCH (c:Claim {id: $cid}), (a:Article {id: $aid}) "
            "MERGE (c)-[:SupportedBy]->(a)",
            {"cid": claim_id, "aid": article_id},
        )

    def add_contradicted_by(self, claim_id: str, fc_id: str) -> None:
        self._safe_execute(
            "MATCH (c:Claim {id: $cid}), (f:FactCheck {id: $fcid}) "
            "MERGE (c)-[:ContradictedBy]->(f)",
            {"cid": claim_id, "fcid": fc_id},
        )

    def add_same_as(self, claim_id_a: str, claim_id_b: str) -> None:
        self._safe_execute(
            "MATCH (a:Claim {id: $a}), (b:Claim {id: $b}) "
            "MERGE (a)-[:SameAs]->(b)",
            {"a": claim_id_a, "b": claim_id_b},
        )

    def add_related_to(self, claim_id_a: str, claim_id_b: str) -> None:
        self._safe_execute(
            "MATCH (a:Claim {id: $a}), (b:Claim {id: $b}) "
            "MERGE (a)-[:RelatedTo]->(b)",
            {"a": claim_id_a, "b": claim_id_b},
        )

    # ── Queries ────────────────────────────────────────────────────────────────

    def get_claim_posts(self, claim_id: str) -> list[dict]:
        result = self._safe_execute(
            "MATCH (p:Post)-[:ContainsClaim]->(c:Claim {id: $cid}) "
            "RETURN p.id AS post_id, p.text AS text",
            {"cid": claim_id},
        )
        return result or []

    def get_claim_evidence(self, claim_id: str) -> list[dict]:
        supporting = self._safe_execute(
            "MATCH (c:Claim {id: $cid})-[:SupportedBy]->(a:Article) "
            "RETURN a.id AS id, a.title AS title, 'supports' AS stance",
            {"cid": claim_id},
        ) or []
        contradicting = self._safe_execute(
            "MATCH (c:Claim {id: $cid})-[:ContradictedBy]->(f:FactCheck) "
            "RETURN f.id AS id, f.title AS title, 'contradicts' AS stance",
            {"cid": claim_id},
        ) or []
        return supporting + contradicting

    def get_topic_claims(self, topic_id: str) -> list[dict]:
        result = self._safe_execute(
            "MATCH (c:Claim)-[:ClaimBelongsToTopic]->(t:Topic {id: $tid}) "
            "RETURN c.id AS claim_id, c.text AS text, "
            "c.propagation_count AS propagation_count",
            {"tid": topic_id},
        )
        return result or []

    def get_all_topics(self) -> list[dict]:
        result = self._safe_execute(
            "MATCH (t:Topic) RETURN t.id AS topic_id, t.label AS label"
        )
        return result or []

    def get_coordinated_accounts(self, min_shared_claims: int = 2) -> list[dict]:
        """
        Find account pairs that independently posted the same claims.
        Returns list of {account1, account2, shared_claim_count, sample_claims}.
        Aggregation done in Python because Kuzu doesn't support HAVING.
        """
        from collections import defaultdict
        rows = self._safe_execute(
            "MATCH (a:Account)-[:Posted]->(p:Post)-[:ContainsClaim]->(c:Claim) "
            "RETURN a.id AS account_id, c.id AS claim_id, c.text AS claim_text"
        ) or []

        # Group claims → accounts
        claim_accounts: dict[str, set] = defaultdict(set)
        claim_texts: dict[str, str] = {}
        for row in rows:
            claim_accounts[row["claim_id"]].add(row["account_id"])
            claim_texts[row["claim_id"]] = row["claim_text"]

        # Count shared claims per account pair
        pair_shared: dict[tuple, list] = defaultdict(list)
        for cid, accounts in claim_accounts.items():
            accs = sorted(accounts)
            for i in range(len(accs)):
                for j in range(i + 1, len(accs)):
                    pair_shared[(accs[i], accs[j])].append(cid)

        result = []
        for (a1, a2), cids in pair_shared.items():
            if len(cids) >= min_shared_claims:
                result.append({
                    "account1": a1,
                    "account2": a2,
                    "shared_claim_count": len(cids),
                    "sample_claims": [
                        claim_texts[c][:80] for c in cids[:3]
                    ],
                })
        result.sort(key=lambda x: x["shared_claim_count"], reverse=True)
        return result

    def get_claim_related_network(
        self, claim_id: str, depth: int = 2
    ) -> list[dict]:
        """
        Follow RelatedTo edges up to `depth` hops from a claim.
        Returns related claim nodes with their propagation counts.
        """
        result = self._safe_execute(
            "MATCH (start:Claim {id: $cid})-[:RelatedTo*1..2]->(related:Claim) "
            "RETURN DISTINCT related.id AS id, related.text AS text, "
            "related.propagation_count AS propagation_count",
            {"cid": claim_id},
        )
        return result or []

    def get_post_topics(self, post_id: str) -> list[dict]:
        """Return topics a given post belongs to."""
        result = self._safe_execute(
            "MATCH (p:Post {id: $pid})-[:BelongsToTopic]->(t:Topic) "
            "RETURN t.id AS topic_id, t.label AS label",
            {"pid": post_id},
        )
        return result or []

    def get_topic_posts(self, topic_id: str, limit: int = 50) -> list[dict]:
        result = self._safe_execute(
            "MATCH (p:Post)-[:BelongsToTopic]->(t:Topic {id: $tid}) "
            "RETURN p.id AS post_id, p.text AS text LIMIT $lim",
            {"tid": topic_id, "lim": limit},
        )
        return result or []

    # ── Phase 0: Account role classification ──────────────────────────────────

    def upsert_account_role(self, account_id: str, role: str) -> None:
        """Set the propagation role for an account node."""
        self._safe_execute(
            "MATCH (a:Account {id: $id}) SET a.role = $role",
            {"id": account_id, "role": role},
        )

    def get_account_roles(self) -> list[dict]:
        """Return all accounts with their role labels."""
        result = self._safe_execute(
            "MATCH (a:Account) RETURN a.id AS account_id, a.username AS username, "
            "a.role AS role"
        )
        return result or []

    def get_accounts_for_topic(self, topic_id: str) -> list[dict]:
        """Return accounts that posted in a given topic (via Post→Topic edge)."""
        result = self._safe_execute(
            "MATCH (a:Account)-[:Posted]->(p:Post)-[:BelongsToTopic]->(t:Topic {id: $tid}) "
            "RETURN DISTINCT a.id AS account_id, a.username AS username",
            {"tid": topic_id},
        )
        return result or []

    # ── Phase 0: Claim mutation chain ─────────────────────────────────────────

    def get_claim_mutation_chain(self, topic_id: str) -> list[dict]:
        """
        Return claims in a topic ordered by propagation_count (ascending proxy
        for 'earlier / less-spread'), following RelatedTo edges to expose the
        narrative mutation chain.

        Returns list of dicts:
          {claim_id, text, propagation_count, related_count}
        Sorted oldest-first (lowest propagation_count first).
        """
        # All claims in the topic with their propagation counts
        claims = self._safe_execute(
            "MATCH (c:Claim)-[:ClaimBelongsToTopic]->(t:Topic {id: $tid}) "
            "RETURN c.id AS claim_id, c.text AS text, "
            "c.propagation_count AS propagation_count",
            {"tid": topic_id},
        ) or []

        if not claims:
            return []

        # Count outgoing RelatedTo edges for each claim (mutation depth signal)
        claim_ids = [c["claim_id"] for c in claims]
        related_counts: dict[str, int] = {cid: 0 for cid in claim_ids}
        for cid in claim_ids:
            rels = self._safe_execute(
                "MATCH (c:Claim {id: $cid})-[:RelatedTo]->(r:Claim) "
                "RETURN count(r) AS cnt",
                {"cid": cid},
            )
            if rels:
                related_counts[cid] = rels[0].get("cnt", 0) or 0

        enriched = [
            {**c, "related_count": related_counts.get(c["claim_id"], 0)}
            for c in claims
        ]
        # Sort: least-propagated first (original claims), then by related_count
        enriched.sort(key=lambda x: (x["propagation_count"] or 0, x["related_count"]))
        return enriched

    # ── Phase 1: Community management ─────────────────────────────────────────

    def upsert_community(
        self,
        community_id: str,
        label: str,
        isolation_score: float = 0.0,
        size: int = 0,
    ) -> None:
        self._safe_execute(
            "MERGE (c:Community {id: $id}) "
            "SET c.label = $label, c.isolation_score = $iso, c.size = $size",
            {"id": community_id, "label": label,
             "iso": isolation_score, "size": size},
        )

    def add_belongs_to_community(self, account_id: str, community_id: str) -> None:
        self._safe_execute(
            "MATCH (a:Account {id: $aid}), (c:Community {id: $cid}) "
            "MERGE (a)-[:BelongsToCommunity]->(c)",
            {"aid": account_id, "cid": community_id},
        )

    def add_coordinated_with(self, account_id_a: str, account_id_b: str) -> None:
        self._safe_execute(
            "MATCH (a:Account {id: $a}), (b:Account {id: $b}) "
            "MERGE (a)-[:CoordinatedWith]->(b)",
            {"a": account_id_a, "b": account_id_b},
        )

    def get_account_topic_edges(self) -> list[dict]:
        """
        Return all (account_id, topic_id) pairs for Phase 1 community detection.
        Used to build the Account–Topic bipartite graph.
        """
        result = self._safe_execute(
            "MATCH (a:Account)-[:Posted]->(p:Post)-[:BelongsToTopic]->(t:Topic) "
            "RETURN DISTINCT a.id AS account_id, t.id AS topic_id"
        )
        return result or []

    def get_all_accounts(self) -> list[dict]:
        result = self._safe_execute(
            "MATCH (a:Account) RETURN a.id AS account_id, a.username AS username, "
            "a.role AS role"
        )
        return result or []

    def get_communities(self) -> list[dict]:
        result = self._safe_execute(
            "MATCH (c:Community) RETURN c.id AS community_id, c.label AS label, "
            "c.isolation_score AS isolation_score, c.size AS size"
        )
        return result or []

    def get_community_accounts(self, community_id: str) -> list[dict]:
        result = self._safe_execute(
            "MATCH (a:Account)-[:BelongsToCommunity]->(c:Community {id: $cid}) "
            "RETURN a.id AS account_id, a.username AS username, a.role AS role",
            {"cid": community_id},
        )
        return result or []

    # ── Phase 2: Entity graph ──────────────────────────────────────────────────

    def upsert_entity(
        self,
        entity_id: str,
        name: str,
        entity_type: str,
        mention_count: int = 1,
    ) -> None:
        self._safe_execute(
            "MERGE (e:Entity {id: $id}) "
            "SET e.name = $name, e.entity_type = $etype, e.mention_count = $mc",
            {"id": entity_id, "name": name, "etype": entity_type, "mc": mention_count},
        )

    def add_claim_mentions_entity(self, claim_id: str, entity_id: str) -> None:
        self._safe_execute(
            "MATCH (c:Claim {id: $cid}), (e:Entity {id: $eid}) "
            "MERGE (c)-[:Mentions]->(e)",
            {"cid": claim_id, "eid": entity_id},
        )

    def add_entity_co_occurs_with(
        self, entity_a_id: str, entity_b_id: str, count: int = 1
    ) -> None:
        self._safe_execute(
            "MATCH (a:Entity {id: $a}), (b:Entity {id: $b}) "
            "MERGE (a)-[:CoOccursWith]->(b)",
            {"a": entity_a_id, "b": entity_b_id},
        )

    def get_top_entities(self, limit: int = 20) -> list[dict]:
        result = self._safe_execute(
            "MATCH (e:Entity) RETURN e.id AS entity_id, e.name AS name, "
            "e.entity_type AS entity_type, e.mention_count AS mention_count "
            "ORDER BY e.mention_count DESC LIMIT $lim",
            {"lim": limit},
        )
        return result or []

    def get_entity_co_occurrences(self, limit: int = 20) -> list[dict]:
        result = self._safe_execute(
            "MATCH (a:Entity)-[:CoOccursWith]->(b:Entity) "
            "RETURN a.name AS entity_a, b.name AS entity_b, "
            "a.entity_type AS type_a, b.entity_type AS type_b "
            "LIMIT $lim",
            {"lim": limit},
        )
        return result or []

    # ── Internal ───────────────────────────────────────────────────────────────

    def _safe_execute(self, query: str,
                      params: Optional[dict] = None) -> Optional[list]:
        try:
            result = self._conn.execute(query, params or {})
            if result is None:
                return None
            col_names = result.get_column_names()
            rows = []
            while result.has_next():
                row = result.get_next()
                rows.append(dict(zip(col_names, row)))
            return rows
        except Exception as exc:
            log.error("kuzu.query_error", query=query, error=str(exc))
            return None
