SPECIMEN: ghost_b system+user prompt, schema lane (verbatim from source)

_SYSTEM (ghost_b.py:123-128):
"You are a precise entity, relation, and fact extractor. Output EXACTLY one JSON object per line. Do NOT output any other text, code fences, explanations, preambles, or postambles. Extract only what is explicitly stated in the text. Do not hallucinate entities, relations, or facts."

_JSON_OBJECT_SYSTEM (ghost_b.py:130-136):
"You are a precise entity, relation, and fact extractor. Output EXACTLY one valid JSON object. Do NOT output JSONL, code fences, explanations, preambles, or postambles. Extract only what is explicitly stated in the text. Do not hallucinate entities, relations, or facts."

_GHOST_B_SCHEMA_CONTROL_HINT (ghost_b.py:139-146):
"Return one JSON object with keys entities, relations, and facts. entities[].entity_type must use the allowed vocabulary. relations[].predicate must use the allowed vocabulary and every relation must include evidence_phrase copied exactly from TEXT. facts[] must include subject, fact_type, property_name, value, confidence, and evidence_phrase when facts are enabled."

_DEFAULT_ENTITY_TYPES (ghost_b.py:145) — open lane fallback ONLY:
["person", "org", "concept", "other"]

Build path (ghost_b.py:4684-4710): build_schema_native_prompt(profile_kwargs...) → renders entity_types from LLMEntity Literal args (ghost_b.py:1802) and predicates from LLMRelation Literal args (ghost_b.py:1803) → system = _JSON_OBJECT_SYSTEM → user = rendered prompt → response_format = schema of ExtractionResponse (ghost_b.py:4705-4710).

BUG ANCHOR: the json_object prompt teaches the 4-bucket lowercase vocab while xgrammar enforces the frozen ExtractionResponse (15 Capitalized types, 31 snake_case predicates) that the prompt NEVER SHOWS (ghost_b.py:1787-1791) — small model guesses letter-by-letter under the token mask (company→Concept, place→Product), recall collapses.

