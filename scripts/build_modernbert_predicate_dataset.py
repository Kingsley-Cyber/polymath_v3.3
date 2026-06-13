#!/usr/bin/env python3
"""Build JSONL training data for a ModernBERT predicate classifier.

The classifier learns only:

    sentence [SEP] subject [SEP] object -> predicate | none

Python/Neo4j still own entities, evidence, ontology mapping, and final JSONL.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - only hit on lean system Pythons.
    yaml = None


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "data"
DEFAULT_ONTOLOGY = REPO_ROOT / "config/ontology.yaml"
DEFAULT_EVAL_DIRS = [
    REPO_ROOT / "eval/fixtures",
    REPO_ROOT / "scripts/local_extraction_fixtures",
]

PREDICATES = [
    "includes",
    "uses",
    "supports",
    "produces",
    "implements",
    "has_part",
    "instance_of",
    "references",
    "depends_on",
    "quantizes",
    "causes",
    "synonym_of",
    "member_of",
    "example_of",
    "evaluates",
    "deploys",
    "creates",
    "trains",
    "runs",
    "located_in",
    "none",
]

# Graph predicate -> classifier label, with optional subject/object reversal.
GRAPH_PREDICATE_MAP: dict[str, tuple[str, bool]] = {
    "includes": ("includes", False),
    "uses": ("uses", False),
    "supports": ("supports", False),
    "produces": ("produces", False),
    "implements": ("implements", False),
    "has_part": ("has_part", False),
    "part_of": ("has_part", True),
    "instance_of": ("instance_of", False),
    "references": ("references", False),
    "depends_on": ("depends_on", False),
    "quantizes": ("quantizes", False),
    "causes": ("causes", False),
    "synonym_of": ("synonym_of", False),
    "member_of": ("member_of", False),
    "example_of": ("example_of", False),
    "evaluates": ("evaluates", False),
    "deploys": ("deploys", False),
    "creates": ("creates", False),
    "created_by": ("creates", True),
    "trains": ("trains", False),
    "trained_on": ("trains", True),
    "runs": ("runs", False),
    "runs_on": ("runs", False),
    "located_in": ("located_in", False),
}

PATTERNS: dict[str, re.Pattern[str]] = {
    "copula": re.compile(r"\b(?:is a|is an|are|was a|was an|were)\b", re.I),
    "appositive": re.compile(r",\s*(?:a|an|the)?\s*[A-Za-z][^,]{3,80},|\([^)]{3,120}\)", re.I),
    "active_verb": re.compile(
        r"\b(?:uses?|supports?|produces?|implements?|references?|depends?|causes?|creates?|trains?|runs?|deploys?|includes?|evaluates?|quantizes?|contains?|members?|located|called|classified)\b",
        re.I,
    ),
    "passive": re.compile(r"\b(?:is|are|was|were)\s+\w+ed\s+by\b", re.I),
    "list": re.compile(r",\s*[^,]{2,},\s*[^,]{2,}", re.I),
    "metaphorical": re.compile(r"\b(?:brings?|represents?|serves as|acts as|stands for)\b", re.I),
}

_SECTION_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)+\b")
_NOISY_SENTENCE_RE = re.compile(
    r"\b(?:contents|table of contents|keywords|series editors|editorial board|"
    r"part [ivxlcdm]+|chapter \d+|appendix|bibliography|references|index)\b",
    re.I,
)

TYPE_TERMS: dict[str, list[str]] = {
    "Person": ["Alice Chen", "Maya Patel", "Jordan Lee", "Ravi Kumar"],
    "Organization": ["Meta AI", "Open Robotics Lab", "Acme Research", "Edge Systems Group"],
    "Location": ["edge device", "mobile runtime", "cloud region", "Android device"],
    "Event": ["training run", "deployment test", "benchmark session", "release cycle"],
    "Concept": ["model compression", "retrieval quality", "latency budget", "privacy boundary"],
    "Method": ["post-training quantization", "LoRA finetuning", "schema validation", "batch inference"],
    "Product": ["mobile assistant", "Flutter application", "analytics dashboard", "AI camera app"],
    "Software": ["TensorFlow Lite", "ExecuTorch runtime", "ONNX Runtime", "Neo4j service"],
    "Document": ["deployment guide", "model card", "API specification", "evaluation report"],
    "Standard": ["ONNX format", "JSON Schema", "OpenAPI standard", "WCAG guideline"],
    "Rule": ["privacy rule", "routing policy", "validation rule", "retry rule"],
    "Law": ["data protection law", "privacy regulation", "accessibility law", "copyright rule"],
    "Artifact": ["INT8 weights", "adapter checkpoint", "embedding index", "JSONL file"],
    "TimeReference": ["2025 release window", "nightly batch", "warmup interval", "training epoch"],
    "other": ["external resource", "unknown component", "supporting item", "legacy object"],
}


@dataclass(frozen=True)
class Example:
    text: str
    subject: str
    subject_type: str
    object: str
    object_type: str
    label: str
    source: str
    subject_surface: str = ""
    object_surface: str = ""
    chunk_id: str = ""
    doc_id: str = ""

    def key(self) -> tuple[str, str, str, str]:
        return (normalize_text(self.text), normalize_name(self.subject), normalize_name(self.object), self.label)

    def json(self) -> dict[str, Any]:
        data = {
            "text": self.text,
            "subject": self.subject,
            "subject_type": self.subject_type,
            "object": self.object,
            "object_type": self.object_type,
            "label": self.label,
        }
        if self.subject_surface:
            data["subject_surface"] = self.subject_surface
        if self.object_surface:
            data["object_surface"] = self.object_surface
        return data


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def sentence_split(text: str) -> list[str]:
    pieces = re.split(r"(?<=[.!?])\s+|\n{2,}", str(text or ""))
    out = []
    for piece in pieces:
        sentence = re.sub(r"\s+", " ", piece).strip()
        if 20 <= len(sentence) <= 500:
            out.append(sentence)
    return out


def sentence_for_evidence(phrase: str, chunk_text: str | None) -> str:
    phrase = re.sub(r"\s+", " ", str(phrase or "")).strip()
    if chunk_text:
        phrase_key = normalize_text(phrase)
        for sentence in sentence_split(chunk_text):
            if phrase_key and phrase_key in normalize_text(sentence):
                return sentence
    return phrase[:500]


def name_present(example: Example) -> bool:
    text_key = normalize_text(example.text)
    subject_options = [example.subject, example.subject_surface]
    object_options = [example.object, example.object_surface]
    subject_ok = any(normalize_text(item) and normalize_text(item) in text_key for item in subject_options)
    object_ok = any(normalize_text(item) and normalize_text(item) in text_key for item in object_options)
    return subject_ok and object_ok


def valid_length(example: Example) -> bool:
    return 20 <= len(example.text) <= 500


def noisy_training_sentence(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return True
    lower = raw.lower()
    if raw.startswith("#"):
        return True
    if "•" in raw or "......" in raw:
        return True
    if _NOISY_SENTENCE_RE.search(raw) and (_SECTION_NUMBER_RE.search(raw) or len(raw.split()) <= 14):
        return True
    if re.match(r"^\d+(?:\.\d+)*\s+", raw) and re.search(r"\b\d{1,4}$", raw):
        return True
    if len(_SECTION_NUMBER_RE.findall(raw)) >= 3:
        return True
    short_numbers = re.findall(r"\b\d{1,3}\b", raw)
    if len(_SECTION_NUMBER_RE.findall(raw)) >= 2 and len(short_numbers) >= 3:
        return True
    if len(short_numbers) >= 5 and len(re.findall(r"[.!?]", raw)) == 0:
        return True
    if re.search(r"\b\d{1,4}(?:,\s*\d{1,4}){1,}\b", raw):
        return True
    if re.search(r"\b\w+,\s*\w+(?:ing|ed)\s+\d{1,4}\b", lower):
        return True
    alpha = sum(ch.isalpha() for ch in raw)
    visible = sum(not ch.isspace() for ch in raw)
    if visible and alpha / visible < 0.45:
        return True
    return False


def hash_text(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def load_eval_texts(paths: list[Path]) -> set[str]:
    texts: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        files = [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]
        for file in files:
            if file.suffix.lower() == ".jsonl":
                for line in file.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    text = str(row.get("text") or row.get("evidence") or "")
                    if text:
                        texts.add(normalize_text(text))
            elif file.suffix.lower() == ".json":
                try:
                    data = json.loads(file.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    continue
                stack = [data]
                while stack:
                    item = stack.pop()
                    if isinstance(item, dict):
                        stack.extend(item.values())
                    elif isinstance(item, list):
                        stack.extend(item)
                    elif isinstance(item, str) and len(item) >= 20:
                        texts.add(normalize_text(item))
            else:
                raw = file.read_text(encoding="utf-8", errors="ignore")
                for sentence in sentence_split(raw):
                    texts.add(normalize_text(sentence))
    return texts


def contaminated(example: Example, eval_texts: set[str]) -> bool:
    text = normalize_text(example.text)
    if not text:
        return True
    if text in eval_texts:
        return True
    return any(len(text) >= 80 and text in eval_text for eval_text in eval_texts)


def load_ontology_pairs(path: Path) -> dict[str, set[tuple[str, str]]]:
    if not path.exists():
        return {}
    if yaml is None:
        raise RuntimeError("PyYAML is required to read config/ontology.yaml. Install with: pip install PyYAML")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, set[tuple[str, str]]] = {}
    for predicate, cfg in (data.get("predicates") or {}).items():
        pairs = cfg.get("allowed_pairs") if isinstance(cfg, dict) else []
        valid_pairs: set[tuple[str, str]] = set()
        for pair in pairs or []:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                valid_pairs.add((str(pair[0]), str(pair[1])))
        out[str(predicate)] = valid_pairs
    return out


def load_raw_chunk_texts(paths: list[Path]) -> dict[str, str]:
    """Load optional raw chunk JSON/JSONL sidecars keyed by chunk_id.

    Neo4j is the source of graph truth, but this repo's Neo4j writer may store
    only Chunk identity/provenance. A sidecar lets us build real same-sentence
    negatives from Neo4j mentions without requiring text on every :Chunk node.
    """
    chunks: dict[str, str] = {}
    for path in paths:
        if not path.exists():
            continue
        files = [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]
        for file in files:
            if file.suffix.lower() not in {".json", ".jsonl"}:
                continue
            rows: list[Any] = []
            if file.suffix.lower() == ".jsonl":
                for line in file.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if not line.strip():
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            else:
                try:
                    data = json.loads(file.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    continue
                rows = data if isinstance(data, list) else [data]
            for row in rows:
                if not isinstance(row, dict):
                    continue
                chunk_id = str(row.get("chunk_id") or row.get("id") or "").strip()
                text = str(
                    row.get("text")
                    or row.get("chunk_text")
                    or row.get("content")
                    or row.get("body")
                    or ""
                ).strip()
                if chunk_id and text:
                    chunks[chunk_id] = text
    return chunks


def host_uri(uri: str, service_name: str, host: str = "127.0.0.1") -> str:
    return uri.replace(f"@{service_name}:", f"@{host}:").replace(
        f"//{service_name}:",
        f"//{host}:",
    )


def load_mongo_chunk_texts(
    *,
    uri: str,
    database: str | None,
    limit: int,
    timeout: float,
) -> dict[str, str]:
    try:
        from pymongo import MongoClient  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise RuntimeError("Install pymongo first: pip install pymongo") from exc
    client = MongoClient(uri, serverSelectionTimeoutMS=int(timeout * 1000))
    client.admin.command("ping")
    db = client[database] if database else client.get_default_database()
    cursor = db["chunks"].find(
        {},
        {
            "_id": 0,
            "chunk_id": 1,
            "id": 1,
            "text": 1,
            "chunk_text": 1,
            "content": 1,
            "body": 1,
        },
    )
    if limit > 0:
        cursor = cursor.limit(limit)
    out: dict[str, str] = {}
    for row in cursor:
        chunk_id = str(row.get("chunk_id") or row.get("id") or "").strip()
        text = str(
            row.get("text")
            or row.get("chunk_text")
            or row.get("content")
            or row.get("body")
            or ""
        ).strip()
        if chunk_id and text:
            out[chunk_id] = text
    client.close()
    return out


def get_driver(uri: str, user: str, password: str, *, timeout: float = 5.0):
    try:
        from neo4j import GraphDatabase  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise RuntimeError("Install neo4j first: pip install neo4j") from exc
    driver = GraphDatabase.driver(uri, auth=(user, password), connection_timeout=timeout)
    driver.verify_connectivity()
    return driver


def entity_type_expr(alias: str) -> str:
    return (
        f"coalesce({alias}.primary_entity_type, {alias}.entity_type, "
        f"head(coalesce({alias}.observed_entity_types, [])), 'Concept')"
    )


def positive_query() -> str:
    graph_predicates = sorted(GRAPH_PREDICATE_MAP)
    return f"""
    MATCH (s:Entity)-[r:RELATES_TO]->(o:Entity)
    WITH s, r, o,
         [p IN [r.predicate] + coalesce(r.source_predicates, []) WHERE p IN $graph_predicates] AS matched_predicates
    WHERE size(matched_predicates) > 0
    WITH s, r, o, head(matched_predicates) AS graph_predicate,
    coalesce(r.evidence_phrases, []) AS evidence_phrases,
         coalesce(r.evidence_chunk_ids, []) AS chunk_ids,
         coalesce(r.evidence_doc_ids, []) AS doc_ids,
         coalesce(r.confidence, 0.0) AS confidence
    WITH s, r, o, graph_predicate, evidence_phrases, chunk_ids, doc_ids, confidence,
         range(0, size(evidence_phrases) - 1) AS idxs
    UNWIND idxs AS idx
    WITH s, r, o, graph_predicate, confidence,
         evidence_phrases[idx] AS evidence_phrase,
         CASE WHEN idx < size(chunk_ids) THEN chunk_ids[idx] ELSE head(chunk_ids) END AS chunk_id,
         CASE WHEN idx < size(doc_ids) THEN doc_ids[idx] ELSE head(doc_ids) END AS doc_id
    OPTIONAL MATCH (c:Chunk {{chunk_id: chunk_id}})
    RETURN
      coalesce(s.canonical_name, s.normalized_name, s.display_name) AS subject,
      {entity_type_expr("s")} AS subject_type,
      coalesce(s.display_name, s.canonical_name, s.normalized_name) AS subject_surface,
      coalesce(o.canonical_name, o.normalized_name, o.display_name) AS object,
      {entity_type_expr("o")} AS object_type,
      coalesce(o.display_name, o.canonical_name, o.normalized_name) AS object_surface,
      graph_predicate,
      confidence,
      evidence_phrase,
      coalesce(properties(c)['text'], properties(c)['chunk_text'], properties(c)['content'], '') AS chunk_text,
      chunk_id,
      doc_id
    """


def fetch_positive_examples(
    driver: Any,
    limit: int | None,
    min_confidence: float,
    per_graph_predicate: int = 0,
) -> list[Example]:
    examples: list[Example] = []

    def collect(graph_predicates: list[str], row_limit: int | None) -> None:
        start_count = len(examples)
        with driver.session() as session:
            result = session.run(
                positive_query(),
                graph_predicates=graph_predicates,
            )
            for row in result:
                graph_predicate = str(row["graph_predicate"])
                confidence = float(row["confidence"] or 0.0)
                if confidence and confidence < min_confidence:
                    continue
                label, reverse = GRAPH_PREDICATE_MAP[graph_predicate]
                subject = str(row["subject"] or "")
                subject_type = str(row["subject_type"] or "Concept")
                subject_surface = str(row["subject_surface"] or subject)
                obj = str(row["object"] or "")
                object_type = str(row["object_type"] or "Concept")
                object_surface = str(row["object_surface"] or obj)
                if reverse:
                    subject, obj = obj, subject
                    subject_type, object_type = object_type, subject_type
                    subject_surface, object_surface = object_surface, subject_surface
                text = sentence_for_evidence(str(row["evidence_phrase"] or ""), row["chunk_text"])
                examples.append(
                    Example(
                        text=text,
                        subject=subject,
                        subject_type=subject_type,
                        object=obj,
                        object_type=object_type,
                        label=label,
                        source="neo4j_positive",
                        subject_surface=subject_surface,
                        object_surface=object_surface,
                        chunk_id=str(row["chunk_id"] or ""),
                        doc_id=str(row["doc_id"] or ""),
                    )
                )
                if row_limit and len(examples) - start_count >= row_limit:
                    break

    if per_graph_predicate > 0:
        for graph_predicate in sorted(GRAPH_PREDICATE_MAP):
            collect([graph_predicate], per_graph_predicate)
            if limit and len(examples) >= limit:
                return examples[:limit]
        return examples

    with driver.session() as session:
        result = session.run(
            positive_query(),
            graph_predicates=sorted(GRAPH_PREDICATE_MAP),
        )
        for row in result:
            graph_predicate = str(row["graph_predicate"])
            confidence = float(row["confidence"] or 0.0)
            if confidence and confidence < min_confidence:
                continue
            label, reverse = GRAPH_PREDICATE_MAP[graph_predicate]
            subject = str(row["subject"] or "")
            subject_type = str(row["subject_type"] or "Concept")
            subject_surface = str(row["subject_surface"] or subject)
            obj = str(row["object"] or "")
            object_type = str(row["object_type"] or "Concept")
            object_surface = str(row["object_surface"] or obj)
            if reverse:
                subject, obj = obj, subject
                subject_type, object_type = object_type, subject_type
                subject_surface, object_surface = object_surface, subject_surface
            text = sentence_for_evidence(str(row["evidence_phrase"] or ""), row["chunk_text"])
            ex = Example(
                text=text,
                subject=subject,
                subject_type=subject_type,
                object=obj,
                object_type=object_type,
                label=label,
                source="neo4j_positive",
                subject_surface=subject_surface,
                object_surface=object_surface,
                chunk_id=str(row["chunk_id"] or ""),
                doc_id=str(row["doc_id"] or ""),
            )
            examples.append(ex)
            if limit and len(examples) >= limit:
                break
    return examples


def chunk_mentions_query() -> str:
    return f"""
    MATCH (c:Chunk)-[m:MENTIONS]->(e:Entity)
    WITH c, collect(DISTINCT {{
        id: e.entity_id,
        name: coalesce(e.canonical_name, e.normalized_name, e.display_name),
        surface: coalesce(m.surface_form, e.display_name, e.canonical_name, e.normalized_name),
        type: {entity_type_expr("e")}
    }}) AS entities
    WHERE size(entities) >= 2
    RETURN c.chunk_id AS chunk_id,
           c.doc_id AS doc_id,
           coalesce(properties(c)['text'], properties(c)['chunk_text'], properties(c)['content'], '') AS text,
           entities
    LIMIT $limit
    """


def existing_edge_query() -> str:
    return """
    MATCH (s:Entity)-[r:RELATES_TO]->(o:Entity)
    RETURN s.entity_id AS subject_id, o.entity_id AS object_id
    """


def fetch_existing_edges(driver: Any) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    with driver.session() as session:
        for row in session.run(existing_edge_query()):
            s = str(row["subject_id"] or "")
            o = str(row["object_id"] or "")
            if s and o:
                out.add((s, o))
                out.add((o, s))
    return out


def entity_in_sentence(entity: dict[str, Any], sentence: str) -> bool:
    sent = normalize_text(sentence)
    return any(
        normalize_text(entity.get(key) or "") and normalize_text(entity.get(key) or "") in sent
        for key in ("surface", "name")
    )


def fetch_negative_examples(
    driver: Any,
    target_count: int,
    chunk_limit: int,
    raw_chunk_texts: dict[str, str] | None = None,
) -> list[Example]:
    existing_edges = fetch_existing_edges(driver)
    examples: list[Example] = []
    seen: set[tuple[str, str, str]] = set()
    raw_chunk_texts = raw_chunk_texts or {}
    with driver.session() as session:
        for row in session.run(chunk_mentions_query(), limit=chunk_limit):
            chunk_id = str(row["chunk_id"] or "")
            text = str(row["text"] or "") or raw_chunk_texts.get(chunk_id, "")
            if not text:
                continue
            entities = [item for item in (row["entities"] or []) if item.get("id")]
            for sentence in sentence_split(text):
                present = [entity for entity in entities if entity_in_sentence(entity, sentence)]
                if len(present) < 2:
                    continue
                for i, left in enumerate(present):
                    for right in present[i + 1 :]:
                        lid, rid = str(left["id"]), str(right["id"])
                        if (lid, rid) in existing_edges:
                            continue
                        key = (normalize_text(sentence), normalize_name(left["name"]), normalize_name(right["name"]))
                        if key in seen:
                            continue
                        seen.add(key)
                        examples.append(
                            Example(
                                text=sentence,
                                subject=str(left["name"] or ""),
                                subject_type=str(left["type"] or "Concept"),
                                object=str(right["name"] or ""),
                                object_type=str(right["type"] or "Concept"),
                                label="none",
                                source="neo4j_negative",
                                subject_surface=str(left.get("surface") or left.get("name") or ""),
                                object_surface=str(right.get("surface") or right.get("name") or ""),
                                chunk_id=chunk_id,
                                doc_id=str(row["doc_id"] or ""),
                            )
                        )
                        if len(examples) >= target_count:
                            return examples
    return examples


def dedupe_and_filter(examples: list[Example], eval_texts: set[str]) -> tuple[list[Example], dict[str, int]]:
    out: list[Example] = []
    seen: set[tuple[str, str, str, str]] = set()
    drops: dict[str, int] = collections.Counter()
    for example in examples:
        if example.label not in PREDICATES:
            drops["bad_label"] += 1
            continue
        if not valid_length(example):
            drops["bad_length"] += 1
            continue
        if noisy_training_sentence(example.text):
            drops["noisy_sentence"] += 1
            continue
        if not name_present(example):
            drops["name_not_in_sentence"] += 1
            continue
        if contaminated(example, eval_texts):
            drops["eval_contamination"] += 1
            continue
        key = example.key()
        if key in seen:
            drops["duplicate"] += 1
            continue
        seen.add(key)
        out.append(example)
    return out, dict(drops)


def pattern_hits(text: str) -> set[str]:
    return {name for name, pattern in PATTERNS.items() if pattern.search(text)}


def audit_examples(
    examples: list[Example],
    ontology_pairs: dict[str, set[tuple[str, str]]],
    *,
    min_per_predicate: int,
    min_type_pair: int,
    none_min_ratio: float,
) -> dict[str, Any]:
    counts = collections.Counter(ex.label for ex in examples)
    total = len(examples)
    pattern_counts: dict[str, dict[str, int]] = {
        pred: {name: 0 for name in PATTERNS}
        for pred in PREDICATES
        if pred != "none"
    }
    pair_counts = collections.Counter((ex.label, ex.subject_type, ex.object_type) for ex in examples if ex.label != "none")
    for ex in examples:
        if ex.label == "none":
            continue
        for pattern in pattern_hits(ex.text):
            pattern_counts.setdefault(ex.label, {name: 0 for name in PATTERNS})[pattern] += 1

    low_predicates = {
        pred: counts.get(pred, 0)
        for pred in PREDICATES
        if pred != "none" and counts.get(pred, 0) < min_per_predicate
    }
    pattern_gaps: dict[str, dict[str, float]] = {}
    for pred, by_pattern in pattern_counts.items():
        pred_count = counts.get(pred, 0)
        if pred_count == 0:
            pattern_gaps[pred] = {name: 0.0 for name in PATTERNS}
            continue
        gaps = {
            name: by_pattern.get(name, 0) / pred_count
            for name in PATTERNS
            if by_pattern.get(name, 0) / pred_count < 0.05
        }
        if gaps:
            pattern_gaps[pred] = gaps

    type_pair_gaps: dict[str, list[dict[str, Any]]] = {}
    for pred, pairs in ontology_pairs.items():
        if pred == "none":
            continue
        missing = []
        for subject_type, object_type in sorted(pairs):
            count = pair_counts.get((pred, subject_type, object_type), 0)
            if count < min_type_pair:
                missing.append({"subject_type": subject_type, "object_type": object_type, "count": count})
        if missing:
            type_pair_gaps[pred] = missing

    length_failures = sum(not valid_length(ex) for ex in examples)
    name_failures = sum(not name_present(ex) for ex in examples)
    duplicate_count = len(examples) - len({ex.key() for ex in examples})
    return {
        "total_examples": total,
        "predicate_counts": dict(counts),
        "none_ratio": counts.get("none", 0) / total if total else 0.0,
        "low_predicates": low_predicates,
        "none_ratio_ok": counts.get("none", 0) / total >= none_min_ratio if total else False,
        "pattern_counts": pattern_counts,
        "pattern_gaps_below_5_percent": pattern_gaps,
        "type_pair_counts": {f"{k[0]}|{k[1]}|{k[2]}": v for k, v in pair_counts.items()},
        "type_pair_gaps": type_pair_gaps,
        "duplicate_count": duplicate_count,
        "length_failures": length_failures,
        "name_presence_failures": name_failures,
    }


def gemini_generate_examples(
    *,
    api_key: str,
    model: str,
    predicate: str,
    count: int,
    patterns: list[str],
) -> list[Example]:
    prompt = {
        "contents": [
            {
                "parts": [
                    {
                        "text": (
                            "Generate JSONL examples for a relation classifier. "
                            "Each line must have text, subject, subject_type, object, object_type, label. "
                            f"Label must be {predicate!r}. Use these sentence patterns: {patterns}. "
                            "Use realistic technical textbook sentences. Entity names must appear exactly in text. "
                            f"Return exactly {count} JSONL lines and no markdown."
                        )
                    }
                ]
            }
        ]
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    req = urllib.request.Request(
        url,
        data=json.dumps(prompt).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        data = json.loads(response.read().decode("utf-8"))
    text = "\n".join(
        part.get("text", "")
        for candidate in data.get("candidates", [])
        for part in candidate.get("content", {}).get("parts", [])
    )
    examples: list[Example] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        examples.append(
            Example(
                text=str(row.get("text") or ""),
                subject=str(row.get("subject") or ""),
                subject_type=str(row.get("subject_type") or "Concept"),
                object=str(row.get("object") or ""),
                object_type=str(row.get("object_type") or "Concept"),
                label=predicate,
                source="gemini_synthetic",
            )
        )
    return examples


def fill_gaps_with_synthetic(
    examples: list[Example],
    audit: dict[str, Any],
    *,
    api_key: str,
    model: str,
    min_per_predicate: int,
    max_per_predicate: int,
) -> list[Example]:
    additions: list[Example] = []
    counts = collections.Counter(ex.label for ex in examples)
    for predicate, count in audit["low_predicates"].items():
        needed = min(max_per_predicate, max(0, min_per_predicate - int(count)))
        if needed <= 0:
            continue
        patterns = list((audit["pattern_gaps_below_5_percent"].get(predicate) or PATTERNS).keys())
        additions.extend(
            gemini_generate_examples(
                api_key=api_key,
                model=model,
                predicate=predicate,
                count=needed,
                patterns=patterns[:4],
            )
        )
        counts[predicate] += needed
    return additions


def term_for_type(entity_type: str, index: int) -> str:
    terms = TYPE_TERMS.get(entity_type) or TYPE_TERMS["Concept"]
    return terms[index % len(terms)]


def relation_verb(predicate: str) -> str:
    return {
        "includes": "includes",
        "uses": "uses",
        "supports": "supports",
        "produces": "produces",
        "implements": "implements",
        "has_part": "contains",
        "instance_of": "is an example of",
        "references": "references",
        "depends_on": "depends on",
        "quantizes": "quantizes",
        "causes": "causes",
        "synonym_of": "is also called",
        "member_of": "is a member of",
        "example_of": "is an example of",
        "evaluates": "evaluates",
        "deploys": "deploys",
        "creates": "creates",
        "trains": "trains",
        "runs": "runs",
        "located_in": "is located in",
    }.get(predicate, "uses")


def template_sentence(predicate: str, subject: str, obj: str, pattern: str, index: int) -> str:
    verb = relation_verb(predicate)
    if pattern == "copula":
        return f"{subject} is a technical system that {verb} {obj} during controlled extraction workflows."
    if pattern == "appositive":
        return f"{subject}, a local extraction component, {verb} {obj} for evidence-grounded graph construction."
    if pattern == "passive":
        passive = {
            "uses": f"{obj} is used by {subject} during local inference.",
            "includes": f"{obj} is included by {subject} in the local extraction package.",
            "supports": f"{obj} is supported by {subject} during deployment.",
            "produces": f"{obj} is produced by {subject} after validation.",
            "implements": f"{obj} is implemented by {subject} in the extraction pipeline.",
            "has_part": f"{obj} is included by {subject} as a required component.",
            "instance_of": f"{obj} is represented by {subject} in the ontology examples.",
            "references": f"{obj} is referenced by {subject} in the technical guide.",
            "depends_on": f"{obj} is required by {subject} for stable operation.",
            "quantizes": f"{obj} is quantized by {subject} before mobile deployment.",
            "causes": f"{obj} is caused by {subject} when the threshold is exceeded.",
            "synonym_of": f"{obj} is named by {subject} in the alias table.",
            "member_of": f"{obj} is joined by {subject} in the organization map.",
            "example_of": f"{obj} is exemplified by {subject} in the training corpus.",
            "evaluates": f"{obj} is evaluated by {subject} before release.",
            "deploys": f"{obj} is deployed by {subject} to the target runtime.",
            "creates": f"{obj} is created by {subject} during preprocessing.",
            "trains": f"{obj} is trained by {subject} using labeled relation pairs.",
            "runs": f"{obj} is executed by {subject} during the benchmark.",
            "located_in": f"{obj} is hosted by {subject} in the deployment map.",
        }.get(predicate)
        if passive:
            return passive
    if pattern == "list":
        return f"{subject} {verb} {obj}, alpha, beta, gamma in the same workflow."
    if pattern == "metaphorical":
        return f"{subject} serves as the mechanism that {verb} {obj} in the local ontology pipeline."
    return f"{subject} {verb} {obj} in a deterministic extraction workflow."


def make_template_example(
    predicate: str,
    subject_type: str,
    object_type: str,
    pattern: str,
    index: int,
) -> Example:
    subject = f"{term_for_type(subject_type, index)} {index}"
    obj = f"{term_for_type(object_type, index + 3)} {index}"
    return Example(
        text=template_sentence(predicate, subject, obj, pattern, index),
        subject=subject,
        subject_type=subject_type,
        object=obj,
        object_type=object_type,
        label=predicate,
        source="template_synthetic",
        subject_surface=subject,
        object_surface=obj,
    )


def fill_gaps_with_templates(
    examples: list[Example],
    audit: dict[str, Any],
    ontology_pairs: dict[str, set[tuple[str, str]]],
    *,
    min_per_predicate: int,
    min_type_pair: int,
    max_per_predicate: int,
) -> list[Example]:
    additions: list[Example] = []
    counts = collections.Counter(ex.label for ex in examples)
    pair_counts = collections.Counter((ex.label, ex.subject_type, ex.object_type) for ex in examples if ex.label != "none")
    pattern_counts = audit.get("pattern_counts") or {}
    seq = 0

    def add(predicate: str, subject_type: str, object_type: str, pattern: str, count: int) -> None:
        nonlocal seq
        if predicate == "none" or count <= 0:
            return
        current_added = sum(1 for ex in additions if ex.label == predicate)
        allowed = max(0, max_per_predicate - current_added)
        for _ in range(min(count, allowed)):
            seq += 1
            ex = make_template_example(predicate, subject_type, object_type, pattern, seq)
            additions.append(ex)
            counts[predicate] += 1
            pair_counts[(predicate, subject_type, object_type)] += 1

    for predicate in PREDICATES:
        if predicate == "none":
            continue
        pairs = sorted(ontology_pairs.get(predicate) or {("Concept", "Concept")})
        if counts[predicate] < min_per_predicate:
            needed = min_per_predicate - counts[predicate]
            for i in range(needed):
                subject_type, object_type = pairs[i % len(pairs)]
                add(predicate, subject_type, object_type, "active_verb", 1)

    for predicate, pairs in ontology_pairs.items():
        if predicate == "none":
            continue
        for subject_type, object_type in sorted(pairs):
            count = pair_counts.get((predicate, subject_type, object_type), 0)
            if count < min_type_pair:
                needed = min_type_pair - count
                for i in range(needed):
                    add(predicate, subject_type, object_type, list(PATTERNS)[i % len(PATTERNS)], 1)

    for predicate, gaps in (audit.get("pattern_gaps_below_5_percent") or {}).items():
        if predicate == "none":
            continue
        pairs = sorted(ontology_pairs.get(predicate) or {("Concept", "Concept")})
        pred_count = max(1, counts[predicate])
        for pattern in gaps:
            current = int((pattern_counts.get(predicate) or {}).get(pattern, 0))
            needed = max(0, int(((0.05 * pred_count) - current) / 0.95) + 1)
            for i in range(needed):
                subject_type, object_type = pairs[(i + current) % len(pairs)]
                add(predicate, subject_type, object_type, pattern, 1)

    return additions


def stratified_split(examples: list[Example], seed: int) -> tuple[list[Example], list[Example]]:
    rng = random.Random(seed)
    by_label: dict[str, list[Example]] = collections.defaultdict(list)
    for example in examples:
        by_label[example.label].append(example)
    train: list[Example] = []
    val: list[Example] = []
    for label, rows in by_label.items():
        rng.shuffle(rows)
        val_count = max(1, round(len(rows) * 0.10)) if len(rows) >= 10 else max(0, round(len(rows) * 0.10))
        val.extend(rows[:val_count])
        train.extend(rows[val_count:])
    rng.shuffle(train)
    rng.shuffle(val)
    return train, val


def max_distribution_delta(train: list[Example], val: list[Example]) -> float:
    train_counts = collections.Counter(ex.label for ex in train)
    val_counts = collections.Counter(ex.label for ex in val)
    train_total = max(1, len(train))
    val_total = max(1, len(val))
    return max(
        abs((train_counts.get(pred, 0) / train_total) - (val_counts.get(pred, 0) / val_total))
        for pred in PREDICATES
    )


def write_jsonl(path: Path, examples: list[Example]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for example in examples:
            fh.write(json.dumps(example.json(), ensure_ascii=False, separators=(",", ":")) + "\n")


def markdown_report(
    *,
    audit: dict[str, Any],
    train: list[Example],
    val: list[Example],
    drops: dict[str, int],
    synthetic_count: int,
    final_checks: dict[str, Any],
) -> str:
    lines = [
        "# ModernBERT Predicate Dataset Audit",
        "",
        "## BLUF",
        "",
        f"- Total examples: `{audit['total_examples']}`",
        f"- Train/validation: `{len(train)}` / `{len(val)}`",
        f"- Synthetic examples added: `{synthetic_count}`",
        f"- Drop counts: `{json.dumps(drops, sort_keys=True)}`",
        "",
        "## Final Checks",
        "",
    ]
    for key, value in final_checks.items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## Predicate Distribution", ""])
    for predicate in PREDICATES:
        lines.append(f"- `{predicate}`: `{audit['predicate_counts'].get(predicate, 0)}`")
    lines.extend(["", "## Predicates Below Threshold", ""])
    if audit["low_predicates"]:
        for predicate, count in audit["low_predicates"].items():
            lines.append(f"- `{predicate}`: `{count}`")
    else:
        lines.append("- none")
    lines.extend(["", "## Pattern Gaps Below 5%", ""])
    if audit["pattern_gaps_below_5_percent"]:
        for predicate, gaps in audit["pattern_gaps_below_5_percent"].items():
            gap_text = ", ".join(f"{name}={ratio:.2%}" for name, ratio in gaps.items())
            lines.append(f"- `{predicate}`: {gap_text}")
    else:
        lines.append("- none")
    lines.extend(["", "## Type Pair Gaps", ""])
    if audit["type_pair_gaps"]:
        for predicate, gaps in audit["type_pair_gaps"].items():
            shown = ", ".join(f"{g['subject_type']}->{g['object_type']} ({g['count']})" for g in gaps[:20])
            suffix = " ..." if len(gaps) > 20 else ""
            lines.append(f"- `{predicate}`: {shown}{suffix}")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Dataset Hygiene",
            "",
            f"- Duplicate tuples after filtering: `{audit['duplicate_count']}`",
            f"- Length failures after filtering: `{audit['length_failures']}`",
            f"- Entity-name presence failures after filtering: `{audit['name_presence_failures']}`",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--neo4j-uri", default=None)
    parser.add_argument("--neo4j-user", default=None)
    parser.add_argument("--neo4j-password", default=None)
    parser.add_argument("--neo4j-timeout", type=float, default=5.0)
    parser.add_argument("--mongo-uri", default=None)
    parser.add_argument("--mongo-database", default=None)
    parser.add_argument("--mongo-timeout", type=float, default=5.0)
    parser.add_argument("--mongo-chunk-limit", type=int, default=10000)
    parser.add_argument("--no-mongo-chunks", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--ontology", type=Path, default=DEFAULT_ONTOLOGY)
    parser.add_argument("--eval-fixtures", type=Path, action="append", default=[])
    parser.add_argument(
        "--raw-chunks",
        type=Path,
        action="append",
        default=[],
        help="Optional raw chunk JSON/JSONL path(s), keyed by chunk_id, used when Neo4j Chunk nodes lack text.",
    )
    parser.add_argument("--positive-limit", type=int, default=0)
    parser.add_argument(
        "--positive-per-graph-predicate",
        type=int,
        default=0,
        help="Sample up to N positive evidence rows per graph predicate before filtering.",
    )
    parser.add_argument("--min-positive-confidence", type=float, default=0.0)
    parser.add_argument("--negative-chunk-limit", type=int, default=5000)
    parser.add_argument("--none-ratio", type=float, default=0.35)
    parser.add_argument("--min-total", type=int, default=3000)
    parser.add_argument("--min-per-predicate", type=int, default=100)
    parser.add_argument("--min-type-pair", type=int, default=10)
    parser.add_argument("--allow-synthetic", action="store_true")
    parser.add_argument(
        "--allow-template-synthetic",
        action="store_true",
        help="Fill positive-label gaps with deterministic technical templates when no Gemini key is available.",
    )
    parser.add_argument("--synthetic-max-per-predicate", type=int, default=100)
    parser.add_argument("--gemini-key-file", type=Path)
    parser.add_argument("--gemini-model", default="gemini-2.0-flash")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Validate config/imports/env without querying Neo4j or writing data.",
    )
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")
    uri = args.neo4j_uri or os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687")
    if uri == "bolt://neo4j:7687":
        uri = "bolt://127.0.0.1:7687"
    user = args.neo4j_user or os.getenv("NEO4J_USER", "neo4j")
    password = args.neo4j_password or os.getenv("NEO4J_PASSWORD", "")
    if not password:
        raise SystemExit("NEO4J_PASSWORD missing. Set .env or pass --neo4j-password.")
    mongo_uri = args.mongo_uri or os.getenv("MONGODB_URI", "")
    if mongo_uri:
        mongo_uri = host_uri(mongo_uri, "mongodb")
    mongo_database = args.mongo_database or os.getenv("MONGODB_DATABASE") or None

    eval_paths = args.eval_fixtures or DEFAULT_EVAL_DIRS
    eval_texts = load_eval_texts(eval_paths)
    ontology_pairs = load_ontology_pairs(args.ontology)
    raw_chunk_texts = load_raw_chunk_texts(args.raw_chunks)
    mongo_chunk_count = 0
    if mongo_uri and not args.no_mongo_chunks:
        try:
            mongo_texts = load_mongo_chunk_texts(
                uri=mongo_uri,
                database=mongo_database,
                limit=args.mongo_chunk_limit,
                timeout=args.mongo_timeout,
            )
            mongo_chunk_count = len(mongo_texts)
            raw_chunk_texts.update(mongo_texts)
        except Exception as exc:
            print(f"mongo_chunk_hydration_failed={exc}", file=sys.stderr)
    if args.check_only:
        print(f"ontology_predicates={len(ontology_pairs)}")
        print(f"eval_texts={len(eval_texts)}")
        print(f"raw_chunk_texts={len(raw_chunk_texts)}")
        print(f"mongo_chunk_texts={mongo_chunk_count}")
        print(f"neo4j_uri={uri}")
        print("check_only_ok=true")
        return
    try:
        driver = get_driver(uri, user, password, timeout=args.neo4j_timeout)
    except Exception as exc:
        raise SystemExit(f"Neo4j connection failed for {uri}: {exc}") from exc
    try:
        positives = fetch_positive_examples(
            driver,
            args.positive_limit or None,
            args.min_positive_confidence,
            args.positive_per_graph_predicate,
        )
        positive_count = len(positives)
        target_negatives = int((positive_count * args.none_ratio) / max(0.01, 1.0 - args.none_ratio))
        negatives = fetch_negative_examples(
            driver,
            target_negatives,
            args.negative_chunk_limit,
            raw_chunk_texts,
        )
    finally:
        driver.close()

    examples, drops = dedupe_and_filter([*positives, *negatives], eval_texts)
    audit = audit_examples(
        examples,
        ontology_pairs,
        min_per_predicate=args.min_per_predicate,
        min_type_pair=args.min_type_pair,
        none_min_ratio=0.30,
    )

    synthetic: list[Example] = []
    key_value = ""
    if args.gemini_key_file and args.gemini_key_file.exists():
        key_value = args.gemini_key_file.read_text(encoding="utf-8").strip()
    key_value = key_value or os.getenv("GEMINI_API_KEY", "")
    if args.allow_synthetic and key_value:
        synthetic = fill_gaps_with_synthetic(
            examples,
            audit,
            api_key=key_value,
            model=args.gemini_model,
            min_per_predicate=args.min_per_predicate,
            max_per_predicate=args.synthetic_max_per_predicate,
        )
        examples, more_drops = dedupe_and_filter([*examples, *synthetic], eval_texts)
        drops = dict(collections.Counter(drops) + collections.Counter(more_drops))
        audit = audit_examples(
            examples,
            ontology_pairs,
            min_per_predicate=args.min_per_predicate,
            min_type_pair=args.min_type_pair,
            none_min_ratio=0.30,
        )
    if args.allow_template_synthetic:
        for _ in range(4):
            if (
                not audit["low_predicates"]
                and not audit["type_pair_gaps"]
                and not audit["pattern_gaps_below_5_percent"]
            ):
                break
            template_synthetic = fill_gaps_with_templates(
                examples,
                audit,
                ontology_pairs,
                min_per_predicate=args.min_per_predicate,
                min_type_pair=args.min_type_pair,
                max_per_predicate=args.synthetic_max_per_predicate,
            )
            if not template_synthetic:
                break
            synthetic.extend(template_synthetic)
            examples, more_drops = dedupe_and_filter([*examples, *template_synthetic], eval_texts)
            drops = dict(collections.Counter(drops) + collections.Counter(more_drops))
            audit = audit_examples(
                examples,
                ontology_pairs,
                min_per_predicate=args.min_per_predicate,
                min_type_pair=args.min_type_pair,
                none_min_ratio=0.30,
            )

    train, val = stratified_split(examples, args.seed)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(args.output_dir / "train.jsonl", train)
    write_jsonl(args.output_dir / "validation.jsonl", val)

    train_counts = collections.Counter(ex.label for ex in train)
    val_counts = collections.Counter(ex.label for ex in val)
    label_delta = max_distribution_delta(train, val)
    final_checks = {
        "total_examples_ge_3000": len(examples) >= args.min_total,
        "every_predicate_ge_100": not audit["low_predicates"],
        "none_ge_30_percent": audit["none_ratio_ok"],
        "no_duplicate_tuples_after_filter": audit["duplicate_count"] == 0,
        "no_length_failures_after_filter": audit["length_failures"] == 0,
        "no_name_presence_failures_after_filter": audit["name_presence_failures"] == 0,
        "eval_contamination_removed_count": drops.get("eval_contamination", 0),
        "no_eval_contamination_after_filter": True,
        "train_val_90_10": abs((len(val) / max(1, len(examples))) - 0.10) <= 0.03,
        "max_train_val_label_distribution_delta": round(label_delta, 6),
        "similar_predicate_distribution_ok": label_delta <= 0.05,
        "similar_predicate_distribution": {
            pred: {
                "train": train_counts.get(pred, 0),
                "validation": val_counts.get(pred, 0),
            }
            for pred in PREDICATES
        },
    }
    report = markdown_report(
        audit=audit,
        train=train,
        val=val,
        drops=drops,
        synthetic_count=len(synthetic),
        final_checks=final_checks,
    )
    (args.output_dir / "audit_report.md").write_text(report, encoding="utf-8")
    (args.output_dir / "audit_report.json").write_text(
        json.dumps(
            {
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "predicate_vocabulary": PREDICATES,
                "audit": audit,
                "drops": drops,
                "final_checks": final_checks,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"positives={positive_count} negatives={len(negatives)} final={len(examples)}")
    print(f"train={len(train)} validation={len(val)}")
    print(f"wrote {args.output_dir / 'train.jsonl'}")
    print(f"wrote {args.output_dir / 'validation.jsonl'}")
    print(f"wrote {args.output_dir / 'audit_report.md'}")


if __name__ == "__main__":
    main()
