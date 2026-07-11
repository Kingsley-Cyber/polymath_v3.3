"""§10.1 semantic summary contract tests (pure)."""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from services.ingestion.summary_semantics import (
    canonical_parent_summary_fields,
    parse_semantic_summary,
    repair_parent_summary_row,
    topic_key_for,
)

def test_full_parse_clamps():
    raw = '{"summary":"S.","domain":"Programming Languages","semantic_chunk_type":"HOW-TO","key_terms":["XPath","XPath","xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"],"mechanisms":["Feedback Loop!","feedback  loop","a","b","c","d","e"]}'
    o = parse_semantic_summary(raw)
    assert o["summary"] == "S." and o["domain"] == "programming_languages"
    assert o["semantic_chunk_type"] == "narrative"          # unknown enum → clamp
    assert o["key_terms"] == ["XPath"]                       # dedupe + length cap
    assert o["mechanisms"][:1] == ["feedback_loop"] and len(o["mechanisms"]) <= 5

def test_valid_enum_kept():
    assert parse_semantic_summary('{"summary":"x","semantic_chunk_type":"procedure"}')["semantic_chunk_type"] == "procedure"

def test_junk_never_fabricates():
    o = parse_semantic_summary("just prose, no json")
    assert o["summary"] == "just prose, no json"
    assert o["semantic_chunk_type"] is None and o["key_terms"] == [] and o["mechanisms"] == []

def test_truncated_json_salvages_clean_summary():
    raw = '{"summary":"SQLite stores relational data in a compact local database. It supports SQL queries, indexes, and transactions for embedded applications.","central_claim":"SQLite is a compact embedded relational database.","key_points":['
    o = parse_semantic_summary(
        raw,
        source_child_ids=["child_1"],
        source_text="SQLite stores relational data in a compact local database.",
    )
    assert o["summary"].startswith("SQLite stores relational data")
    assert not o["summary"].lstrip().startswith("{")
    assert o["repair_status"] == "repaired"
    assert o["validation_status"] == "valid"

def test_reasoning_preamble_does_not_swallow_summary_artifact():
    raw = (
        '<think>Evaluate {"format":"parent_summary"} before answering.</think>\n'
        '```json\n{"summary":"The passage explains how durable queues prevent duplicate provider work while preserving validated artifacts for retrieval.",'
        '"central_claim":"Durable queues prevent duplicate provider work.",'
        '"key_points":[{"point":"Validated artifacts remain reusable.","supporting_child_ids":["child_1"]}]}\n```'
    )
    parsed = parse_semantic_summary(raw, source_child_ids=["child_1"])
    assert parsed["summary"].startswith("The passage explains")
    assert parsed["validation_status"] == "valid"

def test_unrepairable_raw_json_is_quarantined():
    o = parse_semantic_summary('{"central_claim": {"bad": true}', source_child_ids=["child_1"])
    assert o["summary"] == ""
    assert o["validation_status"] == "quarantined"
    assert o["repair_status"] == "quarantined"

def test_canonical_parent_summary_fields_attach_deterministic_metadata():
    parsed = parse_semantic_summary(
        '{"summary":"Parent summaries are compiled retrieval artifacts. They preserve central claims and key evidence for later hydration.","central_claim":"Parent summaries are compiled retrieval artifacts.","concept_tags":["parent summaries","retrieval artifacts"],"key_points":[{"point":"Child evidence is preserved.","supporting_child_ids":["child_1"]}],"retrieval_uses":["evidence"],"abstraction_level":"medium"}',
        source_child_ids=["child_1"],
        source_text="Parent summaries are compiled retrieval artifacts. Child evidence is preserved.",
    )
    fields = canonical_parent_summary_fields(
        parsed,
        parent_id="parent_1",
        doc_id="doc_1",
        corpus_id="corpus_1",
        source_text="Parent summaries are compiled retrieval artifacts. Child evidence is preserved.",
        source_child_ids=["child_1"],
        summary_model="unit-model",
    )
    assert fields["summary_id"].startswith("sum_parent_")
    assert len(fields["source_hash"]) == 64
    assert fields["summary_model"] == "unit-model"
    assert fields["validation_status"] == "valid"
    assert "Parent summaries are compiled" in fields["retrieval_text"]

def test_repair_parent_summary_row_removes_raw_json_summary():
    row = {
        "corpus_id": "corpus_1",
        "doc_id": "doc_1",
        "parent_id": "parent_1",
        "source_child_ids": ["child_1"],
        "text": "SQLite stores relational data in a compact local database.",
        "summary": '{"summary":"SQLite stores relational data in a compact local database. It supports SQL queries and transactions.","central_claim":"SQLite is compact.","key_points":[',
    }
    fixed = repair_parent_summary_row(row)
    assert fixed["summary"].startswith("SQLite stores relational data")
    assert not fixed["summary"].lstrip().startswith("{")
    assert fixed["repair_status"] == "repaired"
    assert fixed["validation_status"] == "valid"
    assert fixed["retrieval_text"]

def test_topic_key_deterministic():
    assert topic_key_for("xml", ["XPath Basics", "sub"]) == "xml.xpath_basics"
    assert topic_key_for(None, ["Head"]) == "head"
    assert topic_key_for("d", None) == "d"
    assert topic_key_for(None, None) is None

if __name__ == "__main__":
    fails = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_"):
            try: v(); print("PASS", k)
            except Exception as e: fails += 1; print("FAIL", k, repr(e))
    sys.exit(1 if fails else 0)
