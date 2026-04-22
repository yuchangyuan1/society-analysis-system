"""
WikipediaService — lightweight Wikipedia REST client for evidence fallback.

Used by KnowledgeAgent.build_evidence_pack when no Chroma/internal articles
are found for a claim (P0-3, Tier A). Always best-effort — failures return
None and log a warning rather than raising.

Network: Wikipedia REST API
  - opensearch to get a candidate page title
  - page/summary for the extract
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Optional

import requests
import structlog

log = structlog.get_logger(__name__)

_USER_AGENT = (
    "society-analysis-project/1.0 "
    "(research prototype; contact: yuchangyuan106@gmail.com)"
)
_TIMEOUT = 6.0

# Wikipedia's opensearch API expects title-like queries. Full sentences return
# nothing, so when a caller passes a claim ("AIPAC runs America.") we fall back
# to capitalised proper-noun candidates extracted from the text. Single-token
# candidates that are actually common sentence-initial words ("The", "One",
# "Is", etc.) are filtered out so we don't land on meaningless pages.
_PROPER_NOUN_RE = re.compile(r"\b[A-Z][A-Za-z0-9-]*(?:\s+[A-Z][A-Za-z0-9-]*)*\b")
_SINGLE_WORD_STOPS = frozenset({
    "The", "A", "An", "One", "This", "That", "These", "Those",
    "He", "She", "It", "They", "We", "You", "His", "Her", "Their",
    "When", "Where", "Why", "How", "What", "Who", "Which",
    "After", "Before", "During", "Since", "Until",
    "New", "Old", "Big", "Small", "Few", "Many", "Some", "All",
    "First", "Second", "Last", "Next", "Other", "No", "None",
    "Is", "Are", "Was", "Were", "Has", "Have", "Had",
    "Do", "Does", "Did", "Be", "Been", "Being",
    "Trillion", "Million", "Billion", "Thousand", "Hundred",
})
_MAX_EXTRACTED_CANDIDATES = 3


class WikipediaService:
    def __init__(self, language: str = "en", timeout: float = _TIMEOUT) -> None:
        self._base = f"https://{language}.wikipedia.org"
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _USER_AGENT})

    def fetch_summary(self, query: str) -> Optional[dict]:
        """
        Try to find a Wikipedia article matching the claim text and return:
          {
            "article_id": wiki_title,
            "title": str,
            "url": str,
            "snippet": str (short extract),
            "source_name": "Wikipedia",
          }
        Returns None if nothing plausible is found or on network error.

        Callers often pass a full claim sentence; opensearch only matches
        title-like inputs, so we try the raw query first and then fall back
        to capitalised proper-noun candidates extracted from the text.
        """
        for candidate in self._candidate_queries(query):
            title = self._resolve_title(candidate)
            if not title:
                continue
            summary = self._fetch_summary_for_title(title)
            if summary is not None:
                if candidate != query:
                    log.info(
                        "wikipedia.query_rewritten",
                        original=query[:80],
                        matched=candidate,
                        title=summary["title"],
                    )
                return summary
        return None

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def count_proper_nouns(text: str) -> int:
        """
        Count distinct capitalised proper-noun phrases in the text, excluding
        common sentence-initial words. Used by the actionability classifier
        (§10.3) to decide whether a claim is context-sparse.
        """
        found: set[str] = set()
        for match in _PROPER_NOUN_RE.findall(text):
            if " " in match:
                found.add(match)
            elif len(match) >= 3 and match not in _SINGLE_WORD_STOPS:
                found.add(match)
        return len(found)

    @staticmethod
    def _candidate_queries(query: str) -> list[str]:
        """
        Build an ordered list of opensearch candidates for this query:
        the raw input first (cheap no-op for short/entity-like inputs), then
        proper-noun phrases extracted from the text. Multi-word capitalised
        sequences (e.g. "Tyler Oliveira") are kept as-is; single capitalised
        tokens are kept only if they aren't common sentence-initial words.
        """
        candidates: list[str] = []
        seen: set[str] = set()

        def add(c: str) -> None:
            c = c.strip()
            if c and c not in seen:
                seen.add(c)
                candidates.append(c)

        add(query)
        for match in _PROPER_NOUN_RE.findall(query):
            if " " in match:
                add(match)
            elif len(match) >= 3 and match not in _SINGLE_WORD_STOPS:
                add(match)
            if len(candidates) - 1 >= _MAX_EXTRACTED_CANDIDATES:
                break
        return candidates

    def _resolve_title(self, query: str) -> Optional[str]:
        """Use opensearch to pick the best-matching page title."""
        url = f"{self._base}/w/api.php"
        params = {
            "action": "opensearch",
            "search": query[:200],
            "limit": 1,
            "namespace": 0,
            "format": "json",
        }
        try:
            r = self._session.get(url, params=params, timeout=self._timeout)
            r.raise_for_status()
            data = r.json()
            titles = data[1] if len(data) > 1 else []
            return titles[0] if titles else None
        except Exception as exc:
            log.warning("wikipedia.opensearch_error", query=query[:60], error=str(exc))
            return None

    def _fetch_summary_for_title(self, title: str) -> Optional[dict]:
        encoded = urllib.parse.quote(title.replace(" ", "_"), safe="")
        url = f"{self._base}/api/rest_v1/page/summary/{encoded}"
        try:
            r = self._session.get(url, timeout=self._timeout)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            data = r.json()
            # Skip disambiguation pages
            if data.get("type") == "disambiguation":
                return None
            extract = data.get("extract", "").strip()
            if not extract:
                return None
            return {
                "article_id": f"wiki:{title}",
                "title": data.get("title", title),
                "url": data.get("content_urls", {}).get("desktop", {}).get("page")
                       or f"{self._base}/wiki/{encoded}",
                "snippet": extract[:500],
                "source_name": "Wikipedia",
            }
        except Exception as exc:
            log.warning("wikipedia.summary_error", title=title, error=str(exc))
            return None
