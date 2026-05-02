"""
Official Ingestion Pipeline — redesign-2026-05 Phase 1.5.

Standalone official-source ingestion arm. Flow:
    RSS feed -> fetch full article -> clean -> token-based chunking
                -> metadata tagging -> data/official_chunks/{date}/{source}.jsonl

Does NOT write Chroma. Phase 2 will replay these jsonl files into Chroma 1
(articles collection). The Phase 1 deliverable is purely about validating
the cleaning pipeline.

Dependencies:
- `services.news_search_service.NewsSearchService` for full-article fetch
- `feedparser` for RSS parsing (new dependency)
- Failures do not raise; they are logged and leave the run partial.

CLI:
    python -m agents.official_ingestion_pipeline --once
    python -m agents.official_ingestion_pipeline --once --source bbc
    python -m agents.official_ingestion_pipeline --list
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog

from config import BASE_DIR
from models.official_chunk import OfficialChunk

log = structlog.get_logger(__name__)


# ── Config loader ────────────────────────────────────────────────────────────

@dataclass
class SourceConfig:
    name: str
    domain: str
    tier: str
    feeds: list[str]
    poll_minutes: int = 360
    enabled: bool = True


@dataclass
class ChunkingConfig:
    target_tokens: int = 800
    overlap_tokens: int = 200
    min_chunk_tokens: int = 100


@dataclass
class PipelineConfig:
    sources: list[SourceConfig] = field(default_factory=list)
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    output_base_dir: Path = BASE_DIR / "data" / "official_chunks"


def load_config(yaml_path: Optional[Path] = None) -> PipelineConfig:
    """Lazy-load YAML; if PyYAML is missing, log and return an empty config."""
    yaml_path = yaml_path or (BASE_DIR / "config" / "official_sources.yaml")
    if not yaml_path.exists():
        log.warning("official.config_missing", path=str(yaml_path))
        return PipelineConfig()
    try:
        import yaml  # type: ignore
    except ImportError:
        log.error(
            "official.pyyaml_missing",
            hint="pip install pyyaml to enable official ingestion",
        )
        return PipelineConfig()

    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    sources = [
        SourceConfig(
            name=s["name"],
            domain=s["domain"],
            tier=s.get("tier", "reputable_media"),
            feeds=list(s.get("feeds", [])),
            poll_minutes=int(s.get("poll_minutes", 360)),
            enabled=bool(s.get("enabled", True)),
        )
        for s in (raw.get("sources") or [])
    ]
    chunking_raw = raw.get("chunking") or {}
    chunking = ChunkingConfig(
        target_tokens=int(chunking_raw.get("target_tokens", 800)),
        overlap_tokens=int(chunking_raw.get("overlap_tokens", 200)),
        min_chunk_tokens=int(chunking_raw.get("min_chunk_tokens", 100)),
    )
    output_raw = (raw.get("output") or {}).get("base_dir")
    base_dir = Path(output_raw) if output_raw else BASE_DIR / "data" / "official_chunks"
    if not base_dir.is_absolute():
        base_dir = BASE_DIR / base_dir
    return PipelineConfig(
        sources=sources, chunking=chunking, output_base_dir=base_dir,
    )


# ── Pipeline ─────────────────────────────────────────────────────────────────

class OfficialIngestionPipeline:
    """Each `run_once` pulls all enabled sources into a date-partitioned dir."""

    def __init__(
        self,
        cfg: Optional[PipelineConfig] = None,
        news_service=None,  # services.news_search_service.NewsSearchService
        embeddings=None,    # services.embeddings_service.EmbeddingsService
        chroma=None,        # services.chroma_collections.ChromaCollections
        write_chroma: bool = True,
    ) -> None:
        self._cfg = cfg or load_config()
        self._news = news_service  # lazy import to dodge circular deps
        if self._news is None:
            try:
                from services.news_search_service import NewsSearchService
                self._news = NewsSearchService()
            except Exception as exc:
                log.warning("official.news_service_unavailable", error=str(exc))
        self._write_chroma = write_chroma
        self._embeddings = embeddings
        self._chroma = chroma
        if self._write_chroma:
            try:
                if self._embeddings is None:
                    from services.embeddings_service import EmbeddingsService
                    self._embeddings = EmbeddingsService()
                if self._chroma is None:
                    from services.chroma_collections import ChromaCollections
                    self._chroma = ChromaCollections()
            except Exception as exc:
                log.warning("official.chroma_unavailable",
                            error=str(exc)[:160])
                self._write_chroma = False

    # ── Public ─────────────────────────────────────────────────────────────────

    def run_once(self, source_filter: Optional[str] = None) -> dict[str, int]:
        """Pull all enabled sources once. Returns {source: chunks_written}."""
        out_root = self._cfg.output_base_dir
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out_dir = out_root / date_str
        out_dir.mkdir(parents=True, exist_ok=True)

        results: dict[str, int] = {}
        for src in self._cfg.sources:
            if not src.enabled:
                continue
            if source_filter and src.name != source_filter:
                continue
            chunks = self._process_source(src)
            if not chunks:
                results[src.name] = 0
                continue
            out_path = out_dir / f"{src.name}.jsonl"
            self._write_jsonl(out_path, chunks)
            embedded = self._upsert_to_chroma(chunks)
            results[src.name] = len(chunks)
            log.info("official.source_done",
                     source=src.name, chunks=len(chunks),
                     embedded=embedded,
                     output=str(out_path))

        log.info("official.run_done", results=results)
        return results

    def list_sources(self) -> list[str]:
        return [s.name for s in self._cfg.sources if s.enabled]

    # ── Internal ───────────────────────────────────────────────────────────────

    def _process_source(self, src: SourceConfig) -> list[OfficialChunk]:
        """Fetch RSS items -> fetch full text -> chunk -> return."""
        articles = self._fetch_feed_items(src)
        if not articles:
            log.warning("official.no_articles", source=src.name)
            return []

        chunks: list[OfficialChunk] = []
        for art in articles:
            try:
                chunks.extend(self._chunk_article(src, art))
            except Exception as exc:
                log.error("official.chunk_error",
                          source=src.name, url=art.get("url", ""),
                          error=str(exc)[:120])
        return chunks

    def _fetch_feed_items(self, src: SourceConfig) -> list[dict]:
        """Parse RSS feeds and fetch full article body via NewsSearchService.

        Phase 1: best-effort. When feedparser / NewsSearchService are missing,
        fall back to an empty list. Phase 2 will harden error handling and
        add incremental etag caching.
        """
        try:
            import feedparser  # type: ignore
        except ImportError:
            log.error(
                "official.feedparser_missing",
                hint="pip install feedparser to enable official RSS ingestion",
            )
            return []

        items: list[dict] = []
        for feed_url in src.feeds:
            via_google_news = "news.google.com" in feed_url
            try:
                parsed = feedparser.parse(feed_url)
            except Exception as exc:
                log.error("official.feed_parse_error",
                          source=src.name, feed=feed_url, error=str(exc)[:120])
                continue
            for entry in (parsed.entries or [])[:20]:
                title = getattr(entry, "title", "") or ""
                link = getattr(entry, "link", "") or ""
                summary = getattr(entry, "summary", "") or ""
                # `original_link` keeps the Google News redirect (unique
                # per entry) so chunk_id stays unique. `link` becomes the
                # publication's canonical URL for user-facing citations.
                original_link = link
                if via_google_news:
                    # Google News titles end with " - Outlet"; strip the suffix.
                    suffix = f" - {src.name.upper()}"
                    if title.lower().endswith(suffix.lower()):
                        title = title[: -len(suffix)].strip()
                    src_meta = getattr(entry, "source", None)
                    if isinstance(src_meta, dict) and src_meta.get("href"):
                        link = src_meta["href"]
                    # Summary is just a redirect anchor; drop it.
                    summary = ""
                items.append({
                    "url": link,                        # citation URL
                    "fingerprint_url": original_link,    # used for chunk_id
                    "title": title,
                    "summary": summary,
                    "author": getattr(entry, "author", "") or "",
                    "published": _parse_published(entry),
                    "_headline_only": via_google_news,
                })

        # Best-effort full-text fetch; fall back to summary on failure.
        # NewsSearchService exposes only `_fetch_article` (private by convention,
        # but currently the only full-text fetcher; Phase 2 may promote it to
        # public).
        if self._news is not None:
            fetcher = getattr(self._news, "_fetch_article", None)
            if callable(fetcher):
                for it in items:
                    if not it["url"] or it.get("_headline_only"):
                        continue
                    try:
                        full = fetcher(it["url"])
                        if full:
                            it["full_text"] = full
                    except Exception as exc:
                        log.warning("official.fetch_body_error",
                                    url=it["url"], error=str(exc)[:120])
        return items

    def _chunk_article(
        self, src: SourceConfig, article: dict,
    ) -> list[OfficialChunk]:
        """Token-based chunking with overlap."""
        text = (article.get("full_text") or article.get("summary")
                or article.get("title") or "")
        text = text.strip()
        if not text:
            return []

        tokens = text.split()  # naive tokenizer; Phase 2 swaps to tiktoken
        target = self._cfg.chunking.target_tokens
        overlap = self._cfg.chunking.overlap_tokens
        min_tokens = self._cfg.chunking.min_chunk_tokens

        chunks: list[OfficialChunk] = []
        i = 0
        idx = 0
        while i < len(tokens):
            window = tokens[i:i + target]
            if len(window) < min_tokens and chunks:
                # last residual; fold into prior chunk
                break
            chunk_text = " ".join(window)
            fp_url = article.get("fingerprint_url") or article.get("url", "")
            chunk_id = hashlib.sha256(
                f"{fp_url}#{idx}".encode("utf-8")
            ).hexdigest()
            chunks.append(OfficialChunk(
                chunk_id=chunk_id,
                source=src.name,
                domain=src.domain,
                tier=src.tier,  # type: ignore[arg-type]
                url=article.get("url", ""),
                title=article.get("title", ""),
                author=article.get("author", ""),
                publish_date=article.get("published"),
                chunk_index=idx,
                text=chunk_text,
                token_count=len(window),
            ))
            idx += 1
            if i + target >= len(tokens):
                break
            i += max(1, target - overlap)
        return chunks

    @staticmethod
    def _write_jsonl(path: Path, chunks: list[OfficialChunk]) -> None:
        with path.open("w", encoding="utf-8") as f:
            for c in chunks:
                f.write(c.model_dump_json() + "\n")

    # ── Chroma 1 upsert ──────────────────────────────────────────────────────

    def _upsert_to_chroma(self, chunks: list[OfficialChunk]) -> int:
        """Embed and upsert OfficialChunks into the chroma_official collection."""
        if not self._write_chroma or not chunks or self._chroma is None:
            return 0
        try:
            texts = [c.text or c.title or "" for c in chunks]
            embeddings = self._embeddings.embed_batch(texts)
            ids = [c.chunk_id for c in chunks]
            metadatas = []
            for c in chunks:
                metadatas.append({
                    "source": c.source,
                    "domain": c.domain,
                    "tier": c.tier,
                    "url": c.url,
                    "title": c.title,
                    "author": c.author,
                    "publish_date": (c.publish_date.isoformat()
                                      if c.publish_date else ""),
                    "chunk_index": c.chunk_index,
                    "topic_hint": c.topic_hint or "",
                })
            self._chroma.official.upsert(
                ids=ids, embeddings=embeddings, documents=texts,
                metadatas=metadatas,
            )
            for c in chunks:
                c.embedded = True
            return len(chunks)
        except Exception as exc:
            log.error("official.chroma_upsert_error",
                      count=len(chunks), error=str(exc)[:160])
            return 0

    def replay_jsonl_to_chroma(
        self, date_str: Optional[str] = None,
    ) -> dict[str, int]:
        """Bulk-load previously-written jsonl files into Chroma 1.

        Use this after an offline crawl, or to back-fill Chroma 1 when the
        pipeline ran with `write_chroma=False`.
        """
        if not self._write_chroma or self._chroma is None:
            log.warning("official.replay_skipped_no_chroma")
            return {}
        out_root = self._cfg.output_base_dir
        targets: list[Path]
        if date_str:
            targets = sorted((out_root / date_str).glob("*.jsonl"))
        else:
            # Walk every date partition.
            targets = sorted(out_root.glob("*/*.jsonl"))
        results: dict[str, int] = {}
        for path in targets:
            chunks: list[OfficialChunk] = []
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    chunks.append(OfficialChunk.model_validate_json(line))
                except Exception as exc:
                    log.warning("official.replay_skip_line",
                                path=str(path), error=str(exc)[:120])
            embedded = self._upsert_to_chroma(chunks)
            results[str(path)] = embedded
            log.info("official.replay_file_done",
                     path=str(path), embedded=embedded)
        return results


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_published(entry) -> Optional[datetime]:
    """Best-effort RFC2822 / ISO date parser. Returns None on failure."""
    raw = getattr(entry, "published", "") or getattr(entry, "updated", "")
    if not raw:
        return None
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(raw)
    except Exception:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return None


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Official Ingestion Pipeline (redesign-2026-05 Phase 1.5)",
    )
    parser.add_argument("--once", action="store_true",
                        help="Pull each enabled source once, write jsonl, "
                             "and upsert into Chroma 1.")
    parser.add_argument("--source", default=None,
                        help="Limit to a single source name (e.g. bbc).")
    parser.add_argument("--list", action="store_true",
                        help="List enabled sources and exit.")
    parser.add_argument("--replay", action="store_true",
                        help="Replay existing jsonl files into Chroma 1 "
                             "(no network fetch).")
    parser.add_argument("--date", default=None,
                        help="With --replay: limit to a single date "
                             "partition (e.g. 2026-05-02).")
    parser.add_argument("--no-chroma", action="store_true",
                        help="Skip Chroma 1 upsert (jsonl-only).")
    args = parser.parse_args()

    pipeline = OfficialIngestionPipeline(write_chroma=not args.no_chroma)

    if args.list:
        for name in pipeline.list_sources():
            print(name)
        return 0

    if args.replay:
        results = pipeline.replay_jsonl_to_chroma(date_str=args.date)
        total = sum(results.values())
        for path, n in results.items():
            print(f"{path}: {n} embedded")
        print(f"TOTAL: {total} chunks embedded into Chroma 1")
        return 0 if total > 0 else 1

    if args.once:
        results = pipeline.run_once(source_filter=args.source)
        for k, v in results.items():
            print(f"{k}: {v} chunks")
        return 0 if any(v > 0 for v in results.values()) else 1

    parser.error("Specify --once, --replay, or --list")
    return 2


if __name__ == "__main__":
    sys.exit(main())
