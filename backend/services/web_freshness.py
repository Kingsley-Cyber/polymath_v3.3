"""Opt-in live web lane for chat.

This module is deliberately additive: it never changes corpus retrieval,
ranking, graph expansion, or synthesis selection. Chat only calls it when the
user explicitly enables the Web toggle for that turn.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import shlex
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha1
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from config import get_settings
from models.schemas import SourceChunk
from services.web_cache import web_cache

logger = logging.getLogger(__name__)

_DEFAULT_MAX_RESULTS = 7
_DEFAULT_CANDIDATE_RESULTS = 15
_WEB_SEARCH_RESULTS_PER_QUERY = 5
_DEFAULT_OBSCURA_MAX_CHARS = 4000
_WEB_RERANK_TEXT_MAX_CHARS = 1200
_DEFAULT_FETCH_MAX_PAGES = 6
_DEFAULT_MAX_RELATED_TERMS = 5
_WEB_FETCH_USER_AGENT = (
    "PolymathWebRetriever/1.0 (+https://localhost; local user initiated search)"
)
_DEFAULT_OBSCURA_DOMAINS = (
    "civitai.com",
    "create.roblox.com",
    "gumroad.com",
    "polymarket.com",
    "producthunt.com",
    "rolimons.com",
    "tradingview.com",
)
_FETCH_CACHE_MAX_ITEMS = 512
_WEB_CACHE_SCHEMA_VERSION = "live-web-v2"
_WEB_SEARCH_CACHE_TTL_SECONDS = 600
_WEB_EXTRACTION_VERSION = "page-text-v3-ytdlp"

_RESEARCH_DOMAINS = (
    "arxiv.org",
    "doi.org",
    "aclanthology.org",
    "semanticscholar.org",
    "researchgate.net",
    "paperswithcode.com",
    "openreview.net",
    "ieee.org",
    "springer.com",
    "sciencedirect.com",
    "nature.com",
)

_SOCIAL_DOMAINS = (
    "reddit.com",
    "x.com",
    "twitter.com",
)

_VIDEO_DOMAINS = (
    "youtube.com",
    "youtu.be",
)

_RESEARCH_QUERY_MARKERS = (
    "academic",
    "arxiv",
    "benchmark",
    "cifar-10",
    "dataset",
    "datasets",
    "evaluation",
    "literature",
    "paper",
    "papers",
    "psychogat",
    "research",
    "study",
    "studies",
    "survey",
)

_SOCIAL_QUERY_MARKERS = (
    "adoption",
    "best",
    "community",
    "deploy",
    "deployment",
    "field",
    "implementation",
    "production",
    "reddit",
    "social",
    "twitter",
    "x.com",
    "x/twitter",
    "way ahead",
    "what people",
    "what works",
)

_MODEL_REGISTRY_QUERY_MARKERS = (
    "ai model",
    "embedding model",
    "gguf",
    "hugging face",
    "huggingface",
    "llm",
    "local model",
    "model",
    "models",
    "ollama",
    "on-device",
    "onnx",
    "quantized",
    "reranker",
    "small language model",
    "slm",
    "vram",
)

_NEWS_QUERY_MARKERS = (
    "breaking",
    "headline",
    "headlines",
    "market news",
    "news",
    "press release",
    "today",
)

_FINANCE_QUERY_MARKERS = (
    "10-k",
    "10q",
    "10-q",
    "8-k",
    "analyst",
    "bitcoin",
    "crypto",
    "earnings",
    "equity",
    "fed",
    "filing",
    "finance",
    "financial",
    "inflation",
    "market",
    "markets",
    "option",
    "options",
    "polymarket",
    "prediction market",
    "sec",
    "shares",
    "stock",
    "stocks",
    "ticker",
    "trading",
    "treasury",
)

_TECH_IMPLEMENTATION_QUERY_MARKERS = (
    "api",
    "aws",
    "code",
    "coding",
    "developer",
    "development",
    "docker",
    "docs",
    "documentation",
    "flutter",
    "framework",
    "github",
    "implementation",
    "ios",
    "javascript",
    "library",
    "mdn",
    "next.js",
    "node",
    "package",
    "python",
    "react",
    "sdk",
    "swift",
    "typescript",
)

_GAME_DEV_QUERY_MARKERS = (
    "aigamedev",
    "game ai",
    "game dev",
    "gamedev",
    "godot",
    "luau",
    "roblox",
    "unity",
    "unreal",
)

_ROBLOX_QUERY_MARKERS = (
    "datastore",
    "devforum",
    "instance",
    "luau",
    "remoteevent",
    "remotefunction",
    "roblox",
    "roblox studio",
    "rbx",
    "ugc",
)

_AI_MEDIA_QUERY_MARKERS = (
    "ai media",
    "ai video",
    "civitai",
    "cogvideox",
    "comfyui",
    "controlnet",
    "fal.ai",
    "framepack",
    "hunyuan",
    "hunyuanvideo",
    "lora",
    "ltx video",
    "ltx-video",
    "replicate",
    "stable diffusion",
    "stablediffusion",
    "tensor rt",
    "tensorrt",
    "video generation",
    "wan 2.1",
    "wan2.1",
    "workflow",
)

_CREATOR_ECONOMY_QUERY_MARKERS = (
    "acquire.com",
    "asset marketplace",
    "creator economy",
    "gumroad",
    "indie hacker",
    "indiehackers",
    "make money",
    "market analysis",
    "marketplace",
    "monetization",
    "opportunity discovery",
    "product hunt",
    "producthunt",
    "rolimons",
    "side hustle",
    "trend discovery",
    "ugc",
    "what sells",
)

_VIDEO_QUERY_MARKERS = (
    "conference talk",
    "demo",
    "tutorial",
    "video",
    "walkthrough",
    "wwdc",
    "youtube",
)

_CYBER_QUERY_MARKERS = (
    "cisa",
    "cve",
    "exploit",
    "nist",
    "nvd",
    "owasp",
    "security",
    "vulnerability",
    "vulnerabilities",
)

_EXPLICIT_X_QUERY_MARKERS = (
    "twitter",
    "x.com",
    "x/twitter",
)

_JOB_QUERY_MARKERS = (
    "apply",
    "career",
    "careers",
    "hiring",
    "interview",
    "job",
    "jobs",
    "recruiter",
    "resume",
    "salary",
)

_JOB_BOARD_DOMAINS = {
    "dice.com",
    "glassdoor.com",
    "indeed.com",
    "linkedin.com",
    "monster.com",
    "talent.com",
    "wellfound.com",
    "ziprecruiter.com",
}

_JOB_HIT_TITLE_MARKERS = (
    "apply",
    "career",
    "developer",
    "engineer",
    "hiring",
    "job",
    "jobs",
    "salary",
)

_NON_FINANCE_ACRONYMS = {
    "AI",
    "API",
    "CPU",
    "GPU",
    "HTTP",
    "JSON",
    "LLM",
    "MLX",
    "NPU",
    "RAG",
    "RAM",
    "SDK",
    "UGC",
    "VRAM",
}

_MAX_WEB_SEARCH_QUERY_VARIANTS = 6

_PROTECTED_QUERY_TERMS = (
    "cpu",
    "gpu",
    "npu",
    "vram",
    "ram",
    "gb",
    "mb",
    "4gb",
    "8gb",
    "16gb",
    "32gb",
    "android",
    "ios",
    "iphone",
)

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
    search_query: str | None = None
    time_range: str | None = None
    from_cache: bool = False


@dataclass
class _PageFetchCacheEntry:
    expires_at: float
    text: str


@dataclass
class _PageFetchResult:
    url: str
    text: str | None
    method: str
    status: str
    chars: int = 0
    from_cache: bool = False
    cache_layer: str | None = None
    obscura_attempted: bool = False
    js_rendered: bool = False


@dataclass(frozen=True)
class SnippetSufficiency:
    sufficient: bool
    score: float
    reason: str
    useful_snippet_chars: int
    top3_snippet_chars: int
    useful_snippet_count: int
    distinct_domains: int
    query_coverage: float
    stronger_evidence_required: bool


_PAGE_FETCH_CACHE: dict[str, _PageFetchCacheEntry] = {}


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


def infer_web_search_time_range(query: str) -> str | None:
    """Return a SearXNG time_range for queries that clearly need freshness."""
    text = _normalized_text(query)
    tokens = _term_tokens(query)
    if not text:
        return None

    if (
        "today" in tokens
        or "yesterday" in tokens
        or "this week" in text
        or "last week" in text
    ):
        return "day"

    if any(
        marker in text
        for marker in (
            "breaking change",
            "changelog",
            "cve",
            "latest",
            "earnings",
            "market news",
            "market sentiment",
            "release notes",
            "security advisory",
            "this month",
        )
    ):
        return "month"

    has_current_language = any(
        marker in text
        for marker in (
            "as of",
            "current",
            "currently",
            "recent",
            "this year",
            "up to date",
            "up-to-date",
            "way ahead",
        )
    )
    has_recent_year = bool(re.search(r"\b202[4-9]\b", text))
    if has_current_language or has_recent_year or _contains_marker(text, _FAST_MOVING_TECH_MARKERS):
        return "year"

    return None


def _query_should_include_social_sources(query: str) -> bool:
    text = _normalized_text(query)
    if not text or _query_prefers_research(query):
        return False
    if _contains_marker(text, _SOCIAL_QUERY_MARKERS):
        return True
    return bool(
        _contains_marker(text, _EXPLICIT_WEB_MARKERS)
        and _contains_marker(text, _FAST_MOVING_TECH_MARKERS)
    )


def _query_should_include_model_registry_sources(query: str) -> bool:
    text = _normalized_text(query)
    if not text or _query_prefers_research(query):
        return False
    return _contains_marker(text, _MODEL_REGISTRY_QUERY_MARKERS)


def _looks_like_finance_query(query: str) -> bool:
    text = _normalized_text(query)
    tokens = _term_tokens(query)
    finance_tokens = {
        marker
        for marker in _FINANCE_QUERY_MARKERS
        if " " not in marker and marker not in {"market", "option", "options"}
    }
    if tokens & finance_tokens:
        return True
    if any(
        phrase in text
        for phrase in (
            "crypto market",
            "market data",
            "market news",
            "market sentiment",
            "prediction market",
            "stock market",
        )
    ):
        return True

    # Keep this intentionally conservative so acronyms like "AI" do not turn
    # every technical query into a finance/news query.
    ticker = re.search(r"\b[A-Z]{2,5}\b", query)
    if ticker and ticker.group(0) in _NON_FINANCE_ACRONYMS:
        return False
    return bool(
        ticker
        and re.search(
            r"\b(stock|stocks|earnings|calls?|puts?|options?|shares?|price|market)\b",
            text,
        )
    )


def _query_should_include_news_sources(query: str) -> bool:
    text = _normalized_text(query)
    if _contains_marker(text, _NEWS_QUERY_MARKERS):
        return True
    return bool(
        _looks_like_finance_query(query)
        or (
            _contains_marker(text, _EXPLICIT_WEB_MARKERS)
            and _contains_marker(text, _FAST_MOVING_TECH_MARKERS)
        )
    )


def _query_should_include_implementation_sources(query: str) -> bool:
    text = _normalized_text(query)
    return bool(
        _contains_marker(text, _TECH_IMPLEMENTATION_QUERY_MARKERS)
        or _contains_marker(text, _GAME_DEV_QUERY_MARKERS)
    )


def _looks_like_roblox_query(query: str) -> bool:
    return _contains_marker(_normalized_text(query), _ROBLOX_QUERY_MARKERS)


def _looks_like_ai_media_query(query: str) -> bool:
    return _contains_marker(_normalized_text(query), _AI_MEDIA_QUERY_MARKERS)


def _looks_like_creator_economy_query(query: str) -> bool:
    text = _normalized_text(query)
    return bool(
        _contains_marker(text, _CREATOR_ECONOMY_QUERY_MARKERS)
        or (_contains_marker(text, _GAME_DEV_QUERY_MARKERS) and "market" in text)
    )


def _query_should_include_video_sources(query: str) -> bool:
    text = _normalized_text(query)
    if _query_prefers_primary_sources(query):
        return False
    return bool(
        _contains_marker(text, _VIDEO_QUERY_MARKERS)
        or _looks_like_roblox_query(query)
        or _looks_like_ai_media_query(query)
        or _looks_like_creator_economy_query(query)
    )


def _query_prefers_primary_sources(query: str) -> bool:
    text = _normalized_text(query)
    return _contains_marker(
        text,
        (
            "cite",
            "citation",
            "docs",
            "documentation",
            "official",
            "primary source",
            "source",
            "sources",
        ),
    )


def _looks_like_cyber_query(query: str) -> bool:
    return _contains_marker(_normalized_text(query), _CYBER_QUERY_MARKERS)


def _query_mentions_x(query: str) -> bool:
    return _contains_marker(_normalized_text(query), _EXPLICIT_X_QUERY_MARKERS)


def _build_model_registry_queries(query: str) -> list[str]:
    """Build short Hugging Face Hub queries; its engine is tag/slug oriented."""

    text = _normalized_text(query)
    candidates: list[str] = []

    if "liquid" in text or "lfm" in text:
        candidates.append("LiquidAI LFM2")
    if "bonsai" in text:
        candidates.append("Bonsai 8B")
    if "llama" in text:
        candidates.append("Llama GGUF")
    if "gemma" in text:
        candidates.append("Gemma GGUF")
    if "qwen" in text:
        candidates.append("Qwen GGUF")
    if "phi" in text:
        candidates.append("Phi GGUF")
    if "mistral" in text:
        candidates.append("Mistral GGUF")
    if "smollm" in text or "smol lm" in text:
        candidates.append("SmolLM")
    if "tinyllama" in text or "tiny llama" in text:
        candidates.append("TinyLlama")
    if "wan" in text:
        candidates.append("Wan video")
    if "ltx" in text:
        candidates.append("LTX Video")
    if "hunyuan" in text:
        candidates.append("HunyuanVideo")
    if "framepack" in text:
        candidates.append("FramePack")
    if "cogvideox" in text or "cogvideo" in text:
        candidates.append("CogVideoX")

    if any(marker in text for marker in ("mobile", "on-device", "on device", "edge")):
        candidates.append("mobile llm")
    if "small language model" in text or "slm" in text:
        candidates.append("small language model")
    if any(marker in text for marker in ("gguf", "quantized", "4gb", "vram")):
        candidates.append("GGUF small LLM")
    if not candidates:
        candidates.append(query)

    seen: set[str] = set()
    unique: list[str] = []
    for item in candidates:
        normalized = _normalized_text(item)
        if normalized and normalized not in seen:
            unique.append(item)
            seen.add(normalized)
    return unique[:3]


def _build_cyber_database_query(query: str) -> str:
    """Build a compact NVD query; NVD keyword search is very literal."""

    tokens = [
        token
        for token in _RELATED_TERM_TOKEN_RE.findall(query.lower())
        if token
        and token not in _RELATED_TERM_STOPWORDS
        and token
        not in {
            "cisa",
            "cve",
            "nist",
            "nvd",
            "owasp",
            "security",
            "vulnerability",
            "vulnerabilities",
        }
    ]
    compact = " ".join(tokens[:6]).strip()
    return compact or query


def _build_roblox_creator_doc_queries(query: str) -> list[str]:
    """Build direct Creator Docs searches for named Roblox APIs/classes."""

    text = _normalized_text(query)
    mappings = (
        ("remoteevent", "RemoteEvent"),
        ("remote function", "RemoteFunction"),
        ("remotefunction", "RemoteFunction"),
        ("datastore", "DataStoreService"),
        ("data store", "DataStoreService"),
        ("humanoid", "Humanoid"),
        ("tweenservice", "TweenService"),
        ("tween service", "TweenService"),
        ("runservice", "RunService"),
        ("run service", "RunService"),
        ("players service", "Players"),
        ("proximityprompt", "ProximityPrompt"),
        ("proximity prompt", "ProximityPrompt"),
    )
    variants: list[str] = []
    seen: set[str] = set()
    for marker, class_name in mappings:
        if marker not in text or class_name in seen:
            continue
        variants.append(
            f"{class_name} site:create.roblox.com/docs/reference/engine/classes/{class_name}"
        )
        seen.add(class_name)
        if len(variants) >= 2:
            break
    return variants


def _append_explicit_source_variants(queries: list[str], base: str) -> None:
    """Honor source names the user explicitly put in the query."""

    lower = base.lower()
    explicit_sites = (
        (("product hunt", "producthunt"), "producthunt.com"),
        (("gumroad",), "gumroad.com"),
        (("polymarket",), "polymarket.com"),
        (("rolimons",), "rolimons.com"),
        (("civitai",), "civitai.com"),
        (("replicate",), "replicate.com"),
        (("fal.ai", "fal ai"), "fal.ai"),
        (("indie hackers", "indiehackers"), "indiehackers.com"),
    )
    for markers, domain in explicit_sites:
        if any(marker in lower for marker in markers):
            _append_query_variant(queries, f"{base} site:{domain}")


def _append_query_variant(queries: list[str], variant: str) -> None:
    if len(queries) >= _MAX_WEB_SEARCH_QUERY_VARIANTS:
        return
    variant = re.sub(r"\s+", " ", variant.strip())
    if variant:
        queries.append(variant)


def build_web_search_queries(query: str) -> list[str]:
    """Build one primary query plus bounded source-specific variants."""
    base = build_search_query(query)
    if not base:
        return []

    queries = [base]
    lower = base.lower()
    if not lower.startswith("!") and _query_should_include_model_registry_sources(base):
        for item in _build_model_registry_queries(base):
            _append_query_variant(queries, f"!hfm {item}")

    _append_explicit_source_variants(queries, base)

    if _looks_like_finance_query(base):
        _append_query_variant(queries, f"!reu {base}")
        _append_query_variant(queries, f"!bin {base}")
        _append_query_variant(queries, f"!ddn {base}")
        _append_query_variant(queries, f"!red {base}")
        if "polymarket" in lower or "prediction market" in lower:
            _append_query_variant(queries, f"{base} site:polymarket.com")
        if "sec" in lower or "filing" in lower or "10-k" in lower or "8-k" in lower:
            _append_query_variant(queries, f"{base} site:sec.gov")

    elif _query_prefers_research(base):
        _append_query_variant(queries, f"!arx {base}")
        _append_query_variant(queries, f"!sem {base}")
        _append_query_variant(queries, f"!oa {base}")

    if _looks_like_creator_economy_query(base):
        if _looks_like_roblox_query(base):
            _append_query_variant(queries, f"{base} site:rolimons.com")
            _append_query_variant(queries, f"{base} site:devforum.roblox.com")
        _append_query_variant(queries, f"!red {base}")
        _append_query_variant(queries, f"!yt {base} trend analysis")
        _append_query_variant(queries, f"{base} site:gumroad.com")
        _append_query_variant(queries, f"{base} site:producthunt.com")
        _append_query_variant(queries, f"{base} site:indiehackers.com")

    elif _looks_like_roblox_query(base):
        for item in _build_roblox_creator_doc_queries(base):
            _append_query_variant(queries, item)
        _append_query_variant(queries, f"{base} site:create.roblox.com/docs")
        _append_query_variant(queries, f"{base} site:devforum.roblox.com")
        _append_query_variant(queries, f"!gh roblox luau {base}")
        if not _query_prefers_primary_sources(base):
            _append_query_variant(queries, f"!yt roblox {base} tutorial")
            _append_query_variant(queries, f"!red roblox {base}")

    elif _looks_like_ai_media_query(base):
        for item in _build_model_registry_queries(base):
            _append_query_variant(queries, f"!hfm {item}")
        _append_query_variant(queries, f"!gh ComfyUI {base}")
        _append_query_variant(queries, f"{base} site:civitai.com")
        _append_query_variant(queries, f"{base} site:replicate.com")
        _append_query_variant(queries, f"{base} site:fal.ai")
        _append_query_variant(queries, f"!yt {base} workflow tutorial")
        _append_query_variant(queries, f"!red {base}")

    elif _looks_like_cyber_query(base):
        _append_query_variant(queries, f"!nvd {_build_cyber_database_query(base)}")
        _append_query_variant(queries, f"{base} site:cisa.gov")
        _append_query_variant(queries, f"{base} site:owasp.org")
        _append_query_variant(queries, f"!gh {base}")

    if "react" in lower:
        _append_query_variant(queries, f"{base} site:react.dev")
        _append_query_variant(queries, f"!mdn {base}")
    if any(marker in lower for marker in ("javascript", "typescript", "web api", "css", "html", "mdn")):
        _append_query_variant(queries, f"!mdn {base}")
    if any(marker in lower for marker in ("ios", "swift", "swiftui", "xcode", "wwdc")):
        _append_query_variant(queries, f"{base} site:developer.apple.com")
        _append_query_variant(queries, f"!yt WWDC {base}")
    if "android" in lower or "kotlin" in lower:
        _append_query_variant(queries, f"{base} site:developer.android.com")
    if "flutter" in lower or "dart" in lower:
        _append_query_variant(queries, f"{base} site:docs.flutter.dev")
        _append_query_variant(queries, f"{base} site:pub.dev")
    if "docker" in lower:
        _append_query_variant(queries, f"{base} site:docs.docker.com")
    if "aws" in lower or "lambda" in lower:
        _append_query_variant(queries, f"{base} site:docs.aws.amazon.com")

    if _query_should_include_implementation_sources(base):
        _append_query_variant(queries, f"!gh {base}")
        _append_query_variant(queries, f"!hn {base}")
        _append_query_variant(queries, f"!so {base}")
        if _contains_marker(lower, _GAME_DEV_QUERY_MARKERS):
            _append_query_variant(queries, f"!gdse {base}")

    if "site:" not in lower and _query_should_include_social_sources(base):
        _append_query_variant(queries, f"!red {base}")
        _append_query_variant(queries, f"!hn {base}")
        _append_query_variant(queries, f"!lem {base}")

    if _query_should_include_video_sources(base):
        _append_query_variant(queries, f"!yt {base}")

    if "site:" not in lower and _query_mentions_x(base):
        _append_query_variant(queries, f"{base} site:x.com OR site:twitter.com")

    if (
        not _looks_like_finance_query(base)
        and not _query_prefers_research(base)
        and _query_should_include_news_sources(base)
    ):
        _append_query_variant(queries, f"!bin {base}")
        _append_query_variant(queries, f"!gn {base}")
        _append_query_variant(queries, f"!ddn {base}")

    if _contains_marker(lower, _GAME_DEV_QUERY_MARKERS):
        if "unity" in lower:
            _append_query_variant(queries, f"{base} site:docs.unity3d.com")
        if "unreal" in lower:
            _append_query_variant(queries, f"{base} site:dev.epicgames.com/documentation")
        if "godot" in lower:
            _append_query_variant(queries, f"{base} site:docs.godotengine.org")

    seen: set[str] = set()
    unique: list[str] = []
    for item in queries:
        normalized = _normalized_text(item)
        if not normalized or normalized in seen:
            continue
        unique.append(item[:300])
        seen.add(normalized)
        if len(unique) >= _MAX_WEB_SEARCH_QUERY_VARIANTS:
            break
    return unique


def _variant_allowed_domains(query: str) -> tuple[str, ...]:
    text = query.lower()
    if text.startswith("!hf") or text.startswith("!hfm"):
        return ("huggingface.co",)
    if text.startswith("!red"):
        return ("reddit.com",)
    if text.startswith("!hn"):
        return ("news.ycombinator.com",)
    if text.startswith("!gh"):
        return ("github.com",)
    if text.startswith("!ghc"):
        return ("github.com",)
    if text.startswith("!arx"):
        return ("arxiv.org",)
    if text.startswith("!reu"):
        return ("reuters.com",)
    if text.startswith("!so") or text.startswith("!sx"):
        return ("stackoverflow.com", "stackexchange.com")
    if text.startswith("!gdse"):
        return ("gamedev.stackexchange.com", "stackexchange.com")
    if text.startswith("!yt"):
        return ("youtube.com", "youtu.be")
    if text.startswith("!yp") or text.startswith("!ppd"):
        return ("piped.video", "srv.piped.video")
    if text.startswith("!mdn"):
        return ("developer.mozilla.org",)
    if text.startswith("!nvd"):
        return ("nvd.nist.gov",)
    if "site:reddit.com" in text:
        return ("reddit.com",)
    if "site:x.com" in text or "site:twitter.com" in text:
        return ("x.com", "twitter.com")
    if "site:polymarket.com" in text:
        return ("polymarket.com",)
    if "site:rolimons.com" in text:
        return ("rolimons.com",)
    if "site:devforum.roblox.com" in text:
        return ("devforum.roblox.com",)
    if "site:create.roblox.com" in text:
        return ("create.roblox.com",)
    if "site:civitai.com" in text:
        return ("civitai.com",)
    if "site:replicate.com" in text:
        return ("replicate.com",)
    if "site:fal.ai" in text:
        return ("fal.ai",)
    if "site:producthunt.com" in text:
        return ("producthunt.com",)
    if "site:indiehackers.com" in text:
        return ("indiehackers.com",)
    if "site:gumroad.com" in text:
        return ("gumroad.com",)
    if "site:sec.gov" in text:
        return ("sec.gov",)
    if "site:cisa.gov" in text:
        return ("cisa.gov",)
    if "site:owasp.org" in text:
        return ("owasp.org",)
    if "site:react.dev" in text:
        return ("react.dev",)
    if "site:developer.apple.com" in text:
        return ("developer.apple.com",)
    if "site:developer.android.com" in text:
        return ("developer.android.com",)
    if "site:docs.flutter.dev" in text:
        return ("docs.flutter.dev",)
    if "site:pub.dev" in text:
        return ("pub.dev",)
    if "site:docs.docker.com" in text:
        return ("docs.docker.com",)
    if "site:docs.aws.amazon.com" in text:
        return ("docs.aws.amazon.com",)
    if "site:docs.unity3d.com" in text:
        return ("docs.unity3d.com",)
    if "site:dev.epicgames.com" in text:
        return ("dev.epicgames.com",)
    if "site:docs.godotengine.org" in text:
        return ("docs.godotengine.org",)
    return ()


def _is_x_post_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    domain = _web_domain(url)
    if domain not in {"x.com", "twitter.com"}:
        return False
    return bool(re.search(r"/status(?:es)?/\d+", parsed.path))


def _is_reddit_thread_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if not _is_social_domain(_web_domain(url)):
        return False
    return "/comments/" in parsed.path


def _is_low_quality_web_hit(hit: WebSearchHit) -> bool:
    domain = _web_domain(hit.url)
    title = (hit.title or "").strip().lower()
    if domain in {"x.com", "twitter.com"}:
        return not _is_x_post_url(hit.url)
    if domain.endswith("reddit.com"):
        return not _is_reddit_thread_url(hit.url)
    if title in {"x", "twitter", "reddit"} or " / x" in title:
        return True
    return False


def _looks_like_job_search_query(query: str) -> bool:
    return bool(set(_term_tokens(query)) & set(_JOB_QUERY_MARKERS))


def _is_job_listing_hit(hit: WebSearchHit) -> bool:
    domain = _web_domain(hit.url)
    title = _normalized_text(hit.title or "")
    if any(domain == item or domain.endswith(f".{item}") for item in _JOB_BOARD_DOMAINS):
        return True
    if "/jobs" in (urlparse(hit.url).path or "").lower():
        return True
    return any(marker in title for marker in _JOB_HIT_TITLE_MARKERS) and any(
        marker in title for marker in ("job", "jobs", "hiring", "salary", "apply")
    )


_BROAD_QUERY_OVERLAP_TERMS = frozenset(
    {
        "application",
        "applications",
        "dataset",
        "datasets",
        "enterprise",
        "example",
        "guide",
        "latest",
        "model",
        "patterns",
        "security",
        "system",
        "systems",
        "today",
        "validation",
    }
)


def _distinctive_query_tokens(query: str) -> set[str]:
    tokens = _term_tokens(query)
    return {
        token
        for token in tokens
        if token not in _BROAD_QUERY_OVERLAP_TERMS
        and (len(token) >= 6 or any(ch.isdigit() for ch in token) or "-" in token)
    }


def _hit_matches_distinctive_query_tokens(hit: WebSearchHit, query: str) -> bool:
    distinctive = _distinctive_query_tokens(query)
    if not distinctive:
        return True
    haystack = " ".join(
        str(value or "")
        for value in (hit.title, hit.snippet, hit.url)
    )
    hit_tokens = _term_tokens(haystack)
    if "remoteevent" in distinctive and (
        "remoteevents" in hit_tokens or {"remote", "events"} <= hit_tokens
    ):
        return True
    return bool(distinctive & hit_tokens)


def _is_low_quality_web_hit_for_query(hit: WebSearchHit, query: str) -> bool:
    if _is_low_quality_web_hit(hit):
        return True
    domain = _web_domain(hit.url)
    if _query_prefers_primary_sources(query) and _is_social_domain(domain):
        return True
    if (
        _query_prefers_primary_sources(query)
        and "missing:" in str(hit.snippet or "").lower()
        and not (
            {"remoteevent", "onserverevent", "security", "validation"}
            & _term_tokens(hit.title or "")
        )
    ):
        return True
    if not _hit_matches_distinctive_query_tokens(hit, query):
        return True
    return _is_job_listing_hit(hit) and not _looks_like_job_search_query(query)


def _is_video_domain(domain: str) -> bool:
    return any(domain == item or domain.endswith(f".{item}") for item in _VIDEO_DOMAINS)


def _extract_json3_caption_text(raw: str) -> str:
    try:
        payload = json.loads(raw)
    except Exception:
        return ""
    lines: list[str] = []
    for event in payload.get("events") or []:
        if not isinstance(event, dict):
            continue
        text = "".join(
            str(seg.get("utf8") or "")
            for seg in event.get("segs") or []
            if isinstance(seg, dict)
        )
        text = html.unescape(re.sub(r"\s+", " ", text)).strip()
        if text:
            lines.append(text)
    return _dedupe_caption_lines(lines)


def _extract_vtt_caption_text(raw: str) -> str:
    lines: list[str] = []
    for line in raw.splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.upper().startswith("WEBVTT"):
            continue
        if "-->" in cleaned or re.fullmatch(r"\d+", cleaned):
            continue
        cleaned = re.sub(r"<[^>]+>", "", cleaned)
        cleaned = html.unescape(re.sub(r"\s+", " ", cleaned)).strip()
        if cleaned:
            lines.append(cleaned)
    return _dedupe_caption_lines(lines)


def _dedupe_caption_lines(lines: list[str]) -> str:
    deduped: list[str] = []
    previous = ""
    for line in lines:
        if line == previous:
            continue
        deduped.append(line)
        previous = line
    return " ".join(deduped)


def _explicit_web_search_target(original_query: str) -> str | None:
    patterns = (
        r"\b(?:search|look up|find)\s+(?:the\s+)?(?:live\s+)?(?:web\s+)?(?:search\s+)?for\s*:?\s*(?P<target>[^.?!]+)",
        r"\bsearch\s+query\s*:?\s*(?P<target>[^.?!]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, original_query, flags=re.IGNORECASE)
        if not match:
            continue
        target = re.sub(r"\s+", " ", match.group("target").strip(" :;-"))
        if len(_term_tokens(target)) >= 3:
            return target[:300]
    return None


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

    explicit_target = _explicit_web_search_target(original_query)
    if explicit_target:
        target_tokens = set(_term_tokens(explicit_target))
        if target_tokens:
            if set(tool_tokens).issubset(target_tokens):
                return explicit_target[:300]
            overlap = target_tokens & set(tool_tokens)
            if len(overlap) / len(target_tokens) >= 0.67:
                return explicit_target[:300]

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

    missing_protected_terms = [
        token
        for token in original_tokens
        if token in _PROTECTED_QUERY_TERMS and token not in tool_tokens
    ]
    if missing_protected_terms:
        tool_query = f"{tool_query} {' '.join(missing_protected_terms[:4])}"

    return tool_query[:300]


def _valid_web_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _web_domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return ""
    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    host = host.split(":", 1)[0].strip(".")
    return host[4:] if host.startswith("www.") else host


def _stable_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha1(raw.encode("utf-8")).hexdigest()


def _web_cache_key(kind: str, payload: dict[str, Any]) -> str:
    return f"polymath:web:{_WEB_CACHE_SCHEMA_VERSION}:{kind}:{_stable_hash(payload)}"


def _search_cache_key(
    query: str,
    *,
    engines: str | None,
    time_range: str | None,
    candidate_limit: int,
) -> str:
    return _web_cache_key(
        "search",
        {
            "query": _normalized_text(query),
            "engines": _normalized_text(engines or ""),
            "time_range": time_range or "",
            "candidate_limit": int(candidate_limit),
            "parser_version": "searxng-results-v2",
        },
    )


def _serialize_hit(hit: WebSearchHit) -> dict[str, Any]:
    return {
        "title": hit.title,
        "url": hit.url,
        "snippet": hit.snippet,
        "score": hit.score,
        "engines": list(hit.engines),
        "published_date": hit.published_date,
        "search_query": hit.search_query,
        "time_range": hit.time_range,
    }


def _deserialize_hit(raw: dict[str, Any], *, from_cache: bool) -> WebSearchHit | None:
    if not isinstance(raw, dict):
        return None
    url = str(raw.get("url") or "")
    if not _valid_web_url(url):
        return None
    engines_raw = raw.get("engines") or ()
    engines = tuple(str(v) for v in engines_raw if v) if isinstance(engines_raw, list) else ()
    try:
        score = float(raw.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    return WebSearchHit(
        title=str(raw.get("title") or url)[:180],
        url=url,
        snippet=str(raw.get("snippet") or "")[:1200],
        score=score,
        engines=engines,
        published_date=str(raw.get("published_date")) if raw.get("published_date") else None,
        search_query=str(raw.get("search_query")) if raw.get("search_query") else None,
        time_range=str(raw.get("time_range")) if raw.get("time_range") else None,
        from_cache=from_cache,
    )


def _page_fetch_cache_key(
    url: str,
    *,
    fetcher: str,
    max_chars: int,
    obscura_domains: str | None = None,
) -> str:
    return _web_cache_key(
        "page",
        {
            "url": url.strip(),
            "domain": _web_domain(url),
            "fetcher": _normalized_text(fetcher),
            "max_chars": int(max_chars),
            "obscura_domains": _normalized_text(obscura_domains or ""),
            "extraction_version": _WEB_EXTRACTION_VERSION,
        },
    )


def _get_page_fetch_cache(key: str) -> str | None:
    entry = _PAGE_FETCH_CACHE.get(key)
    if not entry:
        return None
    if entry.expires_at <= time.monotonic():
        _PAGE_FETCH_CACHE.pop(key, None)
        return None
    return entry.text


def _put_page_fetch_cache(key: str, text: str, *, ttl_seconds: int) -> None:
    if ttl_seconds <= 0 or not text:
        return
    if len(_PAGE_FETCH_CACHE) >= _FETCH_CACHE_MAX_ITEMS:
        oldest_key = min(
            _PAGE_FETCH_CACHE,
            key=lambda item: _PAGE_FETCH_CACHE[item].expires_at,
        )
        _PAGE_FETCH_CACHE.pop(oldest_key, None)
    _PAGE_FETCH_CACHE[key] = _PageFetchCacheEntry(
        expires_at=time.monotonic() + ttl_seconds,
        text=text,
    )


async def _get_page_fetch_cache_async(key: str) -> tuple[str | None, str | None]:
    cached = _get_page_fetch_cache(key)
    if cached:
        return cached, "memory"
    payload = await web_cache.get_json(key)
    if not payload:
        return None, None
    if payload.get("schema_version") != _WEB_CACHE_SCHEMA_VERSION:
        return None, None
    text = str(payload.get("text") or "").strip()
    if not text:
        return None, None
    ttl = int(payload.get("ttl_seconds") or 60)
    _put_page_fetch_cache(key, text, ttl_seconds=max(1, min(ttl, 300)))
    return text, "redis"


async def _put_page_fetch_cache_async(
    key: str,
    text: str,
    *,
    ttl_seconds: int,
) -> None:
    _put_page_fetch_cache(key, text, ttl_seconds=ttl_seconds)
    await web_cache.set_json(
        key,
        {
            "schema_version": _WEB_CACHE_SCHEMA_VERSION,
            "text": text,
            "ttl_seconds": ttl_seconds,
        },
        ttl_seconds=ttl_seconds,
    )


def _raw_source_candidate_urls(url: str) -> list[str]:
    """Return deterministic raw/API URLs for known source-backed pages."""

    parsed = urlparse(url)
    domain = _web_domain(url)
    path = parsed.path.strip("/")

    if domain == "create.roblox.com" and path.startswith("docs/"):
        docs_path = path.removeprefix("docs/").strip("/")
        if docs_path.startswith("reference/engine/classes/"):
            parts = docs_path.split("/")
            if len(parts) > 4:
                docs_path = "/".join(parts[:4])
            return [
                "https://raw.githubusercontent.com/Roblox/creator-docs/main/"
                f"content/en-us/{docs_path}.yaml"
            ]
        return [
            "https://raw.githubusercontent.com/Roblox/creator-docs/main/"
            f"content/en-us/{docs_path}.md"
        ]

    if domain == "github.com":
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 5 and parts[2] == "blob":
            owner, repo, branch = parts[0], parts[1], parts[3]
            file_path = "/".join(parts[4:])
            return [
                f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{file_path}"
            ]
        if len(parts) >= 2:
            owner, repo = parts[0], parts[1]
            return [
                f"https://raw.githubusercontent.com/{owner}/{repo}/main/README.md",
                f"https://raw.githubusercontent.com/{owner}/{repo}/master/README.md",
            ]

    if domain == "huggingface.co":
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] not in {"models", "datasets", "spaces"}:
            repo_id = "/".join(parts[:2])
            return [f"https://huggingface.co/{repo_id}/raw/main/README.md"]
        if len(parts) >= 3 and parts[0] in {"models", "datasets", "spaces"}:
            repo_id = "/".join(parts[1:3])
            return [f"https://huggingface.co/{repo_id}/raw/main/README.md"]

    return []


def _parse_domain_list(value: str | None) -> set[str]:
    if value is None:
        return set(_DEFAULT_OBSCURA_DOMAINS)
    domains = {
        item.strip().lower().removeprefix("www.")
        for item in value.split(",")
        if item.strip()
    }
    return domains


def _domain_matches(domain: str, allowed: set[str]) -> bool:
    return any(domain == item or domain.endswith(f".{item}") for item in allowed)


def _obscura_command_args(
    command: str | None,
    *,
    url: str,
    timeout_seconds: float,
) -> list[str] | None:
    if not command or not command.strip():
        return None
    try:
        base = shlex.split(command)
    except ValueError as exc:
        logger.warning("Invalid OBSCURA_COMMAND %r: %s", command, exc)
        return None
    if not base:
        return None
    return [
        *base,
        "fetch",
        url,
        "--dump",
        "markdown",
        "--quiet",
        "--timeout",
        str(int(timeout_seconds)),
    ]


def _is_research_domain(domain: str) -> bool:
    return any(domain == d or domain.endswith(f".{d}") for d in _RESEARCH_DOMAINS)


def _is_social_domain(domain: str) -> bool:
    return any(domain == d or domain.endswith(f".{d}") for d in _SOCIAL_DOMAINS)


def _query_prefers_research(query: str) -> bool:
    tokens = set(_term_tokens(query))
    return any(marker in tokens for marker in _RESEARCH_QUERY_MARKERS)


def _is_research_source(chunk: SourceChunk) -> bool:
    metadata = chunk.metadata or {}
    url = str(metadata.get("url") or chunk.doc_id or "")
    domain = _web_domain(url)
    title = (chunk.doc_name or "").lower()
    if _is_research_domain(domain):
        return True
    return "arxiv" in title or "research paper" in title or url.lower().endswith(".pdf")


def _is_social_source(chunk: SourceChunk) -> bool:
    metadata = chunk.metadata or {}
    url = str(metadata.get("url") or chunk.doc_id or "")
    return _is_social_domain(_web_domain(url))


_SOURCE_VERIFICATION_QUERY_MARKERS = (
    "according to",
    "cite",
    "citation",
    "exact",
    "official",
    "primary source",
    "quote",
    "source",
    "verify",
    "verbatim",
)


def _query_requires_stronger_web_evidence(query: str) -> bool:
    text = _normalized_text(query)
    return bool(
        _contains_marker(text, _SOURCE_VERIFICATION_QUERY_MARKERS)
        or _contains_marker(text, _TECH_IMPLEMENTATION_QUERY_MARKERS)
        or _contains_marker(text, _CYBER_QUERY_MARKERS)
    )


def _useful_snippet_text(hit: WebSearchHit) -> str:
    snippet = _strip_html(hit.snippet or "")
    lower = snippet.lower()
    if not snippet or len(snippet) < 40:
        return ""
    if lower in {"no results found", "no description available"}:
        return ""
    if lower.count("...") >= 5 and len(snippet) < 180:
        return ""
    return snippet


def assess_snippet_sufficiency(
    query: str,
    hits: list[WebSearchHit],
) -> SnippetSufficiency:
    """Deterministically decide whether snippets are enough context.

    This deliberately stays heuristic and testable. Stronger source-sensitive
    queries can still pass, but only with richer snippets, more domains, and
    better coverage of the user's terms.
    """

    if not hits:
        return SnippetSufficiency(
            sufficient=False,
            score=0.0,
            reason="no_hits",
            useful_snippet_chars=0,
            top3_snippet_chars=0,
            useful_snippet_count=0,
            distinct_domains=0,
            query_coverage=0.0,
            stronger_evidence_required=_query_requires_stronger_web_evidence(query),
        )

    useful: list[tuple[WebSearchHit, str]] = [
        (hit, snippet)
        for hit in hits
        if (snippet := _useful_snippet_text(hit))
    ]
    distinct_domains = len({_web_domain(hit.url) for hit, _ in useful if hit.url})
    snippet_lengths = sorted((len(snippet) for _, snippet in useful), reverse=True)
    total_chars = sum(min(length, 800) for length in snippet_lengths)
    top3_chars = sum(snippet_lengths[:3])

    query_terms = {
        token
        for token in _term_tokens(query)
        if len(token) >= 3 and token not in _RELATED_TERM_STOPWORDS
    }
    covered_terms: set[str] = set()
    combined = " ".join(f"{hit.title} {snippet}" for hit, snippet in useful).lower()
    for token in query_terms:
        if token in combined:
            covered_terms.add(token)
    denominator = min(max(len(query_terms), 1), 8)
    coverage = min(1.0, len(covered_terms) / denominator)

    stronger = _query_requires_stronger_web_evidence(query)
    char_score = min(1.0, total_chars / (1800 if stronger else 950))
    top3_score = min(1.0, top3_chars / (1200 if stronger else 600))
    domain_score = min(1.0, distinct_domains / (3 if stronger else 2))
    count_score = min(1.0, len(useful) / (4 if stronger else 3))
    coverage_score = min(1.0, coverage / (0.7 if stronger else 0.45))
    score = round(
        (
            char_score * 0.25
            + top3_score * 0.25
            + domain_score * 0.2
            + count_score * 0.1
            + coverage_score * 0.2
        ),
        3,
    )

    if stronger and any(marker in _normalized_text(query) for marker in ("exact", "quote", "verbatim")):
        return SnippetSufficiency(
            sufficient=False,
            score=score,
            reason="exact_source_request_requires_page_fetch",
            useful_snippet_chars=total_chars,
            top3_snippet_chars=top3_chars,
            useful_snippet_count=len(useful),
            distinct_domains=distinct_domains,
            query_coverage=round(coverage, 3),
            stronger_evidence_required=True,
        )

    sufficient = score >= (0.88 if stronger else 0.78)
    reason = "sufficient_snippets" if sufficient else "thin_or_low_coverage_snippets"
    return SnippetSufficiency(
        sufficient=sufficient,
        score=score,
        reason=reason,
        useful_snippet_chars=total_chars,
        top3_snippet_chars=top3_chars,
        useful_snippet_count=len(useful),
        distinct_domains=distinct_domains,
        query_coverage=round(coverage, 3),
        stronger_evidence_required=stronger,
    )


def _practical_research_limit(limit: int) -> int:
    return 2 if limit >= 5 else 1


def _diversify_web_source_chunks(
    query: str,
    ranked: list[SourceChunk],
    *,
    limit: int,
) -> list[SourceChunk]:
    """Keep final web context source-diverse after semantic reranking.

    The reranker is relevance-only, so practical deployment questions can get
    crowded by near-duplicate academic pages. Keep the highest-ranked result
    from each domain first, and cap research/paper sources to one unless the
    user explicitly asked for research literature.
    """
    if len(ranked) <= limit:
        return ranked[:limit]

    allow_research_stack = _query_prefers_research(query)
    research_limit = limit if allow_research_stack else _practical_research_limit(limit)
    selected: list[SourceChunk] = []
    deferred: list[SourceChunk] = []
    seen_domains: set[str] = set()
    research_count = 0

    for chunk in ranked:
        metadata = chunk.metadata or {}
        domain = _web_domain(str(metadata.get("url") or chunk.doc_id or ""))
        research = _is_research_source(chunk)
        duplicate_domain = bool(domain and domain in seen_domains)
        too_much_research = research and research_count >= research_limit

        if duplicate_domain or too_much_research:
            deferred.append(chunk)
            continue

        selected.append(chunk)
        if domain:
            seen_domains.add(domain)
        if research:
            research_count += 1
        if len(selected) >= limit:
            break

    for chunk in deferred:
        if chunk in selected:
            continue
        selected.append(chunk)
        if len(selected) >= limit:
            break

    if (
        _query_should_include_social_sources(query)
        and selected
        and not any(_is_social_source(chunk) for chunk in selected)
    ):
        social_candidate = next(
            (chunk for chunk in ranked if _is_social_source(chunk)),
            None,
        )
        if social_candidate and social_candidate not in selected:
            if len(selected) < limit:
                selected.append(social_candidate)
            else:
                selected[-1] = social_candidate
    return selected[:limit]


def _extract_webpage_text(html_text: str, *, max_chars: int) -> str | None:
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
        tag.decompose()

    candidates = []
    for selector in ("main", "article"):
        candidates.extend(soup.select(selector))
    root = max(candidates, key=lambda node: len(node.get_text(" ", strip=True)), default=soup)

    parts: list[str] = []
    if soup.title and soup.title.string:
        parts.append(soup.title.string)
    text = root.get_text(" ", strip=True)
    if text:
        parts.append(text)
    cleaned = re.sub(r"\s+", " ", " ".join(parts)).strip()
    if len(cleaned) < 200:
        return None
    return cleaned[:max_chars]


def _extract_with_trafilatura(
    html_text: str,
    *,
    url: str,
    max_chars: int,
) -> str | None:
    try:
        import trafilatura
    except Exception:
        return None
    try:
        extracted = trafilatura.extract(
            html_text,
            url=url,
            output_format="markdown",
            include_comments=False,
            include_tables=True,
        )
    except Exception as exc:
        logger.debug("Trafilatura extraction failed for %s: %s", url, exc)
        return None
    cleaned = re.sub(r"\s+", " ", extracted or "").strip()
    if len(cleaned) < 200:
        return None
    return cleaned[:max_chars]


def _extract_static_page_text(
    html_text: str,
    *,
    url: str,
    max_chars: int,
    fetcher: str,
) -> str | None:
    if fetcher in {"auto", "trafilatura"}:
        text = _extract_with_trafilatura(html_text, url=url, max_chars=max_chars)
        if text:
            return text
    return _extract_webpage_text(html_text, max_chars=max_chars)


def parse_searxng_results(
    payload: dict[str, Any],
    max_results: int,
    *,
    search_query: str | None = None,
    time_range: str | None = None,
) -> list[WebSearchHit]:
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
                search_query=search_query,
                time_range=time_range,
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
    fetch_stats_by_url: dict[str, dict[str, Any]] | None = None,
    search_query: str | None = None,
    expanded_terms: list[str] | None = None,
    max_chars: int = _DEFAULT_OBSCURA_MAX_CHARS,
) -> list[SourceChunk]:
    fetched_markdown = fetched_markdown or {}
    fetch_stats_by_url = fetch_stats_by_url or {}
    expanded_terms = expanded_terms or []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    chunks: list[SourceChunk] = []
    for rank, hit in enumerate(hits, start=1):
        digest = _web_source_id(hit.url)
        page_text = fetched_markdown.get(hit.url, "").strip()
        fetch_stat = fetch_stats_by_url.get(hit.url) or {}
        body = page_text or hit.snippet or "(No snippet returned.)"
        fetch_status = str(fetch_stat.get("status") or ("ok" if page_text else "snippet_only"))
        fetch_method = str(fetch_stat.get("method") or ("full_page" if page_text else "snippet"))
        evidence_mode = (
            "full_page"
            if page_text
            else "snippet_fetch_failed"
            if fetch_status not in {"snippet_only", "ok"}
            else "snippet_only"
        )
        engines = ", ".join(hit.engines) if hit.engines else "unknown"
        published = f"\nPublished: {hit.published_date}" if hit.published_date else ""
        hit_query = hit.search_query or search_query or ""
        text = (
            f"Live web result fetched_at={now}\n"
            f"Title: {hit.title}\n"
            f"URL: {hit.url}{published}\n"
            f"Engines: {engines}\n"
            f"Search query: {hit_query}\n"
            f"Freshness filter: {hit.time_range or 'none'}\n"
            f"Evidence mode: {evidence_mode}\n"
            f"Fetch status: {fetch_status}\n"
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
                    "search_query": hit_query,
                    "time_range": hit.time_range,
                    "expanded_terms": expanded_terms,
                    "full_page_fetched": bool(page_text),
                    "evidence_mode": evidence_mode,
                    "fetch_method": fetch_method,
                    "fetch_status": fetch_status,
                    "fetch_failed": (
                        bool(fetch_stat)
                        and not page_text
                        and fetch_status
                        not in {"ok", "cache_hit", "snippet_only"}
                    ),
                    "cache_hit": bool(hit.from_cache or fetch_stat.get("from_cache")),
                    "search_cache_hit": bool(hit.from_cache),
                    "page_cache_hit": bool(fetch_stat.get("from_cache")),
                    "cache_layer": fetch_stat.get("cache_layer"),
                    "obscura_attempted": bool(fetch_stat.get("obscura_attempted")),
                    "js_rendered": bool(fetch_stat.get("js_rendered")),
                    "web_content_untrusted": True,
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

        original_by_id = {chunk.chunk_id: chunk for chunk in chunks}
        clipped_chunks: list[SourceChunk] = []
        for chunk in chunks:
            rerank_chunk = chunk.model_copy()
            rerank_chunk.text = rerank_chunk.text[:_WEB_RERANK_TEXT_MAX_CHARS]
            clipped_chunks.append(rerank_chunk)

        reranked_clipped = await reranker_service.rerank(query, clipped_chunks)
        ranked = []
        for clipped in reranked_clipped:
            original = original_by_id.get(clipped.chunk_id)
            if original is None:
                continue
            restored = original.model_copy()
            restored.score = clipped.score
            ranked.append(restored)
    except Exception as exc:
        logger.warning("live web rerank failed, using search order: %s", exc)
        ranked = sorted(chunks, key=lambda chunk: chunk.score, reverse=True)
    return _diversify_web_source_chunks(query, ranked, limit=limit)


class LiveWebSearch:
    async def _fetch_pages_for_search(
        self,
        *,
        search_query: str,
        hits: list[WebSearchHit],
        max_results: int,
        prior_web_urls: set[str] | None = None,
    ) -> tuple[dict[str, str], list[dict[str, Any]], list[WebSearchHit], dict[str, Any]]:
        settings = get_settings()
        sufficiency = assess_snippet_sufficiency(search_query, hits)
        telemetry: dict[str, Any] = {
            "cache_schema_version": _WEB_CACHE_SCHEMA_VERSION,
            "redis_search_cache_hit": any(hit.from_cache for hit in hits),
            "redis_search_cache_hit_count": sum(1 for hit in hits if hit.from_cache),
            "snippet_only": False,
            "snippet_sufficiency_score": sufficiency.score,
            "snippet_sufficiency_reason": sufficiency.reason,
            "snippet_sufficiency": {
                "useful_snippet_chars": sufficiency.useful_snippet_chars,
                "top3_snippet_chars": sufficiency.top3_snippet_chars,
                "useful_snippet_count": sufficiency.useful_snippet_count,
                "distinct_domains": sufficiency.distinct_domains,
                "query_coverage": sufficiency.query_coverage,
                "stronger_evidence_required": sufficiency.stronger_evidence_required,
            },
            "selected_full_page_urls": [],
            "skipped_full_page_fetch_reason": None,
            "conversation_url_dedupe_count": 0,
            "skipped_fetch_existing_url_count": 0,
            "duplicate_url_same_turn_count": 0,
            "redis_page_cache_hit": False,
            "redis_page_cache_hit_count": 0,
            "avg_pages_fetched": 0.0,
            "obscura_attempt_rate": 0.0,
            "obscura_success_rate": 0.0,
            "snippet_sufficiency_pass_rate": 1.0 if sufficiency.sufficient else 0.0,
        }

        if not getattr(settings, "LIVE_WEB_SEARCH_FETCH_FULL_PAGES", False):
            telemetry["skipped_full_page_fetch_reason"] = "full_page_fetch_disabled"
            return {}, [], [], telemetry

        if sufficiency.sufficient:
            telemetry["snippet_only"] = True
            telemetry["skipped_full_page_fetch_reason"] = sufficiency.reason
            return {}, [], [], telemetry

        fetch_limit = min(
            int(getattr(settings, "LIVE_WEB_FETCH_MAX_PAGES", max_results) or max_results),
            max_results,
            len(hits),
        )
        hits_to_fetch = await self._select_hits_for_extraction(
            search_query,
            hits,
            limit=fetch_limit,
        )

        prior = prior_web_urls or set()
        selected: list[WebSearchHit] = []
        seen: set[str] = set()
        for hit in hits_to_fetch:
            if hit.url in seen:
                telemetry["duplicate_url_same_turn_count"] += 1
                continue
            seen.add(hit.url)
            if hit.url in prior:
                telemetry["conversation_url_dedupe_count"] += 1
                telemetry["skipped_fetch_existing_url_count"] += 1
                continue
            selected.append(hit)

        telemetry["selected_full_page_urls"] = [hit.url for hit in selected]
        if hits_to_fetch and not selected:
            telemetry["skipped_full_page_fetch_reason"] = "all_selected_urls_seen_before"

        fetched, fetch_stats = await self._fetch_pages_with_stats(selected)
        attempted_stats = [item for item in fetch_stats if item.get("method") != "skipped"]
        attempts = len(attempted_stats)
        successes = sum(1 for item in attempted_stats if item.get("chars", 0) > 0)
        obscura_attempts = sum(1 for item in attempted_stats if item.get("obscura_attempted"))
        obscura_successes = sum(1 for item in attempted_stats if item.get("js_rendered"))
        telemetry["redis_page_cache_hit"] = any(
            item.get("from_cache") and item.get("cache_layer") == "redis"
            for item in fetch_stats
        )
        telemetry["redis_page_cache_hit_count"] = sum(
            1
            for item in fetch_stats
            if item.get("from_cache") and item.get("cache_layer") == "redis"
        )
        telemetry["avg_pages_fetched"] = round(successes / attempts, 3) if attempts else 0.0
        telemetry["obscura_attempt_rate"] = (
            round(obscura_attempts / attempts, 3) if attempts else 0.0
        )
        telemetry["obscura_success_rate"] = (
            round(obscura_successes / obscura_attempts, 3)
            if obscura_attempts
            else 0.0
        )
        return fetched, fetch_stats, selected, telemetry

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
            time_range = infer_web_search_time_range(search_query)
            hits = await self._search_searxng_pool(
                search_query,
                max_results=candidate_limit,
                time_range=time_range,
            )
        except Exception as exc:
            logger.info("live web search skipped: %s", exc)
            return []
        if not hits:
            return []

        fetched, fetch_stats, _hits_to_fetch, pipeline = await self._fetch_pages_for_search(
            search_query=search_query,
            hits=hits,
            max_results=int(settings.LIVE_WEB_SEARCH_MAX_RESULTS or _DEFAULT_MAX_RESULTS),
        )
        fetch_stats_by_url = {str(item.get("url")): item for item in fetch_stats}
        candidate_chunks = web_hits_to_source_chunks(
            hits,
            fetched_markdown=fetched,
            fetch_stats_by_url=fetch_stats_by_url,
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
            "live web search reranked %d candidate(s) to %d result(s) for query=%r search_query=%r corpus_sources=%d snippet_only=%s",
            len(hits),
            len(selected),
            query[:120],
            search_query[:240],
            len(corpus_sources or []),
            pipeline.get("snippet_only"),
        )
        return selected

    async def _search_searxng_pool(
        self,
        query: str,
        *,
        max_results: int | None = None,
        time_range: str | None = None,
    ) -> list[WebSearchHit]:
        candidate_limit = max(1, int(max_results or _DEFAULT_CANDIDATE_RESULTS))
        queries = build_web_search_queries(query)
        if not queries:
            return []

        if len(queries) == 1:
            result = await self._search_searxng(
                queries[0],
                max_results=candidate_limit,
                time_range=time_range,
            )
            return [
                hit
                for hit in result
                if not _is_low_quality_web_hit_for_query(hit, query)
            ]

        per_query_limit = min(_WEB_SEARCH_RESULTS_PER_QUERY, candidate_limit)
        tasks = []
        for variant in queries:
            variant_time_range = (
                None
                if variant.lower().startswith(("!hf", "!hfm", "!nvd"))
                else time_range
            )
            tasks.append(
                self._search_searxng(
                    variant,
                    max_results=per_query_limit,
                    time_range=variant_time_range,
                )
            )
        results = await asyncio.gather(*tasks, return_exceptions=True)
        hit_lists: list[list[WebSearchHit]] = []
        for variant, result in zip(queries, results):
            if not isinstance(result, list):
                continue
            allowed_domains = _variant_allowed_domains(variant)
            result = [
                hit
                for hit in result
                if not _is_low_quality_web_hit_for_query(hit, query)
            ]
            if allowed_domains:
                result = [
                    hit
                    for hit in result
                    if any(
                        _web_domain(hit.url) == domain
                        or _web_domain(hit.url).endswith(f".{domain}")
                        for domain in allowed_domains
                    )
                ]
            hit_lists.append(result)
        if not hit_lists:
            return []

        base_hits = hit_lists[0]
        social_hit_lists = hit_lists[1:]
        seen: set[str] = set()
        merged: list[WebSearchHit] = []

        def add_hits(items: list[WebSearchHit], limit: int | None = None) -> None:
            for hit in items[:limit]:
                if not hit.url or hit.url in seen:
                    continue
                seen.add(hit.url)
                merged.append(hit)

        reserved_variant_slots = sum(1 for hits in social_hit_lists if hits)
        base_primary_limit = min(
            _WEB_SEARCH_RESULTS_PER_QUERY,
            max(1, candidate_limit - reserved_variant_slots),
        )
        add_hits(base_hits, base_primary_limit)
        for social_hits in social_hit_lists:
            add_hits(social_hits, per_query_limit)
        for hit_list in hit_lists:
            if len(merged) >= candidate_limit:
                break
            add_hits(hit_list)

        return merged[:candidate_limit]

    async def _search_searxng(
        self,
        query: str,
        *,
        max_results: int | None = None,
        time_range: str | None = None,
    ) -> list[WebSearchHit]:
        settings = get_settings()
        base_url = settings.SEARXNG_URL.rstrip("/")
        params = {
            "q": query,
            "format": "json",
            "language": "en",
            "safesearch": "1",
        }
        if settings.SEARXNG_ENGINES and not query.lstrip().startswith("!"):
            params["engines"] = settings.SEARXNG_ENGINES
        if time_range:
            params["time_range"] = time_range
        candidate_limit = max_results
        if candidate_limit is None:
            candidate_limit = getattr(
                settings,
                "LIVE_WEB_SEARCH_CANDIDATE_RESULTS",
                _DEFAULT_CANDIDATE_RESULTS,
            )
        candidate_limit = int(candidate_limit or _DEFAULT_CANDIDATE_RESULTS)
        cache_key = _search_cache_key(
            query,
            engines=params.get("engines"),
            time_range=time_range,
            candidate_limit=candidate_limit,
        )
        cached_payload = await web_cache.get_json(cache_key)
        if cached_payload and cached_payload.get("schema_version") == _WEB_CACHE_SCHEMA_VERSION:
            cached_hits = [
                hit
                for raw in (cached_payload.get("hits") or [])
                if isinstance(raw, dict)
                if (hit := _deserialize_hit(raw, from_cache=True)) is not None
            ]
            if cached_hits:
                return cached_hits[:candidate_limit]

        timeout = httpx.Timeout(settings.SEARXNG_TIMEOUT_SECONDS, connect=2.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(f"{base_url}/search", params=params)
            response.raise_for_status()
            payload = response.json()
        hits = parse_searxng_results(
            payload,
            candidate_limit,
            search_query=query,
            time_range=time_range,
        )
        await web_cache.set_json(
            cache_key,
            {
                "schema_version": _WEB_CACHE_SCHEMA_VERSION,
                "hits": [_serialize_hit(hit) for hit in hits],
            },
            ttl_seconds=_WEB_SEARCH_CACHE_TTL_SECONDS,
        )
        return hits

    async def _select_hits_for_extraction(
        self,
        query: str,
        hits: list[WebSearchHit],
        *,
        limit: int,
    ) -> list[WebSearchHit]:
        if not hits or limit <= 0:
            return []
        filtered_hits = [
            hit
            for hit in hits
            if not _is_low_quality_web_hit_for_query(hit, query)
        ]
        if filtered_hits:
            hits = filtered_hits
        if len(hits) <= limit:
            return hits
        snippet_chunks = web_hits_to_source_chunks(
            hits,
            search_query=query,
            max_chars=1200,
        )
        ranked = await rerank_web_source_chunks(
            query,
            snippet_chunks,
            limit=limit,
        )
        by_url = {hit.url: hit for hit in hits}
        selected = [
            by_url[chunk.metadata["url"]]
            for chunk in ranked
            if (chunk.metadata or {}).get("url") in by_url
        ]
        if len(selected) >= limit:
            return selected[:limit]
        seen = {hit.url for hit in selected}
        selected.extend(hit for hit in hits if hit.url not in seen)
        return selected[:limit]

    async def _fetch_pages(
        self,
        hits: list[WebSearchHit],
    ) -> dict[str, str]:
        fetched, _stats = await self._fetch_pages_with_stats(hits)
        return fetched

    async def _fetch_pages_with_stats(
        self,
        hits: list[WebSearchHit],
    ) -> tuple[dict[str, str], list[dict[str, Any]]]:
        unique_hits: list[WebSearchHit] = []
        duplicate_stats: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for hit in hits:
            if hit.url in seen_urls:
                duplicate_stats.append(
                    {
                        "url": hit.url,
                        "domain": _web_domain(hit.url),
                        "method": "skipped",
                        "status": "duplicate_url_same_turn",
                        "chars": 0,
                        "from_cache": False,
                        "cache_layer": None,
                        "obscura_attempted": False,
                        "js_rendered": False,
                    }
                )
                continue
            seen_urls.add(hit.url)
            unique_hits.append(hit)

        by_domain: dict[str, list[WebSearchHit]] = {}
        for hit in unique_hits:
            by_domain.setdefault(_web_domain(hit.url), []).append(hit)

        async def fetch_domain_group(group: list[WebSearchHit]) -> list[tuple[WebSearchHit, Any]]:
            group_results: list[tuple[WebSearchHit, Any]] = []
            for hit in group:
                try:
                    result = await self._fetch_one_page_with_stats(hit.url)
                except Exception as exc:
                    result = exc
                group_results.append((hit, result))
            return group_results

        grouped = await asyncio.gather(
            *(fetch_domain_group(group) for group in by_domain.values()),
            return_exceptions=True,
        )
        fetched: dict[str, str] = {}
        stats: list[dict[str, Any]] = list(duplicate_stats)
        pairs: list[tuple[WebSearchHit, Any]] = []
        for item in grouped:
            if isinstance(item, list):
                pairs.extend(item)
            else:
                stats.append(
                    {
                        "url": "",
                        "domain": "",
                        "method": "error",
                        "status": str(item)[:180],
                        "chars": 0,
                        "from_cache": False,
                        "cache_layer": None,
                        "obscura_attempted": False,
                        "js_rendered": False,
                    }
                )
        for hit, result in pairs:
            if isinstance(result, _PageFetchResult):
                if result.text and result.text.strip():
                    fetched[hit.url] = result.text.strip()
                stats.append(
                    {
                        "url": hit.url,
                        "domain": _web_domain(hit.url),
                        "method": result.method,
                        "status": result.status,
                        "chars": result.chars,
                        "from_cache": result.from_cache,
                        "cache_layer": result.cache_layer,
                        "obscura_attempted": result.obscura_attempted,
                        "js_rendered": result.js_rendered,
                    }
                )
                continue
            stats.append(
                {
                    "url": hit.url,
                    "domain": _web_domain(hit.url),
                    "method": "error",
                    "status": str(result)[:180],
                    "chars": 0,
                    "from_cache": False,
                    "cache_layer": None,
                    "obscura_attempted": False,
                    "js_rendered": False,
                }
            )
        return fetched, stats

    async def _fetch_one_page(self, url: str) -> str | None:
        result = await self._fetch_one_page_with_stats(url)
        return result.text

    async def _fetch_one_page_with_stats(self, url: str) -> _PageFetchResult:
        settings = get_settings()
        fetcher = str(getattr(settings, "LIVE_WEB_PAGE_FETCHER", "auto") or "auto")
        max_chars = int(settings.OBSCURA_MAX_CHARS or _DEFAULT_OBSCURA_MAX_CHARS)
        cache_key = _page_fetch_cache_key(
            url,
            fetcher=fetcher,
            max_chars=max_chars,
            obscura_domains=str(getattr(settings, "LIVE_WEB_OBSCURA_DOMAINS", "") or ""),
        )
        cached, cache_layer = await _get_page_fetch_cache_async(cache_key)
        if cached:
            return _PageFetchResult(
                url=url,
                text=cached,
                method="cache",
                status="cache_hit",
                chars=len(cached),
                from_cache=True,
                cache_layer=cache_layer,
            )

        text = None
        method = "failed"
        if _is_video_domain(_web_domain(url)):
            text = await self._fetch_one_with_ytdlp(url)
            method = "yt_dlp" if text else "failed"
        if not text:
            text = await self._fetch_one_with_raw_adapter(url)
            method = "raw_adapter"
        obscura_attempted = False
        if not text:
            text = await self._fetch_one_with_httpx(url)
            method = "static_http"
        if not text and self._should_try_obscura(url):
            obscura_attempted = True
            text = await self._fetch_one_with_obscura(url)
            method = "obscura_js" if text else "failed"
        if text:
            text = text[:max_chars]
            ttl_seconds = int(
                getattr(settings, "LIVE_WEB_FETCH_CACHE_TTL_SECONDS", 900) or 0
            )
            await _put_page_fetch_cache_async(
                cache_key,
                text,
                ttl_seconds=ttl_seconds,
            )
            return _PageFetchResult(
                url=url,
                text=text,
                method=method,
                status="ok",
                chars=len(text),
                obscura_attempted=obscura_attempted,
                js_rendered=method == "obscura_js",
            )
        return _PageFetchResult(
            url=url,
            text=None,
            method="failed",
            status=(
                "obscura_failed_or_empty"
                if obscura_attempted
                else "no_extractable_text"
            ),
            obscura_attempted=obscura_attempted,
        )

    def _should_try_obscura(self, url: str) -> bool:
        settings = get_settings()
        if not settings.OBSCURA_COMMAND:
            return False
        allowed = _parse_domain_list(
            getattr(settings, "LIVE_WEB_OBSCURA_DOMAINS", None)
        )
        return bool(allowed and _domain_matches(_web_domain(url), allowed))

    async def _fetch_one_with_raw_adapter(self, url: str) -> str | None:
        for raw_url in _raw_source_candidate_urls(url):
            text = await self._fetch_raw_text_url(raw_url)
            if text:
                return text
        return None

    async def _fetch_raw_text_url(self, url: str) -> str | None:
        settings = get_settings()
        timeout_seconds = min(float(settings.OBSCURA_TIMEOUT_SECONDS or 10.0), 6.0)
        timeout = httpx.Timeout(timeout_seconds, connect=2.0)
        headers = {"User-Agent": _WEB_FETCH_USER_AGENT}
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers=headers,
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
        except Exception as exc:
            logger.debug("Raw source fetch failed for %s: %s", url, exc)
            return None

        text = re.sub(r"\s+", " ", response.text).strip()
        if len(text) < 80:
            return None
        max_chars = int(settings.OBSCURA_MAX_CHARS or _DEFAULT_OBSCURA_MAX_CHARS)
        return text[:max_chars]

    async def _fetch_one_with_httpx(self, url: str) -> str | None:
        settings = get_settings()
        timeout_seconds = min(float(settings.OBSCURA_TIMEOUT_SECONDS or 10.0), 8.0)
        timeout = httpx.Timeout(timeout_seconds, connect=2.0)
        headers = {"User-Agent": _WEB_FETCH_USER_AGENT}
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers=headers,
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
        except Exception as exc:
            logger.debug("HTTP page fetch failed for %s: %s", url, exc)
            return None

        content_type = response.headers.get("content-type", "").lower()
        if (
            content_type
            and "text/html" not in content_type
            and "text/plain" not in content_type
            and "application/xhtml" not in content_type
        ):
            return None
        if "text/plain" in content_type:
            text = re.sub(r"\s+", " ", response.text).strip()
            return text[: settings.OBSCURA_MAX_CHARS] if len(text) >= 200 else None
        fetcher = str(getattr(settings, "LIVE_WEB_PAGE_FETCHER", "auto") or "auto")
        return _extract_static_page_text(
            response.text,
            url=url,
            max_chars=int(settings.OBSCURA_MAX_CHARS or _DEFAULT_OBSCURA_MAX_CHARS),
            fetcher=fetcher.strip().lower(),
        )

    async def _fetch_one_with_obscura(self, url: str) -> str | None:
        settings = get_settings()
        args = _obscura_command_args(
            settings.OBSCURA_COMMAND,
            url=url,
            timeout_seconds=float(settings.OBSCURA_TIMEOUT_SECONDS or 10.0),
        )
        if not args:
            return None
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
            stderr = _stderr.decode("utf-8", errors="replace").strip()
            logger.debug("Obscura fetch exited %s for %s: %s", proc.returncode, url, stderr[:300])
            return None
        text = stdout.decode("utf-8", errors="replace").strip()
        return text[: settings.OBSCURA_MAX_CHARS] if text else None

    async def _fetch_one_with_ytdlp(self, url: str) -> str | None:
        """Extract useful YouTube metadata/transcripts with yt-dlp when available."""
        settings = get_settings()
        max_chars = int(settings.OBSCURA_MAX_CHARS or _DEFAULT_OBSCURA_MAX_CHARS)
        timeout_seconds = min(float(settings.OBSCURA_TIMEOUT_SECONDS or 10.0), 12.0)
        try:
            import yt_dlp  # type: ignore
        except Exception as exc:
            logger.debug("yt-dlp unavailable for %s: %s", url, exc)
            return None

        def extract_info() -> dict[str, Any] | None:
            options = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "noplaylist": True,
                "socket_timeout": timeout_seconds,
                "extract_flat": False,
            }
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
                return info if isinstance(info, dict) else None

        try:
            info = await asyncio.wait_for(
                asyncio.to_thread(extract_info),
                timeout=timeout_seconds + 3,
            )
        except Exception as exc:
            logger.debug("yt-dlp extract failed for %s: %s", url, exc)
            return None
        if not info:
            return None

        title = str(info.get("title") or "").strip()
        channel = str(info.get("channel") or info.get("uploader") or "").strip()
        upload_date = str(info.get("upload_date") or "").strip()
        duration = info.get("duration")
        description = re.sub(r"\s+", " ", str(info.get("description") or "")).strip()
        caption = await self._fetch_ytdlp_caption_text(info, timeout_seconds=timeout_seconds)

        parts = [
            f"Title: {title}" if title else "",
            f"Channel: {channel}" if channel else "",
            f"Upload date: {upload_date}" if upload_date else "",
            f"Duration seconds: {duration}" if duration else "",
            f"Description: {description[:1200]}" if description else "",
            f"Transcript: {caption}" if caption else "",
        ]
        text = "\n".join(part for part in parts if part).strip()
        return text[:max_chars] if len(text) >= 120 else None

    async def _fetch_ytdlp_caption_text(
        self,
        info: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> str:
        track = self._select_ytdlp_caption_track(info)
        if not track:
            return ""
        url = str(track.get("url") or "")
        if not _valid_web_url(url):
            return ""
        ext = str(track.get("ext") or "").lower()
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout_seconds, connect=2.0),
                follow_redirects=True,
                headers={"User-Agent": _WEB_FETCH_USER_AGENT},
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
        except Exception as exc:
            logger.debug("yt-dlp caption fetch failed: %s", exc)
            return ""
        raw = response.text
        if ext == "json3" or raw.lstrip().startswith("{"):
            return _extract_json3_caption_text(raw)[:2500]
        return _extract_vtt_caption_text(raw)[:2500]

    def _select_ytdlp_caption_track(self, info: dict[str, Any]) -> dict[str, Any] | None:
        for group_name in ("subtitles", "automatic_captions"):
            group = info.get(group_name)
            if not isinstance(group, dict):
                continue
            language_keys = [
                key
                for key in group
                if str(key).lower() in {"en", "en-us", "en-gb"}
            ] or [key for key in group if str(key).lower().startswith("en")]
            for language in language_keys:
                tracks = group.get(language)
                if not isinstance(tracks, list):
                    continue
                for preferred_ext in ("json3", "vtt", "srv3", "ttml"):
                    for track in tracks:
                        if (
                            isinstance(track, dict)
                            and str(track.get("ext") or "").lower() == preferred_ext
                            and track.get("url")
                        ):
                            return track
        return None


live_web_search = LiveWebSearch()
