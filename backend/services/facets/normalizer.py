"""Deterministic facet schema builder.

The goal is not to answer queries here. This module gives every ingested
document and chunk a stable semantic handle, so later retrieval can match
"On Device LLM", "on_device_llm", and "on-device llm architecture" without
needing hand-written one-off lanes.
"""

from __future__ import annotations

from collections import Counter
import os
import re
from typing import Any

FACET_SCHEMA_VERSION = "polymath.facets.v1"

_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+./'-]*")
_EXT_RE = re.compile(r"(\.(md|markdown|txt|pdf|docx?|pptx?|html|json|csv|epub))+$", re.I)
_GENERIC_SUFFIXES = {
    "copy",
    "guide",
    "notes",
    "paper",
    "pdf",
    "md",
    "markdown",
    "report",
    "article",
}
_STOPWORDS = {
    "and",
    "are",
    "for",
    "from",
    "into",
    "the",
    "that",
    "this",
    "with",
    "your",
    "their",
    "over",
    "new",
    "using",
    "based",
}
_PROTECTED_ACRONYMS = {
    "ai": "AI",
    "api": "API",
    "apis": "APIs",
    "aws": "AWS",
    "bm25": "BM25",
    "cpu": "CPU",
    "gpu": "GPU",
    "html": "HTML",
    "json": "JSON",
    "kg": "KG",
    "llm": "LLM",
    "llms": "LLMs",
    "nlp": "NLP",
    "neo4j": "Neo4j",
    "pdf": "PDF",
    "prd": "PRD",
    "qdrant": "Qdrant",
    "rag": "RAG",
    "rdf": "RDF",
    "ui": "UI",
    "ux": "UX",
    "xml": "XML",
}

_CONTENT_FACET_LIMIT = 6
_CONTENT_GENERIC_TERMS = {
    "abstract",
    "appendix",
    "article",
    "background",
    "bibliography",
    "bmtitle",
    "chapter",
    "child",
    "cite",
    "concept",
    "conclusion",
    "contents",
    "content",
    "description",
    "document",
    "doi",
    "example",
    "figure",
    "guide",
    "html",
    "information",
    "introduction",
    "isbn",
    "method",
    "paper",
    "paragraph",
    "references",
    "section",
    "summary",
    "table",
    "text",
    "theory",
    "xhtml",
}
_CONTENT_NOISY_PHRASE_TERMS = {
    "bib",
    "biblioentry",
    "bibliography",
    "bmtitle",
    "cite",
    "contents",
    "doi",
    "footnote",
    "head",
    "head1a",
    "html",
    "isbn",
    "letters",
    "monographs",
    "page",
    "psycholog",
    "ref",
    "refs",
    "references",
    "toc",
    "theta",
    "sim",
    "whole",
    "www",
    "xhtml",
}
_CONTENT_STOPWORDS = _STOPWORDS | {
    "all",
    "been",
    "can",
    "does",
    "had",
    "has",
    "how",
    "may",
    "not",
    "one",
    "our",
    "out",
    "should",
    "such",
    "than",
    "then",
    "these",
    "they",
    "those",
    "was",
    "were",
    "when",
    "which",
    "will",
    "within",
    "without",
}

# Broad, reusable retrieval concepts. These are not query patches; they are
# stable aliases that let different phrasings land on the same metadata signal.
_CONTENT_FACET_ALIASES: dict[str, tuple[str, ...]] = {
    "knowledge_graph": (
        "knowledge graph",
        "knowledge graphs",
        "ontology",
        "ontologies",
        "rdf",
        "linked data",
        "semantic network",
        "semantic graph",
        "graph database",
        "nodes and edges",
        "triples",
    ),
    "user_modeling": (
        "user model",
        "user models",
        "user modeling",
        "user modelling",
        "user profile",
        "user profiling",
        "adaptive user",
        "personalization",
        "personalisation",
        "preference model",
        "learner model",
        "student model",
    ),
    "psychometrics": (
        "psychometric",
        "psychometrics",
        "psychological measurement",
        "latent variable",
        "validity",
        "reliability",
        "assessment design",
        "evidence centered assessment",
        "score interpretation",
    ),
    "identity_narrative": (
        "narrative identity",
        "self story",
        "self-story",
        "autobiographical memory",
        "personal myth",
        "identity construction",
        "life story",
        "narrative construction",
    ),
    "neuro_narrative": (
        "neuro narrative",
        "neuro-narrative",
        "narrative therapy",
        "narrative reconstruction",
        "therapeutic narrative",
        "trauma narrative",
        "meaning reconstruction",
    ),
    "agency_preservation": (
        "agency",
        "autonomy",
        "choice",
        "authorship",
        "self determination",
        "self-determination",
        "values",
        "value alignment",
        "personal meaning",
        "control",
    ),
    "emotional_patterns": (
        "emotion",
        "emotional",
        "affect",
        "affective",
        "mood",
        "feeling",
        "sentiment",
        "stress",
        "anxiety",
    ),
    "socialization": (
        "socialization",
        "socialisation",
        "secondary socialization",
        "primary socialization",
        "institutional order",
        "sub-world",
        "sub worlds",
        "significant others",
        "professional world",
    ),
    "scenario_assessment": (
        "scenario",
        "scenarios",
        "situational judgment",
        "choice points",
        "interactive assessment",
        "game based assessment",
        "game-based assessment",
        "simulation",
    ),
    "interpersonal_perception": (
        "interpersonal perception",
        "person perception",
        "perceiving others",
        "social perception",
        "impression formation",
        "attribution",
        "theory of mind",
    ),
    "cooperative_personality": (
        "cooperative personality",
        "cooperative personalities",
        "cooperation",
        "collaboration",
        "team roles",
        "multi agent",
        "multi-agent",
        "agent personality",
    ),
    "tree_of_thoughts": (
        "tree of thoughts",
        "tree-of-thoughts",
        "deliberate problem solving",
        "search tree",
        "reasoning paths",
        "lookahead",
    ),
    "on_device_llm": (
        "on device llm",
        "on-device llm",
        "local llm",
        "small language model",
        "edge inference",
        "private inference",
        "offline model",
    ),
    "retrieval_augmented_generation": (
        "retrieval augmented generation",
        "retrieval-augmented generation",
        "rag",
        "graph rag",
        "rag pipeline",
        "retrieval pipeline",
    ),
    "vector_database": (
        "vector database",
        "vector db",
        "embedding",
        "embeddings",
        "cosine similarity",
        "semantic search",
        "qdrant",
    ),
    "reranking": (
        "rerank",
        "reranker",
        "reranking",
        "cross encoder",
        "cross-encoder",
        "ranked candidates",
    ),
    "graph_database": (
        "graph database",
        "neo4j",
        "cypher",
        "graph traversal",
        "node relationship",
        "nodes relationships",
    ),
    "privacy": (
        "privacy",
        "private",
        "on-device",
        "local first",
        "local-first",
        "data minimization",
        "consent",
    ),
    "code_architecture": (
        "api",
        "backend",
        "frontend",
        "fastapi",
        "react",
        "service layer",
        "architecture",
        "docker",
    ),
}


def _ascii_fold(value: Any) -> str:
    text = str(value or "")
    text = text.replace("’", "'").replace("–", "-").replace("—", "-")
    return text


def _strip_extensions(value: str) -> str:
    text = _ascii_fold(value)
    text = os.path.basename(text)
    for _ in range(3):
        new_text = _EXT_RE.sub("", text)
        if new_text == text:
            break
        text = new_text
    return text


def _split_words(value: Any) -> list[str]:
    text = _strip_extensions(_ascii_fold(value))
    text = re.sub(r"[_:/\\|]+", " ", text)
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    words = [_normalize_word(match.group(0)) for match in _WORD_RE.finditer(text)]
    return [word for word in words if word]


def _normalize_word(value: str) -> str:
    word = str(value or "").strip("'\"().,;:[]{}").lower()
    word = re.sub(r"[^a-z0-9+.-]+", "", word)
    return word


def _compact_label(value: Any, *, max_words: int = 8) -> str:
    words = [
        word
        for word in _split_words(value)
        if word not in _STOPWORDS and len(word) > 1
    ]
    while words and words[-1] in _GENERIC_SUFFIXES:
        words.pop()
    return " ".join(words[:max_words]).strip()


def normalize_facet_id(value: Any) -> str:
    """Return the machine key for a facet: lowercase snake_case ASCII."""

    words = [
        word
        for word in _split_words(value)
        if word not in _STOPWORDS and word not in {"a", "an"}
    ]
    while words and words[-1] in _GENERIC_SUFFIXES:
        words.pop()
    text = "_".join(words)
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_")


def canonical_display_name(value: Any) -> str:
    """Return a stable human label while preserving common technical acronyms."""

    words = _split_words(value)
    display: list[str] = []
    for word in words:
        if word in _PROTECTED_ACRONYMS:
            display.append(_PROTECTED_ACRONYMS[word])
        elif word in {"on", "device"} and display and display[-1] == "On":
            display.append(word.capitalize())
        elif word:
            display.append(word.capitalize())
    text = " ".join(display)
    text = text.replace("On-device", "On-Device")
    text = text.replace("On Device", "On-Device")
    text = text.replace("Evidence-centered", "Evidence-Centered")
    text = text.replace("Evidence Centered", "Evidence-Centered")
    return text.strip()


def _alias_values(label: str, raw_values: list[Any]) -> list[str]:
    aliases: list[str] = []
    seen: set[str] = set()
    for raw in [label, *raw_values]:
        compact = _compact_label(raw, max_words=10)
        for candidate in {compact, normalize_facet_id(raw).replace("_", " ")}:
            candidate = " ".join(str(candidate or "").lower().split())
            if candidate and candidate not in seen:
                seen.add(candidate)
                aliases.append(candidate)
    return aliases[:10]


def _facet_record(
    label: Any,
    *,
    source_level: str,
    source_refs: list[dict[str, Any]],
    raw_values: list[Any] | None = None,
    confidence: float = 0.7,
) -> dict[str, Any] | None:
    compact = _compact_label(label)
    facet_id = normalize_facet_id(compact or label)
    if not facet_id or len(facet_id) < 3:
        return None
    display = canonical_display_name(compact or label)
    raw_values = raw_values or [label]
    aliases = _alias_values(compact or str(label), raw_values)
    return {
        "facet_id": facet_id,
        "display_name": display,
        "aliases": aliases,
        "search_terms": aliases[:6],
        "source_level": source_level,
        "source_refs": source_refs[:6],
        "confidence": round(max(0.0, min(float(confidence), 1.0)), 3),
    }


def _schema_lens_facets(schema_lens: dict[str, Any] | None, doc_ref: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(schema_lens, dict):
        return []
    rows: list[dict[str, Any]] = []
    for field, level, conf in (
        ("corpus_domains", "schema_lens_domain", 0.68),
        ("canonical_families", "schema_lens_family", 0.72),
        ("object_kinds", "schema_lens_object_kind", 0.58),
    ):
        for value in (schema_lens.get(field) or [])[:10]:
            label = str(value or "").replace("_", " ")
            row = _facet_record(
                label,
                source_level=level,
                source_refs=[doc_ref],
                raw_values=[value, label],
                confidence=conf,
            )
            if row:
                rows.append(row)
    return rows


def _heading_facets(chunks: list[Any], doc_ref: dict[str, Any]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    raw_by_key: dict[str, Any] = {}
    for chunk in chunks:
        for heading in (getattr(chunk, "heading_path", None) or [])[:2]:
            label = _compact_label(heading, max_words=8)
            fid = normalize_facet_id(label)
            if not fid or fid in raw_by_key:
                continue
            raw_by_key[fid] = heading
            counts[fid] += 1
    rows: list[dict[str, Any]] = []
    for fid, _count in counts.most_common(8):
        raw = raw_by_key[fid]
        row = _facet_record(
            raw,
            source_level="heading",
            source_refs=[doc_ref],
            raw_values=[raw],
            confidence=0.64,
        )
        if row:
            rows.append(row)
    return rows


def _dedupe_facets(rows: list[dict[str, Any]], *, limit: int = 16) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        fid = str(row.get("facet_id") or "")
        if not fid:
            continue
        existing = by_id.get(fid)
        if existing is None or float(row.get("confidence", 0)) > float(existing.get("confidence", 0)):
            by_id[fid] = row
    return sorted(
        by_id.values(),
        key=lambda row: (-float(row.get("confidence", 0)), str(row.get("facet_id"))),
    )[:limit]


def _match_haystack(value: Any) -> str:
    text = _ascii_fold(value).lower()
    text = re.sub(r"[^a-z0-9+.-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _text_words(value: Any) -> list[str]:
    text = re.sub(r"[._:/\\|#]+", " ", _ascii_fold(value))
    words = [_normalize_word(match.group(0)) for match in re.finditer(r"[A-Za-z0-9][A-Za-z0-9+'-]*", text)]
    return [
        word
        for word in words
        if word
        and len(word) > 2
        and word not in _CONTENT_STOPWORDS
        and word not in _CONTENT_GENERIC_TERMS
        and word not in _CONTENT_NOISY_PHRASE_TERMS
        and not any(ch.isdigit() for ch in word)
    ]


def _valid_content_phrase(words: tuple[str, ...]) -> bool:
    if not words or all(word in _CONTENT_GENERIC_TERMS for word in words):
        return False
    if any(word in _CONTENT_NOISY_PHRASE_TERMS for word in words):
        return False
    if any(word.startswith(("http", "www")) for word in words):
        return False
    if len(set(words)) < min(2, len(words)):
        return False
    if all(len(word) < 5 for word in words):
        return False
    phrase_id = normalize_facet_id(" ".join(words))
    if not phrase_id or len(phrase_id) < 6:
        return False
    if phrase_id in _CONTENT_GENERIC_TERMS:
        return False
    return True


def _content_phrase_facets(text: str, *, limit: int) -> list[tuple[str, float]]:
    words = _text_words(text)
    if len(words) < 2:
        return []
    counts: Counter[tuple[str, ...]] = Counter()
    first_pos: dict[tuple[str, ...], int] = {}
    max_words = min(len(words), 900)
    for n in (2, 3):
        for i in range(0, max_words - n + 1):
            window = tuple(words[i : i + n])
            if not _valid_content_phrase(window):
                continue
            counts[window] += 1
            first_pos.setdefault(window, i)
    scored: list[tuple[float, str]] = []
    for window, count in counts.items():
        # Repeated phrases are stronger; early singletons can still help for
        # headings/abstracts where the most important phrase appears once.
        if count < 2 and first_pos.get(window, 999) > 80:
            continue
        fid = normalize_facet_id(" ".join(window))
        if not fid:
            continue
        early = max(0.0, 1.0 - (first_pos.get(window, 999) / 120.0))
        score = (count * len(window)) + early
        scored.append((score, fid))
    scored.sort(key=lambda item: (-item[0], item[1]))
    out: list[tuple[str, float]] = []
    seen: set[str] = set()
    for _score, fid in scored:
        if fid in seen:
            continue
        if fid in _CONTENT_GENERIC_TERMS:
            continue
        seen.add(fid)
        out.append((fid, 0.62))
        if len(out) >= limit:
            break
    return out


def _specific_content_alias(facet_id: str, alias: str) -> bool:
    """Reject broad one-word aliases that stamp unrelated document metadata.

    Multiword phrases carry enough context for deterministic matching. A
    single word is accepted only when it is the canonical facet name or a
    protected technical acronym. Corpus-derived phrase mining remains the
    general fallback, so this gate removes query-shaped trigger pollution
    without introducing another hand-maintained denylist.
    """

    words = _split_words(alias)
    if len(words) != 1:
        return bool(words)
    token = words[0]
    return token in _PROTECTED_ACRONYMS or normalize_facet_id(token) == facet_id


def _content_facets(
    *,
    text: Any,
    heading_path: list[Any] | None = None,
    source: str,
    existing_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Derive lightweight content facets from actual chunk/summary text.

    These facets are soft retrieval hints. They intentionally avoid model calls
    so ingestion/backfill stays deterministic, fast, and works offline.
    """

    headings = " ".join(str(h) for h in (heading_path or []) if h)
    body = str(text or "")
    haystack = _match_haystack(f"{headings}\n{body}")
    if not haystack:
        return {}

    existing = set(existing_ids or [])
    scored: list[tuple[str, float]] = []
    seen: set[str] = set()
    for facet_id, aliases in _CONTENT_FACET_ALIASES.items():
        matched = False
        for alias in aliases:
            if not _specific_content_alias(facet_id, alias):
                continue
            alias_norm = _match_haystack(alias)
            if not alias_norm:
                continue
            if re.search(rf"(?<![a-z0-9]){re.escape(alias_norm)}(?![a-z0-9])", haystack):
                matched = True
                break
        if matched and facet_id not in seen:
            seen.add(facet_id)
            scored.append((facet_id, 0.86 if facet_id not in existing else 0.8))

    phrase_text = f"{headings}\n{body[:4000]}"
    phrase_limit = min(
        max(0, _CONTENT_FACET_LIMIT - len(scored)),
        2 if scored else 4,
    )
    for fid, confidence in _content_phrase_facets(
        phrase_text,
        limit=phrase_limit,
    ):
        if fid in seen or fid in existing:
            continue
        seen.add(fid)
        scored.append((fid, confidence))
        if len(scored) >= _CONTENT_FACET_LIMIT:
            break

    if not scored:
        return {}
    content_ids = [fid for fid, _confidence in scored[:_CONTENT_FACET_LIMIT]]
    confidence = max(conf for _fid, conf in scored[:_CONTENT_FACET_LIMIT])
    return {
        "content_facet_ids": content_ids,
        "content_facet_text": " ".join(fid.replace("_", " ") for fid in content_ids),
        "content_facet_source": source,
        "content_facet_confidence": round(confidence, 3),
    }


def _merge_chunk_semantic_facets(
    *,
    facet_ids: list[str],
    facet_text: str,
    content_meta: dict[str, Any],
) -> dict[str, Any]:
    semantic = {
        "schema_version": FACET_SCHEMA_VERSION,
        "facet_ids": facet_ids,
        "source": "ingestion",
    }
    if facet_text:
        semantic["facet_text"] = facet_text
    for key in (
        "content_facet_ids",
        "content_facet_text",
        "content_facet_source",
        "content_facet_confidence",
    ):
        if content_meta.get(key) not in (None, "", []):
            semantic[key] = content_meta[key]
    return semantic


def _chunk_facets(
    chunk: Any,
    doc_facet_ids: list[str],
    *,
    content_text: Any = None,
    content_source: str = "chunk_text",
) -> dict[str, Any]:
    heading_labels = [_compact_label(h, max_words=8) for h in (getattr(chunk, "heading_path", None) or [])[:2]]
    local_ids = [normalize_facet_id(label) for label in heading_labels if label]
    facet_ids = list(dict.fromkeys([*local_ids, *doc_facet_ids[:6]]))[:8]
    facet_text = " ".join(fid.replace("_", " ") for fid in facet_ids)
    text_for_facets = content_text if content_text not in (None, "") else getattr(chunk, "text", "")
    content_meta = _content_facets(
        text=text_for_facets,
        heading_path=getattr(chunk, "heading_path", None) or [],
        source=content_source,
        existing_ids=facet_ids,
    )
    return {
        "facet_ids": facet_ids,
        "facet_text": facet_text,
        **content_meta,
        "semantic_facets": {
            **_merge_chunk_semantic_facets(
                facet_ids=facet_ids,
                facet_text=facet_text,
                content_meta=content_meta,
            )
        },
    }


def _document_content_facet_rows(
    *,
    doc_ref: dict[str, Any],
    parents: list[Any],
    children: list[Any],
    summaries: list[Any],
) -> list[dict[str, Any]]:
    summary_text = "\n".join(str(getattr(s, "summary", "") or "") for s in summaries[:24])
    parent_text = "\n".join(str(getattr(p, "text", "") or "")[:900] for p in parents[:12])
    child_text = "\n".join(str(getattr(c, "text", "") or "")[:500] for c in children[:24])
    heading_text = "\n".join(
        " ".join(str(h) for h in (getattr(item, "heading_path", None) or [])[:2])
        for item in [*parents[:16], *children[:16]]
    )
    content = _content_facets(
        text=f"{summary_text}\n{heading_text}\n{parent_text}\n{child_text}",
        heading_path=[],
        source="document_content",
    )
    rows: list[dict[str, Any]] = []
    for facet_id in content.get("content_facet_ids") or []:
        label = facet_id.replace("_", " ")
        aliases = [
            label,
            canonical_display_name(label),
            *list(_CONTENT_FACET_ALIASES.get(facet_id, ()))[:6],
        ]
        row = _facet_record(
            label,
            source_level="document_content",
            source_refs=[doc_ref],
            raw_values=aliases,
            confidence=max(0.58, float(content.get("content_facet_confidence") or 0.62) - 0.08),
        )
        if row:
            rows.append(row)
    return rows


def build_ingest_facet_profile(
    *,
    filename: str,
    doc_id: str,
    corpus_id: str,
    schema_lens: dict[str, Any] | None = None,
    parents: list[Any] | None = None,
    children: list[Any] | None = None,
    summaries: list[Any] | None = None,
) -> dict[str, Any]:
    """Build document/parent/child facet metadata for an ingest run."""

    doc_ref = {
        "doc_id": doc_id,
        "corpus_id": corpus_id,
        "filename": filename,
    }
    filename_label = _compact_label(filename, max_words=8) or filename
    primary = _facet_record(
        filename_label,
        source_level="doc",
        source_refs=[doc_ref],
        raw_values=[filename, filename_label],
        confidence=0.9,
    )
    rows: list[dict[str, Any]] = []
    if primary:
        rows.append(primary)
    rows.extend(_schema_lens_facets(schema_lens, doc_ref))
    rows.extend(_heading_facets([*(parents or []), *(children or [])], doc_ref))
    rows.extend(
        _document_content_facet_rows(
            doc_ref=doc_ref,
            parents=parents or [],
            children=children or [],
            summaries=summaries or [],
        )
    )
    doc_facets = _dedupe_facets(rows)
    doc_facet_ids = [row["facet_id"] for row in doc_facets]
    doc_facet_text = " ".join(
        str(term)
        for row in doc_facets
        for term in [row.get("display_name"), *(row.get("aliases") or [])[:2]]
        if term
    )
    summary_by_parent = {
        str(getattr(s, "parent_id", "") or ""): getattr(s, "summary", "")
        for s in (summaries or [])
        if getattr(s, "parent_id", None)
    }
    parent_facets = {
        p.parent_id: _chunk_facets(
            p,
            doc_facet_ids,
            content_text=summary_by_parent.get(str(p.parent_id)) or getattr(p, "text", ""),
            content_source=(
                "parent_summary"
                if summary_by_parent.get(str(p.parent_id))
                else "parent_text"
            ),
        )
        for p in (parents or [])
        if getattr(p, "parent_id", None)
    }
    child_facets = {
        c.chunk_id: _chunk_facets(
            c,
            doc_facet_ids,
            content_text=getattr(c, "text", ""),
            content_source="child_text",
        )
        for c in (children or [])
        if getattr(c, "chunk_id", None)
    }
    return {
        "schema_version": FACET_SCHEMA_VERSION,
        "doc_facets": doc_facets,
        "facet_ids": doc_facet_ids,
        "facet_text": doc_facet_text,
        "primary_facet_id": doc_facet_ids[0] if doc_facet_ids else None,
        "source": "ingestion",
        "parent_facets": parent_facets,
        "child_facets": child_facets,
    }
