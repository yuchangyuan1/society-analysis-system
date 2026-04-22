"""
CLI entry point for OpenClaw skills to invoke Python services.

Usage (from project root):
    python -m services.cli <command> [args]

Commands:
    claim-extract   --text "<text>" [--post-id <id>]
    claim-dedup     --claim "<text>"
    evidence-pack   --claim-id <id>
    generate-card   --text "<counter_message>" --id <report_id> [--bg-prompt "<prompt>"] [--claim-summary "<summary>"]
    ingest-jsonl    --path <jsonl_path>
    seed-knowledge
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running as `python -m services.cli` from project root
sys.path.insert(0, str(Path(__file__).parent.parent))


def _build_services():
    """Lazy import and instantiate shared services (expensive — only when needed)."""
    from services.chroma_service import ChromaService
    from services.embeddings_service import EmbeddingsService
    from services.kuzu_service import KuzuService
    from services.postgres_service import PostgresService

    pg = PostgresService()
    try:
        pg.connect()
    except Exception:
        pass  # Non-fatal; services degrade gracefully
    chroma = ChromaService()
    kuzu = KuzuService()
    embedder = EmbeddingsService()
    return pg, chroma, kuzu, embedder


def cmd_claim_extract(args) -> None:
    pg, chroma, kuzu, embedder = _build_services()
    from agents.knowledge import KnowledgeAgent

    agent = KnowledgeAgent(pg=pg, chroma=chroma, kuzu=kuzu, embedder=embedder)
    claims = agent.extract_and_deduplicate_claims(args.text, post_id=args.post_id)
    output = [
        {
            "id": c.id,
            "normalized_text": c.normalized_text,
            "propagation_count": c.propagation_count,
        }
        for c in claims
    ]
    print(json.dumps(output, indent=2))


def cmd_claim_dedup(args) -> None:
    pg, chroma, kuzu, embedder = _build_services()
    from agents.knowledge import KnowledgeAgent

    agent = KnowledgeAgent(pg=pg, chroma=chroma, kuzu=kuzu, embedder=embedder)
    embed = embedder.embed(args.claim)
    candidates = chroma.query_claims(embed, n_results=3)
    if not candidates:
        print(json.dumps({"result": "DIFFERENT"}))
        return
    top = candidates[0]
    sim = chroma.__class__.cosine_similarity(top["distance"])
    from config import CLAIM_EMBED_SIM_HIGH

    if sim >= CLAIM_EMBED_SIM_HIGH:
        verdict = agent._llm_dedup(args.claim, top["document"])
        print(json.dumps({"result": verdict, "matched_id": top["id"], "similarity": sim}))
    else:
        print(json.dumps({"result": "DIFFERENT", "similarity": sim}))


def cmd_evidence_pack(args) -> None:
    pg, chroma, kuzu, embedder = _build_services()
    from agents.knowledge import KnowledgeAgent
    from models.claim import Claim

    agent = KnowledgeAgent(pg=pg, chroma=chroma, kuzu=kuzu, embedder=embedder)
    claim = Claim(id=args.claim_id, normalized_text=args.claim_id)
    claim = agent.build_evidence_pack(claim)
    output = {
        "claim_id": claim.id,
        "supporting": len(claim.supporting_evidence),
        "contradicting": len(claim.contradicting_evidence),
        "uncertain": len(claim.uncertain_evidence),
    }
    print(json.dumps(output, indent=2))


def cmd_generate_card(args) -> None:
    from services.stable_diffusion_service import StableDiffusionService

    sd = StableDiffusionService()
    path = sd.generate_card(
        counter_text=args.text,
        background_prompt=args.bg_prompt or "",
        claim_summary=args.claim_summary or "",
        report_id=args.id,
    )
    print(json.dumps({"visual_card_path": path, "status": "ok" if path else "unavailable"}))


def cmd_ingest_jsonl(args) -> None:
    pg, chroma, kuzu, embedder = _build_services()
    from services.claude_vision_service import ClaudeVisionService
    from services.x_api_service import XApiService
    from agents.ingestion import IngestionAgent

    vision = ClaudeVisionService()
    x_api = XApiService()
    agent = IngestionAgent(pg=pg, kuzu=kuzu, vision=vision, x_api=x_api)
    posts = agent.ingest_posts_from_jsonl(args.path)
    print(json.dumps({"count": len(posts), "source": "jsonl"}))


def cmd_seed_knowledge(_args) -> None:
    from scripts.seed_knowledge import seed

    count = seed()
    print(json.dumps({"seeded": count}))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Society service CLI — called by OpenClaw skills"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # claim-extract
    p = sub.add_parser("claim-extract", help="Extract and deduplicate claims from text")
    p.add_argument("--text", required=True)
    p.add_argument("--post-id", default=None)

    # claim-dedup
    p = sub.add_parser("claim-dedup", help="Check if a claim exists in the knowledge base")
    p.add_argument("--claim", required=True)

    # evidence-pack
    p = sub.add_parser("evidence-pack", help="Build evidence pack for a known claim")
    p.add_argument("--claim-id", required=True)

    # generate-card
    p = sub.add_parser("generate-card", help="Generate a visual clarification card")
    p.add_argument("--text", required=True, help="Counter-message text for the card")
    p.add_argument("--id", required=True, help="Report ID (used in output filename)")
    p.add_argument("--bg-prompt", default="", help="SD background image prompt")
    p.add_argument("--claim-summary", default="", help="Original claim summary for overlay")

    # ingest-jsonl
    p = sub.add_parser("ingest-jsonl", help="Ingest posts from a JSONL file")
    p.add_argument("--path", required=True, help="Path to JSONL file")

    # seed-knowledge
    sub.add_parser("seed-knowledge", help="Seed Chroma and Kuzu with fact-check articles")

    args = parser.parse_args()

    dispatch = {
        "claim-extract": cmd_claim_extract,
        "claim-dedup": cmd_claim_dedup,
        "evidence-pack": cmd_evidence_pack,
        "generate-card": cmd_generate_card,
        "ingest-jsonl": cmd_ingest_jsonl,
        "seed-knowledge": cmd_seed_knowledge,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
