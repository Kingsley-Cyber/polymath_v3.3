"""Per-corpus domain vocabularies (owner-ordered 2026-08-12).

Measured basis. Handing a zero-shot span encoder the abstract 15-class
global schema produced 98.7% `other` on real content, and it proposed junk
spans like "In this chapter" as entities. The same sentence under concrete
domain labels:

    Python   -> Software 0.98        ->  Programming Language 1.00
    Pygame   -> Software 0.99        ->  Software Library     1.00
    Django   -> Software 0.99        ->  Software Framework   1.00
    print    -> Method   0.90        ->  Function             0.96
    dictionary -> (missed)           ->  Data Structure       1.00
    "In this chapter" -> other 0.44  ->  (not proposed at all)

Concrete labels do not merely type better; they suppress junk proposals.

The corpora are coherent thematic libraries whose NAMES are legacy:
  markbuildsbrands-transcripts -> consumer psychology, behaviour, marketing
  video-generation-school      -> film production, VFX, cinematography, anatomy
  cybersecurity-study          -> IT certification, cloud, security, data eng

Relation vocabularies stay close to the global predicate set so the
compiler's ontology signatures keep applying; entity vocabularies are
domain-specific. Types outside the global ontology route to REVIEW rather
than being rejected (see extraction_compiler).

Run:  docker exec polymath_v33-backend-1 python /app/scripts/apply_domain_schemas.py
"""
import asyncio
import sys
from datetime import datetime

sys.path.insert(0, "/app")

CORE_RELATIONS = [
    "part_of", "member_of", "located_in", "created_by", "owns", "uses",
    "instance_of", "produces", "consumes", "depends_on", "supports",
    "defines", "causes", "derived_from", "references", "implements",
    "contradicts", "preceded_by", "related_to",
]

SCHEMAS: dict[str, dict[str, list[str]]] = {
    "markbuildsbrands-transcripts": {
        "entity_schema": [
            "Person", "Organization", "Brand", "Product",
            "Consumer Segment", "Psychological Construct", "Cognitive Bias",
            "Behavior", "Emotion", "Motivation", "Research Study",
            "Research Method", "Theory", "Marketing Strategy", "Metric",
            "Market", "Publication",
        ],
        "relation_schema": CORE_RELATIONS + ["influences", "measured_by", "studied_in"],
    },
    "video-generation-school": {
        "entity_schema": [
            "Shot Type", "Camera Movement", "Camera Angle", "Lighting Technique",
            "Editing Technique", "Visual Effect", "Sound Technique",
            "Production Role", "Equipment", "Software", "Film", "Person",
            "Organization", "Anatomical Structure", "Facial Action",
            "Composition Principle", "Narrative Device",
        ],
        "relation_schema": CORE_RELATIONS + ["performed_by", "achieved_with", "composed_of"],
    },
    "cybersecurity-study": {
        "entity_schema": [
            "Protocol", "Network Service", "Cloud Service", "Cloud Provider",
            "Vulnerability", "Attack Technique", "Security Control",
            "Software", "Programming Language", "Software Library",
            "Command", "Configuration Setting", "Data Structure",
            "Certification", "Standard", "Organization", "Person",
            "Database", "Infrastructure Component",
        ],
        "relation_schema": CORE_RELATIONS + ["mitigates", "exploits", "configured_by", "runs_on"],
    },
}


async def main() -> None:
    from services.conversation import conversation_service

    await conversation_service.connect()
    db = conversation_service._db
    for name, schema in SCHEMAS.items():
        corpus = await db["corpora"].find_one({"name": name}, {"corpus_id": 1})
        if not corpus:
            print(f"  {name}: NOT FOUND — skipped")
            continue
        await db["corpora"].update_one(
            {"corpus_id": corpus["corpus_id"]},
            {"$set": {
                "default_ingestion_config.entity_schema": schema["entity_schema"],
                "default_ingestion_config.relation_schema": schema["relation_schema"],
                "default_ingestion_config.schema_strict": "soft",
                "domain_schema_applied_at": datetime.utcnow(),
            }},
        )
        print(
            f"  {name[:34]:36} entities={len(schema['entity_schema']):3} "
            f"relations={len(schema['relation_schema']):3}"
        )
    print("applied. workers pick this up on the next claim — no restart needed.")


if __name__ == "__main__":
    asyncio.run(main())
