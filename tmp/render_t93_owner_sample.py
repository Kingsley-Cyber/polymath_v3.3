#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from scripts.semantic_gateway_ugo_canary import _interim_claim_id


CORPUS_ID = "5a20bc21-95df-42c2-80c8-f927b4e83904"
SELECTION = (
    ("0e24bdfb56579a848f7164229d04e36c34916c77ca2afd9d619e345d991d7783", 2),
    ("0c4cf7dfb7c7eb6fbb86fab09613bdb9594a4f3b6c3f467ff9254d15dda25766", 3),
    ("077f954daf38252f03aa2ce200be33f9828ea28b61b6d8bce480ecd325f1eeb5", 2),
    ("02e459af3d5105765bbca19b5077ca330f68e16ce3de6e8f096703b217204bb9", 3),
)


def _even_indices(length: int, count: int) -> list[int]:
    if count == 1:
        return [length // 2]
    indices = [round(index * (length - 1) / (count - 1)) for index in range(count)]
    if len(set(indices)) != count:
        raise RuntimeError("sample selection did not produce unique indices")
    return indices


def _quote(text: str) -> list[str]:
    lines = text.splitlines() or [text]
    return ["> " + line if line else ">" for line in lines]


def _value(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) if value else "none"
    return str(value)


def _supported_rows(
    rows: list[dict[str, Any]],
    *,
    claim_id: str,
) -> list[str]:
    rendered: list[str] = []
    for row in rows:
        claim_ids = row.get("supporting_claim_ids") or []
        if claim_ids != [claim_id]:
            raise RuntimeError("supported statement cites an unexpected claim set")
        rendered.append(
            f"- {row['text']}  \n  Evidence: `{claim_id}` (exact quote above)"
        )
    return rendered or ["- None."]


async def render(out: Path) -> dict[str, Any]:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        try:
            db = client.get_default_database()
        except Exception:
            db = client[settings.MONGODB_DATABASE]

        accepted_count = await db["semantic_digest_jobs"].count_documents(
            {"corpus_id": CORPUS_ID, "status": "succeeded"}
        )
        if accepted_count != 66:
            raise RuntimeError(f"expected 66 accepted digests, found {accepted_count}")

        output: list[str] = [
            "# T9.3 Mark digest sample for owner review",
            "",
            "This is a zero-new-spend quality review of 10 already-purchased, accepted semantic digests across four source documents. Documents are ordered from the smallest accepted-document packet profile to the largest. Within each document, parents are selected deterministically at evenly spaced positions after sorting by `(packet_bytes, ordinal)`, yielding compact, middle, and large packets where available.",
            "",
            "The paid-pass packet contract contains one interim supporting claim per parent: the exact validated parent text. To keep the report readable, that quote appears once in each parent section; every proposal still names its supporting claim ID and points to that exact quote. No polarity field exists in this interim claim contract, so polarity is reported as not encoded rather than inferred. Conditions and exceptions are reproduced from the accepted digest where present.",
            "",
            "Quality context: 66 accepted digests exist before sampling; this report performs no provider call, retry, canonical projection, or Phase-2 materialization.",
            "",
        ]

        selected_parent_count = 0
        selected_packet_bytes: list[int] = []
        proposal_counts = {"domain": 0, "frame": 0, "latent": 0, "motif": 0}
        for document_number, (doc_id, sample_count) in enumerate(SELECTION, 1):
            document = await db["documents"].find_one(
                {"corpus_id": CORPUS_ID, "doc_id": doc_id},
                {"_id": 0, "title": 1, "filename": 1},
            )
            if not document:
                raise RuntimeError(f"missing selected document {doc_id}")
            jobs = (
                await db["semantic_digest_jobs"]
                .find(
                    {"corpus_id": CORPUS_ID, "doc_id": doc_id, "status": "succeeded"},
                    {
                        "_id": 0,
                        "ordinal": 1,
                        "packet_bytes": 1,
                        "parent_id": 1,
                        "cache_key": 1,
                        "accepted_cache_key": 1,
                    },
                )
                .sort([("packet_bytes", 1), ("ordinal", 1)])
                .to_list(length=None)
            )
            if len(jobs) < sample_count:
                raise RuntimeError(f"document {doc_id} lacks {sample_count} accepted jobs")
            indices = _even_indices(len(jobs), sample_count)
            chosen = [jobs[index] for index in indices]
            output.extend(
                [
                    f"## Document {document_number}: {document.get('title') or document.get('filename')}",
                    "",
                    f"Source file: `{document.get('filename')}`  ",
                    f"Accepted parents in document: {len(jobs)}  ",
                    "Selected packet sizes: "
                    + ", ".join(f"{int(job['packet_bytes']):,} bytes" for job in chosen),
                    "",
                ]
            )

            for parent_number, job in enumerate(chosen, 1):
                parent = await db["parent_chunks"].find_one(
                    {"corpus_id": CORPUS_ID, "parent_id": job["parent_id"]},
                    {
                        "_id": 0,
                        "parent_id": 1,
                        "source_hash": 1,
                        "text": 1,
                        "heading_path": 1,
                    },
                )
                cache_id = job.get("accepted_cache_key") or job.get("cache_key")
                cache = await db["semantic_digest_cache"].find_one(
                    {"_id": cache_id},
                    {"_id": 0, "digest": 1, "provenance": 1, "canonical_write": 1},
                )
                if not parent or not cache:
                    raise RuntimeError(f"missing parent/cache for ordinal {job['ordinal']}")
                digest = cache.get("digest") or {}
                provenance = cache.get("provenance") or {}
                if digest.get("parent_id") != job["parent_id"]:
                    raise RuntimeError("digest parent identity mismatch")
                if cache.get("canonical_write") is not False:
                    raise RuntimeError("sample cache row is not noncanonical")
                claim_id = _interim_claim_id(job["parent_id"], parent.get("source_hash"))
                parent_text = str(parent.get("text") or "")
                if not parent_text:
                    raise RuntimeError("sample parent has empty supporting claim")

                all_proposals = (
                    list(digest.get("domain_proposals") or [])
                    + list(digest.get("frame_proposals") or [])
                    + list(digest.get("latent_concepts") or [])
                    + list(digest.get("motif_proposals") or [])
                )
                for proposal in all_proposals:
                    if proposal.get("supporting_claim_ids") != [claim_id]:
                        raise RuntimeError("proposal cites an unexpected claim set")

                selected_parent_count += 1
                selected_packet_bytes.append(int(job["packet_bytes"]))
                proposal_counts["domain"] += len(digest.get("domain_proposals") or [])
                proposal_counts["frame"] += len(digest.get("frame_proposals") or [])
                proposal_counts["latent"] += len(digest.get("latent_concepts") or [])
                proposal_counts["motif"] += len(digest.get("motif_proposals") or [])
                heading = " / ".join(str(x) for x in parent.get("heading_path") or [])
                output.extend(
                    [
                        f"### Parent {document_number}.{parent_number} — ordinal {job['ordinal']}",
                        "",
                        f"Packet size: {int(job['packet_bytes']):,} bytes  ",
                        f"Heading path: {heading or 'none'}  ",
                        f"Accepted contract: `{provenance.get('prompt_version')}` / `{provenance.get('repair_prompt_version') or 'none'}`  ",
                        f"Model: `{provenance.get('model_id')}`  ",
                        f"Supporting claim ID: `{claim_id}`  ",
                        "Polarity: not encoded in the interim claim contract.",
                        "",
                        "#### Summary",
                        "",
                        str(digest.get("summary") or ""),
                        "",
                        "#### Central thesis",
                        "",
                        str(digest.get("central_thesis") or ""),
                        "",
                        "#### Exact supporting-claim quote",
                        "",
                        *_quote(parent_text),
                        "",
                        "#### Domain proposals",
                        "",
                    ]
                )
                domains = digest.get("domain_proposals") or []
                output.extend(
                    [
                        f"- `{row['registry_id']}` — {row['proposed_label']} (role: {row['role']}; state: {row['assignment_state']})  \n  Evidence: `{claim_id}` (exact quote above)"
                        for row in domains
                    ]
                    or ["- None."]
                )
                output.extend(["", "#### Frame proposals", ""])
                frames = digest.get("frame_proposals") or []
                output.extend(
                    [
                        f"- `{row['frame_id']}` (role: {row['role']}; state: {row['assignment_state']}) — {row['explanation']}  \n  Evidence: `{claim_id}` (exact quote above)"
                        for row in frames
                    ]
                    or ["- None."]
                )
                output.extend(["", "#### Latent-concept proposals", ""])
                latent = digest.get("latent_concepts") or []
                output.extend(
                    [
                        f"- {row['preferred_label']} (state: {row['assignment_state']}) — {row['definition']}  \n  Aliases: {_value(row.get('aliases'))}.  \n  Evidence: `{claim_id}` (exact quote above)"
                        for row in latent
                    ]
                    or ["- None."]
                )
                output.extend(["", "#### Motif proposals", ""])
                motifs = digest.get("motif_proposals") or []
                output.extend(
                    [
                        f"- {row['proposed_label']} — frames: {_value(row.get('frame_sequence'))}; abstract sequence: {_value(row.get('abstract_sequence'))}.  \n  Evidence: `{claim_id}` (exact quote above)"
                        for row in motifs
                    ]
                    or ["- None."]
                )
                output.extend(["", "#### Conditions", ""])
                output.extend(
                    _supported_rows(
                        digest.get("conditions") or [],
                        claim_id=claim_id,
                    )
                )
                output.extend(["", "#### Exceptions", ""])
                output.extend(
                    _supported_rows(
                        digest.get("exceptions") or [],
                        claim_id=claim_id,
                    )
                )
                output.extend(["", "#### Unresolved interpretations", ""])
                unresolved = digest.get("unresolved_interpretations") or []
                output.extend([f"- {value}" for value in unresolved] or ["- None."])
                output.append("")

        if selected_parent_count != 10:
            raise RuntimeError(f"expected 10 selected parents, found {selected_parent_count}")
        output.extend(
            [
                "## Sample receipt",
                "",
                f"- Accepted digests available: {accepted_count}",
                f"- Documents sampled: {len(SELECTION)}",
                f"- Parents sampled: {selected_parent_count}",
                f"- Selected packet-byte range: {min(selected_packet_bytes):,}–{max(selected_packet_bytes):,}",
                f"- Proposal counts: domains={proposal_counts['domain']}, frames={proposal_counts['frame']}, latent={proposal_counts['latent']}, motifs={proposal_counts['motif']}",
                "- New provider calls: 0",
                "- Canonical writes: 0",
                "- Phase-2 jobs materialized: 0",
                "",
            ]
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(output), encoding="utf-8")
        return {
            "accepted_count": accepted_count,
            "documents": len(SELECTION),
            "parents": selected_parent_count,
            "packet_bytes_min": min(selected_packet_bytes),
            "packet_bytes_max": max(selected_packet_bytes),
            "proposal_counts": proposal_counts,
        }
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    receipt = asyncio.run(render(args.out))
    print(receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
