#!/usr/bin/env python3
"""Dependency-path pattern mining script.

Mines existing claims from ghost_b_extractions to discover which spaCy
dependency-path signatures occur most frequently. Produces a frequency-ranked
rule backlog report that drives the DependencyMatcher pattern priority.

Usage:
    # Inside backend container:
    python scripts/mine_dependency_patterns.py --sample 50000

    # Locally (requires pymongo in .tmp_pkgs):
    PYTHONPATH=.tmp_pkgs python backend/scripts/mine_dependency_patterns.py --sample 50000

Output:
    docs/baselines/DEPENDENCY_PATTERN_MINING_REPORT.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — allow running from repo root or backend/
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _REPO_ROOT / "backend"
sys.path.insert(0, str(_BACKEND))
sys.path.insert(0, str(_REPO_ROOT / ".tmp_pkgs"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dependency path utilities (self-contained, no import from services)
# ---------------------------------------------------------------------------


def find_head_token(doc, start_char: int, end_char: int):
    """Find the syntactic head token for an entity span."""
    span_tokens = [tok for tok in doc if tok.idx >= start_char and tok.idx + len(tok.text) <= end_char]
    if not span_tokens:
        span_tokens = [tok for tok in doc if start_char <= tok.idx < end_char]
    if not span_tokens:
        return None
    best = span_tokens[0]
    for tok in span_tokens[1:]:
        if tok.dep_ == "ROOT":
            return tok
        if best.head == tok:
            best = tok
    if best.dep_ in ("det", "amod", "compound", "nummod") and best.head.i != best.i:
        best = best.head
    return best


def shortest_dep_path(doc, tok_a, tok_b):
    """BFS shortest path on undirected dependency tree."""
    from collections import deque

    if tok_a.i == tok_b.i:
        return []
    n = len(doc)
    adj = [[] for _ in range(n)]
    for tok in doc:
        if tok.head.i != tok.i:
            adj[tok.i].append((tok.head.i, tok.dep_, "up"))
            adj[tok.head.i].append((tok.i, tok.dep_, "down"))

    visited = [False] * n
    parent = [None] * n
    queue = deque([tok_a.i])
    visited[tok_a.i] = True

    while queue:
        curr = queue.popleft()
        if curr == tok_b.i:
            break
        for neighbor, dep, direction in adj[curr]:
            if not visited[neighbor]:
                visited[neighbor] = True
                parent[neighbor] = (curr, dep, direction)
                queue.append(neighbor)

    if not visited[tok_b.i]:
        return []
    path = []
    node = tok_b.i
    while parent[node] is not None:
        prev, dep, direction = parent[node]
        path.append((dep, direction, node))
        node = prev
    path.reverse()
    return path


def build_signature(path, doc, tok_a, tok_b) -> str:
    """Convert dependency path to normalized signature."""
    if not path:
        return "ENTITY_A --same-- ENTITY_B"
    parts = ["ENTITY_A"]
    for dep, direction, token_idx in path:
        tok = doc[token_idx]
        if tok.pos_ == "VERB" and tok.i != tok_b.i:
            node_label = "VERB"
        elif tok.i == tok_b.i:
            node_label = "ENTITY_B"
        else:
            node_label = tok.dep_.upper()
        if direction == "up":
            parts.append(f"<-{dep}-")
        else:
            parts.append(f"-{dep}->")
        parts.append(node_label)
    if parts[-1] != "ENTITY_B":
        parts.append("ENTITY_B")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Mining logic
# ---------------------------------------------------------------------------


@dataclass
class SignatureAccumulator:
    """Accumulates stats for a single dependency-path signature."""

    frequency: int = 0
    examples: list[str] = field(default_factory=list)
    relations: Counter = field(default_factory=Counter)

    def add(self, sentence: str, predicate: str):
        self.frequency += 1
        if len(self.examples) < 5:
            self.examples.append(sentence)
        self.relations[predicate] += 1


def load_supported_patterns() -> set[str]:
    """Load the supported patterns registry."""
    import yaml

    path = _REPO_ROOT / "config" / "supported_dep_patterns.yaml"
    if not path.exists():
        return set()
    data = yaml.safe_load(path.read_text()) or {}
    return set(data.get("supported") or [])


def mine_claims(sample_size: int, batch_size: int = 500) -> dict:
    """Mine claims from MongoDB and produce the pattern report."""
    import spacy
    from config import get_settings
    import motor.motor_asyncio
    import asyncio

    settings = get_settings()
    supported = load_supported_patterns()

    logger.info("Loading spaCy model...")
    nlp = spacy.load("en_core_web_sm", disable=["ner", "textcat"])
    if "lemmatizer" not in nlp.pipe_names:
        nlp.add_pipe("lemmatizer")

    accumulators: dict[str, SignatureAccumulator] = defaultdict(SignatureAccumulator)
    total_claims = 0
    total_parsed = 0
    skipped_no_args = 0
    skipped_no_path = 0

    async def _fetch_claims():
        """Fetch claims from MongoDB in batches."""
        client = motor.motor_asyncio.AsyncIOMotorClient(settings.MONGODB_URI)
        db = client[settings.MONGODB_DATABASE]
        cursor = db["ghost_b_extractions"].find(
            {"claim_compilation.claims.0": {"$exists": True}},
            {"claim_compilation.claims": 1, "text": 1},
        ).limit(sample_size)
        docs = await cursor.to_list(sample_size)
        client.close()
        return docs

    logger.info("Fetching up to %d extraction docs from MongoDB...", sample_size)
    extraction_docs = asyncio.run(_fetch_claims())
    logger.info("Fetched %d docs. Extracting claims...", len(extraction_docs))

    # Flatten claims with their source text
    claim_batch: list[tuple[str, list[dict]]] = []  # (proposition_text, arguments)
    for doc_record in extraction_docs:
        cc = doc_record.get("claim_compilation") or {}
        claims = cc.get("claims") or []
        for claim in claims:
            args = claim.get("arguments") or []
            prop_text = claim.get("proposition_text") or ""
            if len(args) >= 2 and prop_text.strip():
                # Check that args have char offsets
                has_offsets = all(
                    a.get("start_char") is not None and a.get("end_char") is not None
                    for a in args[:2]
                )
                if has_offsets:
                    claim_batch.append((prop_text, args[:2], claim.get("normalized_predicate", "UNKNOWN")))
                    total_claims += 1

    logger.info("Total claims with 2+ args and offsets: %d", total_claims)

    # Parse in batches with spaCy
    texts = [c[0] for c in claim_batch]
    logger.info("Parsing %d sentences with spaCy (batch_size=%d)...", len(texts), batch_size)
    t0 = time.time()

    parsed_docs = list(nlp.pipe(texts, batch_size=batch_size))
    parse_time = time.time() - t0
    logger.info("Parsed in %.1fs (%.0f sentences/sec)", parse_time, len(texts) / max(parse_time, 0.1))

    # Extract dependency paths
    for idx, (doc, (_, args, predicate)) in enumerate(zip(parsed_docs, claim_batch)):
        subj_arg, obj_arg = args[0], args[1]
        subj_start = subj_arg.get("start_char", 0)
        subj_end = subj_arg.get("end_char", 0)
        obj_start = obj_arg.get("start_char", 0)
        obj_end = obj_arg.get("end_char", 0)

        tok_a = find_head_token(doc, subj_start, subj_end)
        tok_b = find_head_token(doc, obj_start, obj_end)

        if tok_a is None or tok_b is None:
            skipped_no_args += 1
            continue
        if tok_a.i == tok_b.i:
            skipped_no_args += 1
            continue

        path = shortest_dep_path(doc, tok_a, tok_b)
        if not path:
            skipped_no_path += 1
            continue

        signature = build_signature(path, doc, tok_a, tok_b)
        sentence = doc.text.strip()[:200]

        accumulators[signature].add(sentence, predicate)
        total_parsed += 1

    logger.info(
        "Mining complete: %d signatures from %d parsed claims "
        "(skipped: %d no-args, %d no-path)",
        len(accumulators), total_parsed, skipped_no_args, skipped_no_path,
    )

    # Build report
    signatures_report = []
    for sig, acc in sorted(accumulators.items(), key=lambda x: -x[1].frequency):
        top_relations = dict(acc.relations.most_common(10))
        total_rel = sum(acc.relations.values())
        top_count = acc.relations.most_common(1)[0][1] if acc.relations else 0
        concentration = round(top_count / total_rel, 3) if total_rel > 0 else 0.0

        signatures_report.append({
            "signature": sig,
            "frequency": acc.frequency,
            "already_supported": sig in supported,
            "example_sentences": acc.examples[:5],
            "observed_relations": top_relations,
            "relation_concentration": concentration,
        })

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_extraction_docs": len(extraction_docs),
        "total_claims_with_offsets": total_claims,
        "total_claims_parsed": total_parsed,
        "skipped_no_args": skipped_no_args,
        "skipped_no_path": skipped_no_path,
        "unique_signatures": len(accumulators),
        "parse_time_seconds": round(parse_time, 2),
        "signatures": signatures_report[:100],  # Top 100
    }

    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Mine dependency-path patterns from existing claims.")
    parser.add_argument("--sample", type=int, default=50000, help="Max extraction docs to process.")
    parser.add_argument("--out", type=str, default=None, help="Output JSON path.")
    parser.add_argument("--batch-size", type=int, default=500, help="spaCy pipe batch size.")
    args = parser.parse_args()

    out_path = args.out or str(_REPO_ROOT / "docs" / "baselines" / "DEPENDENCY_PATTERN_MINING_REPORT.json")

    report = mine_claims(sample_size=args.sample, batch_size=args.batch_size)

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(report, indent=2, ensure_ascii=False))
    logger.info("WROTE %s", out_path)

    # Print summary
    print(f"\n{'='*60}")
    print(f"PATTERN MINING SUMMARY")
    print(f"{'='*60}")
    print(f"Claims mined: {report['total_claims_parsed']}")
    print(f"Unique signatures: {report['unique_signatures']}")
    print(f"\nTop 15 signatures:")
    print(f"{'Signature':<55} {'Freq':>6} {'Supported':>10}")
    print(f"{'-'*55} {'-'*6} {'-'*10}")
    for sig in report["signatures"][:15]:
        supported_mark = "YES" if sig["already_supported"] else "NO"
        print(f"{sig['signature']:<55} {sig['frequency']:>6} {supported_mark:>10}")


if __name__ == "__main__":
    main()
