import asyncio
import os

from motor.motor_asyncio import AsyncIOMotorClient
from services.ingestion.alias_candidates import collect_alias_candidates
from services.ingestion.alias_gate import run_alias_gate
from services.ingestion.enrich import schwartz_hearst_matches


async def main() -> None:
    cid = "8bf57c76-7e2d-49eb-9a11-6e260406903f"
    db = AsyncIOMotorClient(os.environ["MONGODB_URI"]).get_default_database()
    ch = await db.chunks.find_one(
        {
            "corpus_id": cid,
            "text": {"$regex": "Retrieval-Augmented Generation \\(RAG\\)"},
        }
    )
    print("FOUND", bool(ch))
    if not ch:
        ch = await db.chunks.find_one(
            {"corpus_id": cid, "text": {"$regex": "Retrieval-Augmented"}}
        )
        print("FALLBACK", bool(ch), (ch or {}).get("chunk_id"))
    text = ch["text"]
    print("TEXT", text[:300])
    matches = schwartz_hearst_matches(text)
    print("MATCHES", matches)
    ents = []
    for m in matches:
        ents.append({"canonical_name": m["long_form"], "surface_form": m["long_form"]})
        ents.append({"canonical_name": m["short"], "surface_form": m["short"]})
    batch = collect_alias_candidates(
        text, ents, document_id=ch["doc_id"], chunk_id=ch["chunk_id"]
    )
    print(
        "CANDS",
        [
            (c.candidate_type, c.canonical_surface, c.candidate_surface, c.source_method)
            for c in batch.candidates
        ],
    )
    print(
        "INCOMPLETE",
        [(i.reason, i.canonical_surface, i.candidate_surface) for i in batch.incomplete],
    )
    gate = run_alias_gate(batch.candidates, incomplete=batch.incomplete)
    for d in gate.decisions:
        c = next(x for x in batch.candidates if x.alias_candidate_id == d.alias_candidate_id)
        print(
            "DEC",
            d.decision,
            d.decision_reason,
            c.canonical_surface,
            "->",
            c.candidate_surface,
        )


if __name__ == "__main__":
    asyncio.run(main())
