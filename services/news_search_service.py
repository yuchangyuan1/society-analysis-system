"""
News Search Service — fetch authoritative evidence for trending topics.

Flow:
  1. DuckDuckGo text search with site: operators targeting trusted domains
  2. Fallback: DuckDuckGo news search, filter to trusted-domain allowlist
  3. Fetch full article body via httpx + BeautifulSoup
  4. Return structured article dicts ready for RAG ingestion

Trusted domains are deliberately conservative: we only accept well-known
fact-checking outlets and major international news agencies so the RAG
evidence base stays high-quality.
"""
from __future__ import annotations

import hashlib
import time
from typing import Optional

import httpx
import structlog

log = structlog.get_logger(__name__)

# ── Trusted source allowlist ──────────────────────────────────────────────────
# Only articles from these domains are ingested as authoritative evidence.
TRUSTED_DOMAINS: set[str] = {
    # Wire services / major international news
    "reuters.com",
    "apnews.com",
    "bbc.com",
    "bbc.co.uk",
    "theguardian.com",
    "npr.org",
    "aljazeera.com",
    "nbcnews.com",
    "cbsnews.com",
    "abcnews.go.com",
    "pbs.org",
    "usatoday.com",
    # Fact-check specialists
    "snopes.com",
    "factcheck.org",
    "politifact.com",
    "fullfact.org",
    "africacheck.org",
    "theconversation.com",
    # Health & science authorities
    "who.int",
    "cdc.gov",
    "nih.gov",
    "pubmed.ncbi.nlm.nih.gov",
    "nature.com",
    "sciencemag.org",
    "thelancet.com",
    "nejm.org",
    "sciencedirect.com",
    # Reference
    "en.wikipedia.org",
}

# Space-separated list used in site: search operators
_SITE_FILTER = " OR ".join(f"site:{d}" for d in sorted(TRUSTED_DOMAINS))

_REQUEST_TIMEOUT = 12  # seconds
_FETCH_DELAY = 1.0     # seconds between article fetches (polite crawling)
_DDG_DELAY  = 2.0      # seconds between DDG API calls
_MAX_BODY_CHARS = 8000 # truncate very long articles
_MAX_RETRIES = 3


class NewsSearchService:
    """
    Search authoritative news and fact-check sites for evidence on a topic.
    Uses DuckDuckGo (text + news) with site-filter targeting trusted domains.
    """

    def __init__(self) -> None:
        self._http = httpx.Client(
            timeout=_REQUEST_TIMEOUT,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            },
        )

    # ── Public interface ───────────────────────────────────────────────────────

    def search_and_fetch(
        self,
        query: str,
        max_results: int = 5,
    ) -> list[dict]:
        """
        Search for `query` on authoritative domains, fetch article text.

        Returns list of:
          {
            article_id: str,   # md5(url)[:16]
            title:      str,
            url:        str,
            body:       str,   # full text (or snippet if fetch failed)
            source:     str,
            published:  str,
          }
        """
        # Strategy 1: targeted text search on trusted sites
        raw = self._ddg_text_trusted(query, max_results=max_results * 2)

        # Strategy 2: general news search filtered post-hoc
        if len(raw) < max_results:
            news_raw = self._ddg_news(query, max_results=max_results * 3)
            trusted_news = [
                {"title": r.get("title",""), "url": r.get("url",""),
                 "body": r.get("body",""), "source": r.get("source",""),
                 "published": r.get("date","")}
                for r in news_raw
                if self._is_trusted(r.get("url",""))
            ]
            raw.extend(trusted_news)

        articles: list[dict] = []
        seen_ids: set[str] = set()

        for item in raw:
            url = item.get("url", "") or item.get("href", "")
            if not url:
                continue
            article_id = self._url_to_id(url)
            if article_id in seen_ids:
                continue
            seen_ids.add(article_id)

            body = self._fetch_article(url) or item.get("body", "")
            if not body.strip():
                continue

            articles.append({
                "article_id": article_id,
                "title": item.get("title", ""),
                "url": url,
                "body": body[:_MAX_BODY_CHARS],
                "source": item.get("source", ""),
                "published": item.get("published", item.get("date", "")),
            })
            _safe = lambda s: s.encode("ascii", "replace").decode("ascii")
            log.info(
                "news_search.fetched",
                title=_safe(item.get("title", "")[:60]),
                url=_safe(url[:70]),
                chars=len(body),
            )
            if len(articles) >= max_results:
                break
            time.sleep(_FETCH_DELAY)

        log.info("news_search.done", query=query[:60], found=len(articles))
        return articles

    def close(self) -> None:
        self._http.close()

    # ── Internal ───────────────────────────────────────────────────────────────

    def _ddg_text_trusted(self, query: str, max_results: int = 10) -> list[dict]:
        """
        DuckDuckGo text search restricted to trusted domains via site: operators.
        Returns list of {title, href/url, body} dicts.
        """
        # Build a site-filtered query: keep it short to avoid DDG rejecting it
        # Use the top 10 most useful domains for brevity
        top_sites = (
            "site:reuters.com OR site:apnews.com OR site:bbc.com "
            "OR site:snopes.com OR site:factcheck.org OR site:politifact.com "
            "OR site:who.int OR site:theguardian.com OR site:fullfact.org "
            "OR site:theconversation.com"
        )
        site_query = f"{query} ({top_sites})"
        return self._ddg_text(site_query, max_results)

    def _ddg_text(self, query: str, max_results: int = 10) -> list[dict]:
        """Generic DuckDuckGo text search with retry on rate limit."""
        for attempt in range(_MAX_RETRIES):
            try:
                from ddgs import DDGS
                with DDGS() as ddgs:
                    results = list(ddgs.text(query, max_results=max_results))
                # Normalise field names
                return [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("href", r.get("url", "")),
                        "body": r.get("body", ""),
                        "source": "",
                        "published": "",
                    }
                    for r in results
                ]
            except Exception as exc:
                err = str(exc)
                if "Ratelimit" in err or "429" in err or "403" in err:
                    wait = _DDG_DELAY * (2 ** attempt)
                    log.warning("news_search.ratelimit", attempt=attempt+1,
                                wait=wait, query=query[:40])
                    time.sleep(wait)
                else:
                    log.warning("news_search.text_error", error=err[:100])
                    return []
        return []

    def _ddg_news(self, query: str, max_results: int = 15) -> list[dict]:
        """DuckDuckGo news search with retry on rate limit."""
        for attempt in range(_MAX_RETRIES):
            try:
                from ddgs import DDGS
                with DDGS() as ddgs:
                    results = list(ddgs.news(query, max_results=max_results))
                return results
            except Exception as exc:
                err = str(exc)
                if "Ratelimit" in err or "429" in err or "403" in err:
                    wait = _DDG_DELAY * (2 ** attempt)
                    log.warning("news_search.news_ratelimit", attempt=attempt+1,
                                wait=wait, query=query[:40])
                    time.sleep(wait)
                else:
                    log.warning("news_search.news_error", error=err[:100])
                    return []
        return []

    def _is_trusted(self, url: str) -> bool:
        """Return True if the URL's domain is in the trusted allowlist."""
        try:
            from urllib.parse import urlparse
            host = urlparse(url).hostname or ""
            for domain in TRUSTED_DOMAINS:
                if host == domain or host.endswith("." + domain):
                    return True
        except Exception:
            pass
        return False

    def _fetch_article(self, url: str) -> Optional[str]:
        """
        Fetch the article at `url` and extract readable text.
        Returns None on any failure (network error, paywall, etc.).
        """
        try:
            resp = self._http.get(url)
            if resp.status_code != 200:
                return None
            return self._extract_text(resp.text)
        except Exception as exc:
            log.debug("news_search.fetch_failed", url=url[:80], error=str(exc)[:80])
            return None

    @staticmethod
    def _extract_text(html: str) -> str:
        """
        Extract main article text from HTML using BeautifulSoup.
        Removes navigation, ads, scripts, and other boilerplate.
        """
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")

        # Remove boilerplate tags
        for tag in soup(["script", "style", "nav", "header", "footer",
                          "aside", "form", "noscript", "iframe", "figure"]):
            tag.decompose()

        # Try to find the main article body
        article = (
            soup.find("article")
            or soup.find(attrs={"class": lambda c: c and "article" in " ".join(c).lower()})
            or soup.find("main")
            or soup.body
        )

        if article is None:
            return ""

        lines = [line.strip() for line in article.get_text(separator="\n").splitlines()]
        text = "\n".join(line for line in lines if line)
        return text

    @staticmethod
    def _url_to_id(url: str) -> str:
        return hashlib.md5(url.encode()).hexdigest()[:16]
