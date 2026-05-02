"""
Phase 1 (redesign-2026-05) unit tests.

Coverage:
- MultimodalAgent: sampling rules, budget enforcement, missing-image handling
- EntityExtractor: JSON parse tolerance, dedupe
- OfficialIngestionPipeline: chunk splitting, jsonl write
- PrecomputePipelineV2: end-to-end stage wiring (with mocks)

All LLM / Vision / OpenAI calls are mocked.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agents.entity_extractor import EntityExtractor
from agents.multimodal_agent import MultimodalAgent
from agents.official_ingestion_pipeline import (
    ChunkingConfig,
    OfficialIngestionPipeline,
    PipelineConfig,
    SourceConfig,
)
from models.entity import EntitySpan
from models.post import ImageAsset, Post


# ── MultimodalAgent ──────────────────────────────────────────────────────────

def _make_post(post_id: str, *, likes=0, replies=0, with_image=True) -> Post:
    images: list[ImageAsset] = []
    if with_image:
        images.append(ImageAsset(
            id=f"{post_id}_img",
            post_id=post_id,
            url=f"https://example.com/{post_id}.png",
        ))
    return Post(
        id=post_id,
        account_id="user1",
        text=f"text-{post_id}",
        like_count=likes,
        reply_count=replies,
        images=images,
    )


def test_multimodal_skips_low_engagement_posts():
    vision = MagicMock()
    vision.analyze_image.return_value = {
        "ocr_text": "X",
        "image_caption": "Y",
        "image_type": "photo",
    }
    agent = MultimodalAgent(vision=vision, min_likes=50, min_replies=20)
    posts = [
        _make_post("p1", likes=10, replies=5),    # too low -> skipped
        _make_post("p2", likes=100, replies=0),   # likes >= 50 -> run
        _make_post("p3", likes=0, replies=25),    # replies >= 20 -> run
    ]
    summary = agent.enrich_posts(posts)
    assert summary.images_processed == 2
    assert summary.images_skipped_engagement == 1
    assert posts[0].images[0].image_caption is None  # skipped
    assert posts[1].images[0].image_caption == "Y"
    assert posts[2].images[0].image_caption == "Y"


def test_multimodal_respects_daily_budget():
    vision = MagicMock()
    vision.analyze_image.return_value = {
        "ocr_text": "X", "image_caption": "Y", "image_type": "photo",
    }
    # Budget allows exactly 2 calls
    agent = MultimodalAgent(
        vision=vision,
        daily_budget_usd=0.04,
        cost_per_call_usd=0.02,
        min_likes=0, min_replies=0,  # all eligible
    )
    posts = [_make_post(f"p{i}", likes=100) for i in range(5)]
    summary = agent.enrich_posts(posts)
    assert summary.images_processed == 2
    assert summary.images_skipped_budget == 3


def test_multimodal_handles_post_without_images():
    vision = MagicMock()
    agent = MultimodalAgent(vision=vision, min_likes=0, min_replies=0)
    posts = [_make_post("p1", likes=100, with_image=False)]
    summary = agent.enrich_posts(posts)
    assert summary.posts_with_images == 0
    assert summary.images_processed == 0
    vision.analyze_image.assert_not_called()


def test_multimodal_idempotent_on_already_enriched():
    vision = MagicMock()
    vision.analyze_image.return_value = {
        "ocr_text": "X", "image_caption": "Y", "image_type": "photo",
    }
    agent = MultimodalAgent(vision=vision, min_likes=0, min_replies=0)
    post = _make_post("p1", likes=100)
    post.images[0].image_caption = "already done"
    summary = agent.enrich_posts([post])
    assert summary.images_processed == 0
    assert post.images[0].image_caption == "already done"


# ── EntityExtractor ──────────────────────────────────────────────────────────

def _mock_openai_response(content: str):
    """Build a mock OpenAI chat completion response."""
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def test_entity_extractor_basic():
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_openai_response(
        json.dumps([
            {"post_idx": 1, "name": "Joe Biden", "type": "PERSON"},
            {"post_idx": 1, "name": "World Health Organization", "type": "ORG"},
            {"post_idx": 2, "name": "Beijing", "type": "LOC"},
        ])
    )
    extractor = EntityExtractor(client=client, batch_size=30)
    posts = [
        Post(id="p1", account_id="u", text="Biden met with WHO."),
        Post(id="p2", account_id="u", text="Conference in Beijing."),
    ]
    total = extractor.extract_for_posts(posts)
    assert total == 3
    assert {(e.name, e.entity_type) for e in posts[0].entities} == {
        ("Joe Biden", "PERSON"),
        ("World Health Organization", "ORG"),
    }
    assert posts[1].entities[0].name == "Beijing"
    assert posts[1].entities[0].entity_type == "LOC"


def test_entity_extractor_dedupes_within_post():
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_openai_response(
        json.dumps([
            {"post_idx": 1, "name": "WHO", "type": "ORG"},
            {"post_idx": 1, "name": "who", "type": "ORG"},  # case-insensitive dup
        ])
    )
    extractor = EntityExtractor(client=client)
    posts = [Post(id="p1", account_id="u", text="WHO WHO.")]
    extractor.extract_for_posts(posts)
    assert len(posts[0].entities) == 1


def test_entity_extractor_handles_markdown_fenced_json():
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_openai_response(
        "```json\n[{\"post_idx\":1,\"name\":\"NASA\",\"type\":\"ORG\"}]\n```"
    )
    extractor = EntityExtractor(client=client)
    posts = [Post(id="p1", account_id="u", text="NASA launch.")]
    extractor.extract_for_posts(posts)
    assert posts[0].entities[0].name == "NASA"


def test_entity_extractor_skips_invalid_types():
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_openai_response(
        json.dumps([
            {"post_idx": 1, "name": "Foo", "type": "VEHICLE"},  # invalid -> OTHER
            {"post_idx": 1, "name": "Bar", "type": "PERSON"},
        ])
    )
    extractor = EntityExtractor(client=client)
    posts = [Post(id="p1", account_id="u", text="Foo Bar.")]
    extractor.extract_for_posts(posts)
    types = {e.entity_type for e in posts[0].entities}
    assert "OTHER" in types
    assert "PERSON" in types


def test_entity_extractor_api_error_returns_empty():
    client = MagicMock()
    client.chat.completions.create.side_effect = RuntimeError("api down")
    extractor = EntityExtractor(client=client)
    posts = [Post(id="p1", account_id="u", text="Whatever.")]
    total = extractor.extract_for_posts(posts)
    assert total == 0
    assert posts[0].entities == []


# ── OfficialIngestionPipeline ────────────────────────────────────────────────

def test_chunk_article_token_split(tmp_path: Path):
    cfg = PipelineConfig(
        sources=[],
        chunking=ChunkingConfig(target_tokens=10, overlap_tokens=2,
                                min_chunk_tokens=2),
        output_base_dir=tmp_path,
    )
    pipeline = OfficialIngestionPipeline(cfg=cfg, news_service=None)
    src = SourceConfig(
        name="test", domain="example.com", tier="reputable_media",
        feeds=[], poll_minutes=360, enabled=True,
    )
    article = {
        "url": "https://example.com/a",
        "title": "Title",
        "summary": "fallback",
        "author": "Alice",
        "published": datetime(2026, 1, 1),
        "full_text": " ".join([f"word{i}" for i in range(25)]),
    }
    chunks = pipeline._chunk_article(src, article)
    assert len(chunks) >= 2
    assert chunks[0].chunk_index == 0
    assert chunks[0].source == "test"
    assert chunks[0].domain == "example.com"
    assert chunks[0].token_count <= 10
    # IDs are deterministic
    assert chunks[0].chunk_id != chunks[1].chunk_id


def test_chunk_article_empty_returns_no_chunks(tmp_path: Path):
    cfg = PipelineConfig(output_base_dir=tmp_path)
    pipeline = OfficialIngestionPipeline(cfg=cfg, news_service=None)
    src = SourceConfig(
        name="test", domain="example.com", tier="reputable_media",
        feeds=[], poll_minutes=360, enabled=True,
    )
    chunks = pipeline._chunk_article(
        src, {"url": "u", "title": "", "summary": "", "full_text": ""},
    )
    assert chunks == []


def test_run_once_writes_jsonl(tmp_path: Path, monkeypatch):
    cfg = PipelineConfig(
        sources=[
            SourceConfig(name="bbc", domain="bbc.com", tier="reputable_media",
                         feeds=["https://example/bbc.rss"], enabled=True),
        ],
        chunking=ChunkingConfig(target_tokens=5, overlap_tokens=1,
                                min_chunk_tokens=2),
        output_base_dir=tmp_path,
    )
    pipeline = OfficialIngestionPipeline(cfg=cfg, news_service=None)

    # Mock _fetch_feed_items to skip the real RSS / HTTP path
    def _fake_fetch_feed_items(self, src):
        return [{
            "url": "https://bbc.com/news/test",
            "title": "Title",
            "summary": "summary",
            "author": "Author",
            "published": datetime(2026, 5, 1),
            "full_text": " ".join([f"w{i}" for i in range(15)]),
        }]
    monkeypatch.setattr(
        OfficialIngestionPipeline, "_fetch_feed_items",
        _fake_fetch_feed_items,
    )

    results = pipeline.run_once()
    assert results["bbc"] >= 1
    # Find the jsonl file
    jsonl_files = list(tmp_path.glob("*/bbc.jsonl"))
    assert len(jsonl_files) == 1
    lines = jsonl_files[0].read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) >= 1
    first = json.loads(lines[0])
    assert first["source"] == "bbc"
    assert first["domain"] == "bbc.com"
    assert first["tier"] == "reputable_media"
    assert first["chunk_index"] == 0


def test_run_once_source_filter(tmp_path: Path, monkeypatch):
    cfg = PipelineConfig(
        sources=[
            SourceConfig(name="bbc", domain="bbc.com", tier="reputable_media",
                         feeds=[], enabled=True),
            SourceConfig(name="nyt", domain="nytimes.com",
                         tier="reputable_media", feeds=[], enabled=True),
        ],
        output_base_dir=tmp_path,
    )
    pipeline = OfficialIngestionPipeline(cfg=cfg, news_service=None)
    monkeypatch.setattr(
        OfficialIngestionPipeline, "_fetch_feed_items",
        lambda self, src: [],
    )
    results = pipeline.run_once(source_filter="bbc")
    assert "bbc" in results
    assert "nyt" not in results


def test_list_sources_returns_only_enabled():
    cfg = PipelineConfig(
        sources=[
            SourceConfig(name="bbc", domain="bbc.com", tier="reputable_media",
                         feeds=[], enabled=True),
            SourceConfig(name="rt", domain="rt.com", tier="reputable_media",
                         feeds=[], enabled=False),
        ],
    )
    pipeline = OfficialIngestionPipeline(cfg=cfg, news_service=None)
    assert pipeline.list_sources() == ["bbc"]


# ── PipelineV2 wiring smoke test ─────────────────────────────────────────────

def test_persist_v2_writes_replied_edges_when_parent_post_id_present(
    tmp_path: Path,
):
    """KG Phase A: parent_post_id should produce Kuzu Replied edges."""
    from agents.precompute_pipeline_v2 import PrecomputePipelineV2

    # 3 posts: root + 2 children replying to root
    posts = [
        Post(id="root", account_id="alice", text="root post"),
        Post(id="c1", account_id="bob", text="reply 1",
             parent_post_id="root"),
        Post(id="c2", account_id="carol", text="reply 2",
             parent_post_id="root"),
    ]
    ingestion = MagicMock()
    ingestion.ingest_posts_from_jsonl.return_value = posts
    knowledge = MagicMock()

    kuzu = MagicMock()
    pg = MagicMock()

    pipeline = PrecomputePipelineV2(
        ingestion=ingestion,
        knowledge=knowledge,
        multimodal=MagicMock(enrich_posts=MagicMock()),
        entity_extractor=MagicMock(extract_for_posts=MagicMock()),
        topic_clusterer=MagicMock(cluster=MagicMock(return_value=[])),
        post_deduper=MagicMock(
            annotate=MagicMock(),
            find_duplicates=MagicMock(return_value=MagicMock(
                duplicate_post_ids=set(),
            )),
        ),
        schema_agent=None,
        schema_sync=None,
        pg=pg,
        kuzu=kuzu,
    )
    pipeline.run(run_dir=tmp_path / "kg-run", jsonl_path="any.jsonl")

    # Each child should have triggered an upsert_post(parent, "") + add_replied
    add_replied_calls = kuzu.add_replied.call_args_list
    edge_pairs = {(call.args[0], call.args[1])
                  for call in add_replied_calls}
    assert ("c1", "root") in edge_pairs
    assert ("c2", "root") in edge_pairs
    # Root has no parent_post_id; no spurious Replied edge should originate from it.
    assert all(child != "root" for child, _ in edge_pairs)


def test_pipeline_v2_runs_with_jsonl_fixture(tmp_path: Path, monkeypatch):
    """Ensure precompute_pipeline_v2 wires fetch + multimodal + entity correctly.

    Mocks ingestion + knowledge so we don't touch OpenAI / Postgres / Reddit.
    """
    from agents.precompute_pipeline_v2 import PrecomputePipelineV2

    sample_posts = [
        Post(id="p1", account_id="u1", text="hello world", like_count=5),
        Post(id="p2", account_id="u2", text="another post", reply_count=2),
    ]

    ingestion = MagicMock()
    ingestion.ingest_posts_from_jsonl.return_value = sample_posts

    knowledge = MagicMock()
    knowledge.classify_post_emotions = MagicMock()  # in-place; nothing to return

    multimodal = MagicMock()
    multimodal.enrich_posts = MagicMock()
    entity_extractor = MagicMock()
    entity_extractor.extract_for_posts = MagicMock()

    pipeline = PrecomputePipelineV2(
        ingestion=ingestion,
        knowledge=knowledge,
        multimodal=multimodal,
        entity_extractor=entity_extractor,
    )

    result = pipeline.run(
        run_dir=tmp_path / "run-1",
        jsonl_path="any.jsonl",
    )
    assert result.run_id == "run-1"
    assert len(result.posts) == 2
    stage_names = [s.name for s in result.stages]
    # Phase 2 added schema_propose + persist_v2 to the v2 pipeline. The
    # Phase 1 fixture leaves schema_agent / pg unset so those stages are
    # no-ops, but they still appear in the stage list.
    assert stage_names[:5] == [
        "fetch_posts", "ingest", "normalize",
        "emotion_baseline", "topic_cluster",
    ]
    # Stages that are wired in this fixture must be ok; the optional
    # schema_propose / persist_v2 stages run with `None` collaborators
    # and just record an "ok" no-op.
    assert all(s.status == "ok" for s in result.stages
               if s.name in {"fetch_posts", "ingest", "normalize",
                              "emotion_baseline", "topic_cluster"})
    multimodal.enrich_posts.assert_called_once()
    entity_extractor.extract_for_posts.assert_called_once()
    knowledge.classify_post_emotions.assert_called_once()
    manifest_path = tmp_path / "run-1" / "run_manifest_v2.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "v2"
    assert manifest["post_count"] == 2
