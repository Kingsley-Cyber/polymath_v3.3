"""§10.1 semantic summary contract tests (pure)."""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from services.ingestion.summary_semantics import parse_semantic_summary, topic_key_for

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
