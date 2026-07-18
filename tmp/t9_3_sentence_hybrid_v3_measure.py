#!/usr/bin/env python3
"""Read-only packet-v3 size and sentence-to-atomic closure measurement."""

from __future__ import annotations

import asyncio
from collections import Counter
from decimal import Decimal
import hashlib
import json
import math
from typing import Any

from models.hash_taxonomy import canonical_json_v1, namespace_hash
from models.semantic_digest_claim_input import (
    COMPILATION_COLLECTION,
    parse_materialized_row_document,
)
from scripts.materialize_semantic_digest_claim_inputs import (
    DEFAULT_CORPUS_NAME,
    _database,
    _load_scope,
    _route_prices,
)
from scripts.semantic_gateway_ugo_canary import _packet_from_parent
from services.ingestion.paid_cost_reservation import worst_case_authority_usd


EXPECTED_ELIGIBLE_PARENTS = 795


def _quantiles(values: list[int]) -> dict[str, int]:
    ordered = sorted(values)

    def nearest_rank(fraction: float) -> int:
        index = max(0, math.ceil(len(ordered) * fraction) - 1)
        return ordered[index]

    return {
        "min": ordered[0],
        "p25": nearest_rank(0.25),
        "p50": nearest_rank(0.50),
        "p75": nearest_rank(0.75),
        "p90": nearest_rank(0.90),
        "max": ordered[-1],
    }


def _ordered_child_ids(parent: dict[str, Any]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in parent.get("child_ids") or []:
        child_id = str(value).strip()
        if child_id and child_id not in seen:
            seen.add(child_id)
            ordered.append(child_id)
    return ordered


async def main() -> int:
    client, db = await _database()
    try:
        scope = await _load_scope(
            db,
            corpus_name=DEFAULT_CORPUS_NAME,
            expected_parent_count=EXPECTED_ELIGIBLE_PARENTS,
            expected_child_count=None,
        )
        raw_compilations = await db[COMPILATION_COLLECTION].find(
            {"corpus_id": scope.corpus_id, "status": "candidate"},
        ).to_list(length=None)
        rows_by_child = {
            row.child_id: row
            for row in (
                parse_materialized_row_document(value)
                for value in raw_compilations
            )
        }
        extraction_rows = await db["ghost_b_extractions"].find(
            {
                "corpus_id": scope.corpus_id,
                "chunk_id": {"$in": scope.child_ids},
                "status": "ok",
                "schema_version": "polymath.extract.v1",
            },
            {
                "_id": 0,
                "chunk_id": 1,
                "status": 1,
                "schema_version": 1,
                "entities": 1,
            },
        ).to_list(length=None)
        extraction_by_child = {
            str(row.get("chunk_id") or ""): row for row in extraction_rows
        }

        prose_sizes: list[int] = []
        v3_naive_sizes: list[int] = []
        v3_compact_all_sizes: list[int] = []
        v3_ordered_units_sizes: list[int] = []
        v3_index_map_sizes: list[int] = []
        v3_tagged_text_sizes: list[int] = []
        sentence_counts: list[int] = []
        atomic_claim_counts: list[int] = []
        map_fanouts: list[int] = []
        missing_map_sentences = 0
        all_sentences = 0
        reasons: Counter[str] = Counter()
        packet_hashes: list[str] = []

        for parent in scope.parents:
            child_ids = _ordered_child_ids(parent)
            compilation_rows = {
                child_id: rows_by_child[child_id] for child_id in child_ids
            }
            parent_extractions = [
                extraction_by_child[child_id]
                for child_id in child_ids
                if child_id in extraction_by_child
            ]
            if any(
                not row.envelope.body.claims for row in compilation_rows.values()
            ):
                reasons["source_child_without_atomic_claim"] += 1
                continue

            prose = _packet_from_parent(
                corpus_id=scope.corpus_id,
                corpus_name=DEFAULT_CORPUS_NAME,
                parent=parent,
                extraction_rows=parent_extractions,
                max_entities=40,
            )
            extraction_entities = prose.packet["extraction_entities"]

            sentence_to_atomic: dict[str, set[str]] = {}
            for row in compilation_rows.values():
                for claim in row.envelope.body.claims:
                    for sentence_id in claim.evidence_sentence_ids:
                        sentence_to_atomic.setdefault(sentence_id, set()).add(
                            claim.claim_id
                        )

            sentence_claims: list[dict[str, Any]] = []
            compact_claims: list[dict[str, Any]] = []
            ordered_units: list[dict[str, Any]] = []
            indexed_claims: list[dict[str, Any]] = []
            tagged_sentences: list[str] = []
            parent_sentence_count = 0
            global_sentence_order = 0
            for child_order, child_id in enumerate(child_ids):
                row = compilation_rows[child_id]
                refs = sorted(
                    row.evidence_refs,
                    key=lambda item: (item.start, item.end, item.evidence_ref_id),
                )
                for sentence_order, ref in enumerate(refs):
                    all_sentences += 1
                    parent_sentence_count += 1
                    mapped_ids = sentence_to_atomic.get(ref.evidence_ref_id, set())
                    if not mapped_ids:
                        missing_map_sentences += 1
                        ordered_units.append({"text": ref.quote})
                        tagged_sentences.append(ref.quote)
                    else:
                        map_fanouts.append(len(mapped_ids))
                        ordered_units.append(
                            {"claim_id": ref.evidence_ref_id, "text": ref.quote}
                        )
                        indexed_claims.append(
                            {
                                "claim_id": ref.evidence_ref_id,
                                "sentence_index": global_sentence_order,
                            }
                        )
                        tagged_sentences.append(
                            f"[{ref.evidence_ref_id}] {ref.quote}"
                        )
                    compact_claims.append(
                        {
                            "claim_id": ref.evidence_ref_id,
                            "text": ref.quote,
                            "citation_eligible": bool(mapped_ids),
                        }
                    )
                    sentence_claims.append(
                        {
                            "claim_id": ref.evidence_ref_id,
                            "child_id": child_id,
                            "child_order": child_order,
                            "sentence_order": sentence_order,
                            "text": ref.quote,
                            "citation_eligible": bool(mapped_ids),
                        }
                    )
                    global_sentence_order += 1

            shared = {
                "packet_schema_version": (
                    "semantic_parent_packet.sentence_hybrid.v3-proposal"
                ),
                "corpus_id": scope.corpus_id,
                "corpus_name": DEFAULT_CORPUS_NAME,
                "doc_id": str(parent.get("doc_id") or ""),
                "parent_id": str(parent.get("parent_id") or ""),
                "extraction_entities": extraction_entities,
                "evidence_contract": {
                    "claims_interim": True,
                    "sentence_order": "parent_child_order_then_source_offset",
                    "provider_atomic_claims_visible": False,
                    "post_validation_mapping": (
                        "sentence_claim_id_to_local_atomic_claim_ids"
                    ),
                    "ineligible_sentence_citations_rejected": True,
                },
            }
            variants = {
                "naive": {**shared, "claims": sentence_claims},
                "compact_all": {**shared, "claims": compact_claims},
                "ordered_units": {**shared, "sentence_units": ordered_units},
                "index_map": {
                    **shared,
                    "parent_text": str(parent.get("text") or ""),
                    "claims": indexed_claims,
                },
                "tagged_text": {
                    **shared,
                    "ordered_sentence_claim_text": "\n".join(tagged_sentences),
                },
            }
            serialized = {
                key: canonical_json_v1(value) for key, value in variants.items()
            }
            v3_naive_sizes.append(len(serialized["naive"].encode("utf-8")))
            v3_compact_all_sizes.append(
                len(serialized["compact_all"].encode("utf-8"))
            )
            v3_ordered_units_sizes.append(
                len(serialized["ordered_units"].encode("utf-8"))
            )
            v3_index_map_sizes.append(len(serialized["index_map"].encode("utf-8")))
            v3_tagged_text_sizes.append(
                len(serialized["tagged_text"].encode("utf-8"))
            )
            packet_hashes.append(
                hashlib.sha256(serialized["ordered_units"].encode("utf-8")).hexdigest()
            )
            sentence_counts.append(parent_sentence_count)
            atomic_claim_counts.append(
                sum(
                    len(row.envelope.body.claims)
                    for row in compilation_rows.values()
                )
            )
            prose_sizes.append(len(canonical_json_v1(prose.packet).encode("utf-8")))

        route = _route_prices()
        price = route["price"]
        parameters = route["parameters"]
        authority_kwargs = {
            "max_output_tokens": int(parameters["max_tokens"]),
            "uncached_input_usd": Decimal(str(price["uncached_input_usd"])),
            "output_usd": Decimal(str(price["output_usd"])),
            "price_unit_tokens": int(price["price_unit_tokens"]),
        }
        receipt = {
            "schema_version": "polymath.t9_3_sentence_hybrid_measurement.v1",
            "read_only": True,
            "provider_calls": 0,
            "writes": 0,
            "eligible_parent_count": len(scope.parents),
            "packet_ready_count": len(v3_ordered_units_sizes),
            "nonready_reasons": dict(sorted(reasons.items())),
            "sentence_mapping": {
                "sentence_count": all_sentences,
                "mapped_sentence_count": all_sentences - missing_map_sentences,
                "unmapped_sentence_count": missing_map_sentences,
                "mapping_coverage": round(
                    (all_sentences - missing_map_sentences) / all_sentences, 8
                ),
                "atomic_claim_count_distribution": _quantiles(atomic_claim_counts),
                "sentence_count_distribution": _quantiles(sentence_counts),
                "mapped_atomic_fanout_distribution": _quantiles(map_fanouts),
            },
            "packet_bytes": {
                "prose_v1": _quantiles(prose_sizes),
                "sentence_hybrid_naive": _quantiles(v3_naive_sizes),
                "compact_all_sentence_claims": _quantiles(v3_compact_all_sizes),
                "ordered_units_optional_claim_id": _quantiles(
                    v3_ordered_units_sizes
                ),
                "parent_text_plus_sentence_index_map": _quantiles(
                    v3_index_map_sizes
                ),
                "tagged_ordered_sentence_text": _quantiles(v3_tagged_text_sizes),
                "over_20000_counts": {
                    "naive": sum(value > 20_000 for value in v3_naive_sizes),
                    "compact_all": sum(
                        value > 20_000 for value in v3_compact_all_sizes
                    ),
                    "ordered_units": sum(
                        value > 20_000 for value in v3_ordered_units_sizes
                    ),
                    "index_map": sum(
                        value > 20_000 for value in v3_index_map_sizes
                    ),
                    "tagged_text": sum(
                        value > 20_000 for value in v3_tagged_text_sizes
                    ),
                },
                "ordered_units_set_hash": namespace_hash(
                    "input-set", frozenset(packet_hashes)
                ),
            },
            "two_attempt_authorities_usd": {
                "ordered_units_max_any_10": str(
                    worst_case_authority_usd(
                        packet_input_token_upper_bounds=sorted(
                            v3_ordered_units_sizes, reverse=True
                        )[:10],
                        **authority_kwargs,
                    )
                ),
                "ordered_units_all_ready": str(
                    worst_case_authority_usd(
                        packet_input_token_upper_bounds=v3_ordered_units_sizes,
                        **authority_kwargs,
                    )
                ),
                "tagged_text_max_any_10": str(
                    worst_case_authority_usd(
                        packet_input_token_upper_bounds=sorted(
                            v3_tagged_text_sizes, reverse=True
                        )[:10],
                        **authority_kwargs,
                    )
                ),
                "tagged_text_all_ready": str(
                    worst_case_authority_usd(
                        packet_input_token_upper_bounds=v3_tagged_text_sizes,
                        **authority_kwargs,
                    )
                ),
            },
            "raw_text_emitted": False,
        }
        print(json.dumps(receipt, sort_keys=True))
        return 0 if len(v3_ordered_units_sizes) == 793 else 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
