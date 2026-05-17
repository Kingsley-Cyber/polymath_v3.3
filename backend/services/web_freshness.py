"""Opt-in live web lane for chat.

This module is deliberately additive: it never changes corpus retrieval,
ranking, graph expansion, or synthesis selection. Chat only calls it when the
user explicitly enables the Web toggle for that turn.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha1
from typing import Any
from urllib.parse import urlparse

import httpx

from config import get_settings
from models.schemas import SourceChunk

logger = logging.getLogger(__name__)

_DEFAULT_MAX_RESULTS = 2
_DEFAULT_CANDIDATE_RESULTS = 14
_DEFAULT_OBSCURA_MAX_CHARS = 4000
_DEFAULT_MAX_RELATED_TERMS = 5

_EXPLICIT_WEB_MARKERS = (
    "latest",
    "current",
    "up to date",
    "up-to-date",
    "recent",
    "today",
    "this year",
    "new version",
    "release notes",
    "changelog",
    "deprecated",
    "breaking change",
    "compatibility",
    "security advisory",
    "cve",
)

_FAST_MOVING_TECH_MARKERS = (
    "machine learning",
    "deep learning",
    "llm",
    "rag",
    "embedding",
    "openai",
    "anthropic",
    "gemini",
    "mistral",
    "pytorch",
    "tensorflow",
    "jax",
    "cuda",
    "vllm",
    "llama.cpp",
    "langchain",
    "transformers.js",
    "hugging face",
    "c++",
    "cpp",
    "c++20",
    "c++23",
    "c++26",
    "clang",
    "gcc",
    "cmake",
    "llvm",
    "swift",
    "swiftui",
    "xcode",
    "ios",
    "macos",
    "visionos",
)

_TECH_DOC_SUFFIXES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("swift", "swiftui", "xcode", "ios", "macos", "visionos"), "Swift Apple documentation current"),
    (("c++", "cpp", "clang", "gcc", "cmake", "llvm"), "cppreference current documentation"),
    (("machine learning", "deep learning", "llm", "rag", "pytorch", "tensorflow", "jax", "cuda", "vllm", "llama.cpp", "hugging face"), "latest documentation"),
)

_RELATED_TERM_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "and",
        "are",
        "as",
        "be",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "use",
        "used",
        "what",
        "when",
        "where",
        "which",
        "with",
        "concept",
        "concepts",
        "data",
        "document",
        "documents",
        "entity",
        "entities",
        "file",
        "files",
        "note",
        "notes",
        "source",
        "sources",
        "topic",
        "topics",
    }
)

_RELATED_TERM_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9.+#-]*")


@dataclass(frozen=True)
class WebSearchHit:
    title: str
    url: str
    snippet: str
    score: float
    engines: tuple[str, ...] = ()
    published_date: str | None = None


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _contains_marker(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _strip_html(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _normalize_related_term(value: str) -> str:
    value = _strip_html(str(value or ""))
    value = value.strip(" \t\r\n\"'`.,;:()[]{}")
    value = re.sub(r"\s+", " ", value)
    return value[:80].strip()


def _is_useful_related_term(term: str) -> bool:
    text = term.lower()
    if len(text) < 3 or len(text) > 80:
        return False
    if text in _RELATED_TERM_STOPWORDS:
        return False
    if text.startswith(("http://", "https://")):
        return False
    return any(ch.isalpha() for ch in text)


def _term_tokens(value: str) -> set[str]:
    return {
        token.strip("-")
        for token in _RELATED_TERM_TOKEN_RE.findall(value.lower())
        if token.strip("-") and token.strip("-") not in _RELATED_TERM_STOPWORDS
    }


def _is_query_aligned_related_term(query_tokens: set[str], term: str) -> bool:
    if not query_tokens:
        return False
    term_tokens = _term_tokens(term)
    if not term_tokens:
        return False
    if query_tokens & term_tokens:
        return True

    # Allow compact variants such as "llama.cpp" vs "llama cpp", but avoid
    # letting unrelated graph hubs rewrite a live-web query.
    compact_query = {token.replace(".", "").replace("-", "") for token in query_tokens}
    compact_term = {token.replace(".", "").replace("-", "") for token in term_tokens}
    return bool(compact_query & compact_term)


def select_related_search_terms(
    query: str,
    related_terms: list[str] | None,
    *,
    max_terms: int = _DEFAULT_MAX_RELATED_TERMS,
) -> list[str]:
    """Pick deterministic corpus concepts to append to the web query."""
    if not related_terms:
        return []

    query_norm = _normalized_text(query)
    query_tokens = _term_tokens(query)
    selected: list[str] = []
    seen: set[str] = set()
    for raw in related_terms:
        term = _normalize_related_term(raw)
        key = _normalized_text(term)
        if not key or key in seen or not _is_useful_related_term(term):
            continue
        if key == query_norm:
            continue
        if not _is_query_aligned_related_term(query_tokens, term):
            continue
        selected.append(term)
        seen.add(key)
        if len(selected) >= max_terms:
            break
    return selected


def _format_search_term(term: str) -> str:
    if " " in term and '"' not in term:
        return f'"{term}"'
    return term


def build_search_query(
    query: str,
    related_terms: list[str] | None = None,
) -> str:
    """Create a compact SearXNG query using only the user's wording."""
    base = re.sub(r"\s+", " ", query.strip())
    return base[:300]


def refine_tool_search_query(
    tool_query: str,
    original_query: str | None = None,
) -> str:
    """Keep model-authored web queries anchored to the user's actual ask.

    This does not filter results or rewrite good refinements. It only catches
    the failure mode where the model sends a tiny ambiguous query such as
    "small" or "on device" for a richer user question.
    """
    tool_query = re.sub(r"\s+", " ", str(tool_query or "").strip())
    original_query = re.sub(r"\s+", " ", str(original_query or "").strip())

    if not original_query:
        return tool_query[:300]
    if not tool_query:
        return original_query[:300]

    tool_tokens = _term_tokens(tool_query)
    original_tokens = _term_tokens(original_query)
    if not original_tokens:
        return tool_query[:300]

    # If the model over-compresses a rich question into a short fragment, use
    # the original wording. SearXNG is especially prone to brand/dictionary
    # hits on fragments like "small" and "on".
    if len(tool_tokens) <= 2 and len(original_tokens) >= 4:
        return original_query[:300]

    # Preserve important acronyms from the user's question. If the user asks
    # about RAG/SLMs/FTS5/etc., the search query should keep those anchors.
    acronyms = [
        token
        for token in re.findall(r"\b[A-Z][A-Z0-9.+#-]{1,}\b", original_query)
        if token.lower() not in {t.lower() for t in tool_tokens}
    ]
    if acronyms:
        tool_query = f"{tool_query} {' '.join(acronyms[:4])}"

    return tool_query[:300]


def _valid_web_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def parse_searxng_results(payload: dict[str, Any], max_results: int) -> list[WebSearchHit]:
    """Normalize SearXNG JSON into bounded, deduped hits."""
    seen: set[str] = set()
    hits: list[WebSearchHit] = []
    for raw in payload.get("results") or []:
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("url") or "").strip()
        if not _valid_web_url(url) or url in seen:
            continue
        title = _strip_html(str(raw.get("title") or url))
        snippet = _strip_html(
            str(raw.get("content") or raw.get("snippet") or raw.get("description") or "")
        )
        if not title and not snippet:
            continue
        engines_raw = raw.get("engines") or raw.get("engine") or ()
        if isinstance(engines_raw, str):
            engines = (engines_raw,)
        elif isinstance(engines_raw, list):
            engines = tuple(str(v) for v in engines_raw if v)
        else:
            engines = ()
        try:
            score = float(raw.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        hits.append(
            WebSearchHit(
                title=title[:180],
                url=url,
                snippet=snippet[:1200],
                score=score,
                engines=engines,
                published_date=(
                    str(raw.get("publishedDate"))
                    if raw.get("publishedDate")
                    else None
                ),
            )
        )
        seen.add(url)
        if len(hits) >= max_results:
            break
    return hits


def _web_source_id(url: str) -> str:
    return sha1(url.encode("utf-8")).hexdigest()[:16]


def web_hits_to_source_chunks(
    hits: list[WebSearchHit],
    *,
    fetched_markdown: dict[str, str] | None = None,
    search_query: str | None = None,
    expanded_terms: list[str] | None = None,
    max_chars: int = _DEFAULT_OBSCURA_MAX_CHARS,
) -> list[SourceChunk]:
    fetched_markdown = fetched_markdown or {}
    expanded_terms = expanded_terms or []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    chunks: list[SourceChunk] = []
    for rank, hit in enumerate(hits, start=1):
        digest = _web_source_id(hit.url)
        page_text = fetched_markdown.get(hit.url, "").strip()
        body = page_text or hit.snippet or "(No snippet returned.)"
        engines = ", ".join(hit.engines) if hit.engines else "unknown"
        published = f"\nPublished: {hit.published_date}" if hit.published_date else ""
        text = (
            f"Live web result fetched_at={now}\n"
            f"Title: {hit.title}\n"
            f"URL: {hit.url}{published}\n"
            f"Engines: {engines}\n"
            f"Search query: {search_query or ''}\n"
            f"Corpus expansion terms: {', '.join(expanded_terms) if expanded_terms else 'none'}\n"
            f"Content: {body}"
        )
        chunks.append(
            SourceChunk(
                chunk_id=f"web:{digest}",
                parent_id=f"web:{digest}",
                doc_id=hit.url,
                corpus_id="live-web",
                text=text[: max(max_chars, 1200)],
                score=max(0.05, 1.0 - (rank - 1) * 0.08),
                source_tier="web_search",
                corpus_name="Live Web",
                doc_name=hit.title or urlparse(hit.url).netloc,
                metadata={
                    "url": hit.url,
                    "engines": list(hit.engines),
                    "rank": rank,
                    "published_date": hit.published_date,
                    "source": "searxng",
                    "search_query": search_query,
                    "expanded_terms": expanded_terms,
                    "full_page_fetched": bool(page_text),
                },
                provenance=[
                    {
                        "retriever": "live_web_search",
                        "url": hit.url,
                        "engines": list(hit.engines),
                    }
                ],
            )
        )
    return chunks


async def rerank_web_source_chunks(
    query: str,
    chunks: list[SourceChunk],
    *,
    limit: int,
) -> list[SourceChunk]:
    """Rank live-web candidate snippets with the same local reranker as RAG.

    SearXNG order is useful recall, not final relevance. We fetch a wider web
    candidate pool, ask Qwen/MLX to rank the snippets against the user's query,
    and only pass the top few websites to the model. If the sidecar is down,
    the reranker client falls back to source score order.
    """

    if not chunks:
        return []
    limit = max(1, min(int(limit or len(chunks)), len(chunks)))
    try:
        from services.reranker import reranker_service

        ranked = await reranker_service.rerank(query, chunks)
    except Exception as exc:
        logger.warning("live web rerank failed, using search order: %s", exc)
        ranked = sorted(chunks, key=lambda chunk: chunk.score, reverse=True)
    return ranked[:limit]


class LiveWebSearch:
    async def maybe_search(
        self,
        *,
        query: str,
        corpus_sources: list[SourceChunk],
        related_terms: list[str] | None = None,
        force: bool = False,
    ) -> list[SourceChunk]:
        settings = get_settings()
        if not force or not settings.LIVE_WEB_SEARCH_ENABLED:
            return []

        expanded_terms = select_related_search_terms(query, related_terms)
        search_query = build_search_query(query, expanded_terms)
        try:
            candidate_limit = max(
                int(settings.LIVE_WEB_SEARCH_MAX_RESULTS or _DEFAULT_MAX_RESULTS),
                int(
                    getattr(
                        settings,
                        "LIVE_WEB_SEARCH_CANDIDATE_RESULTS",
                        _DEFAULT_CANDIDATE_RESULTS,
                    )
                    or _DEFAULT_CANDIDATE_RESULTS
                ),
            )
            hits = await self._search_searxng(
                search_query,
                max_results=candidate_limit,
            )
        except Exception as exc:
            logger.info("live web search skipped: %s", exc)
            return []
        if not hits:
            return []

        fetched: dict[str, str] = {}
        if settings.LIVE_WEB_SEARCH_FETCH_FULL_PAGES and settings.OBSCURA_COMMAND:
            fetched = await self._fetch_pages_with_obscura(hits)
        candidate_chunks = web_hits_to_source_chunks(
            hits,
            fetched_markdown=fetched,
            search_query=search_query,
            expanded_terms=expanded_terms,
            max_chars=settings.OBSCURA_MAX_CHARS,
        )
        selected = await rerank_web_source_chunks(
            search_query,
            candidate_chunks,
            limit=int(settings.LIVE_WEB_SEARCH_MAX_RESULTS or _DEFAULT_MAX_RESULTS),
        )
        logger.info(
            "live web search reranked %d candidate(s) to %d result(s) for query=%r search_query=%r corpus_sources=%d",
            len(hits),
            len(selected),
            query[:120],
            search_query[:240],
            len(corpus_sources or []),
        )
        return selected

    async def _search_searxng(
        self,
        query: str,
        *,
        max_results: int | None = None,
    ) -> list[WebSearchHit]:
        settings = get_settings()
        base_url = settings.SEARXNG_URL.rstrip("/")
        params = {
            "q": query,
            "format": "json",
            "language": "en",
            "safesearch": "1",
        }
        if settings.SEARXNG_ENGINES:
            params["engines"] = settings.SEARXNG_ENGINES
        timeout = httpx.Timeout(settings.SEARXNG_TIMEOUT_SECONDS, connect=2.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(f"{base_url}/search", params=params)
            response.raise_for_status()
            payload = response.json()
        candidate_limit = max_results
        if candidate_limit is None:
            candidate_limit = getattr(
                settings,
                "LIVE_WEB_SEARCH_CANDIDATE_RESULTS",
                _DEFAULT_CANDIDATE_RESULTS,
            )
        return parse_searxng_results(
            payload,
            int(candidate_limit or _DEFAULT_CANDIDATE_RESULTS),
        )

    async def _fetch_pages_with_obscura(
        self,
        hits: list[WebSearchHit],
    ) -> dict[str, str]:
        fetched: dict[str, str] = {}
        for hit in hits:
            text = await self._fetch_one_with_obscura(hit.url)
            if text:
                fetched[hit.url] = text
        return fetched

    async def _fetch_one_with_obscura(self, url: str) -> str | None:
        settings = get_settings()
        command = shlex.split(settings.OBSCURA_COMMAND)
        if not command:
            return None
        args = [
            *command,
            "fetch",
            url,
            "--dump",
            "markdown",
            "--quiet",
            "--timeout",
            str(int(settings.OBSCURA_TIMEOUT_SECONDS)),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=settings.OBSCURA_TIMEOUT_SECONDS + 2,
            )
        except Exception as exc:
            logger.debug("Obscura fetch failed for %s: %s", url, exc)
            return None
        if proc.returncode != 0:
            return None
        text = stdout.decode("utf-8", errors="replace").strip()
        return text[: settings.OBSCURA_MAX_CHARS] if text else None


live_web_search = LiveWebSearch()
