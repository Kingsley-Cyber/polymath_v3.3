SPECIMEN: ExtractionResponse wire schema (ghost_b_schemas.py:190-201)

entities: list[LLMEntity]   — entity_type: EntityType (15 values, ghost_b_schemas.py:32)
relations: list[LLMRelation] — predicate: Predicate (31 values, ghost_b_schemas.py:53); object_kind: Literal["entity","literal"]="literal" (:150)
facts: list[LLMFact]         — fact_type: FactType (9 values, :158); subject min1 max200; property_name max80; value max500; confidence ge0 le1; evidence_phrase min1 max500

Frozen-contract note (ghost_b_schemas.py:170-180): adding a field requires coordinated updates to EntityItem/RelationItem/FactItem dataclasses, _parse(), and neo4j_writer property maps. A field added in isolation strands data: LLM emits it, _parse() ignores it, graph never sees it.

JSON schema payload: generated via model_json_schema() + _pin_all_required post-processor (ghost_b.py), schema_name "ghost_b_extraction" (ghost_b.py:4711-4715).

