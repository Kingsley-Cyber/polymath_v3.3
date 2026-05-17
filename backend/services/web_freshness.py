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

logger = logging.getLogger(__name__)

_DEFAULT_MAX_RESULTS = 6
_DEFAULT_CANDIDATE_RESULTS = 15
_WEB_SEARCH_RESULTS_PER_QUERY = 5
_DEFAULT_OBSCURA_MAX_CHARS = 4000
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

_RESEARCH_QUERY_MARKERS = (
    "academic",
    "arxiv",
    "benchmark",
    "evaluation",
    "literature",
    "paper",
    "papers",
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
    obscura_attempted: bool = False
    js_rendered: bool = False


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
    return bool(
        _contains_marker(text, _VIDEO_QUERY_MARKERS)
        or _looks_like_roblox_query(query)
        or _looks_like_ai_media_query(query)
        or _looks_like_creator_economy_query(query)
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


def _page_fetch_cache_key(url: str, *, fetcher: str, max_chars: int) -> str:
    domain = _web_domain(url)
    return sha1(f"{url}|{domain}|{fetcher}|{max_chars}".encode("utf-8")).hexdigest()


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
        hit_query = hit.search_query or search_query or ""
        text = (
            f"Live web result fetched_at={now}\n"
            f"Title: {hit.title}\n"
            f"URL: {hit.url}{published}\n"
            f"Engines: {engines}\n"
            f"Search query: {hit_query}\n"
            f"Freshness filter: {hit.time_range or 'none'}\n"
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
    return _diversify_web_source_chunks(query, ranked, limit=limit)


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

        fetched: dict[str, str] = {}
        if settings.LIVE_WEB_SEARCH_FETCH_FULL_PAGES:
            fetch_limit = min(
                int(
                    getattr(settings, "LIVE_WEB_FETCH_MAX_PAGES", _DEFAULT_FETCH_MAX_PAGES)
                    or _DEFAULT_FETCH_MAX_PAGES
                ),
                int(settings.LIVE_WEB_SEARCH_MAX_RESULTS or _DEFAULT_MAX_RESULTS),
                len(hits),
            )
            hits_to_fetch = await self._select_hits_for_extraction(
                search_query,
                hits,
                limit=fetch_limit,
            )
            fetched = await self._fetch_pages(hits_to_fetch)
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
            return await self._search_searxng(
                queries[0],
                max_results=candidate_limit,
                time_range=time_range,
            )

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
                if not _is_low_quality_web_hit(hit)
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
            search_query=query,
            time_range=time_range,
        )

    async def _select_hits_for_extraction(
        self,
        query: str,
        hits: list[WebSearchHit],
        *,
        limit: int,
    ) -> list[WebSearchHit]:
        if not hits or limit <= 0:
            return []
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
        tasks = [self._fetch_one_page_with_stats(hit.url) for hit in hits]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        fetched: dict[str, str] = {}
        stats: list[dict[str, Any]] = []
        for hit, result in zip(hits, results):
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
        )
        cached = _get_page_fetch_cache(cache_key)
        if cached:
            return _PageFetchResult(
                url=url,
                text=cached,
                method="cache",
                status="cache_hit",
                chars=len(cached),
                from_cache=True,
            )

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
            _put_page_fetch_cache(
                cache_key,
                text,
                ttl_seconds=int(
                    getattr(settings, "LIVE_WEB_FETCH_CACHE_TTL_SECONDS", 900) or 0
                ),
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
            status="no_extractable_text",
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
