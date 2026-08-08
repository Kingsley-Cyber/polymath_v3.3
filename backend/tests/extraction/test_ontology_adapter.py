from __future__ import annotations

from services.ontology_adapter.adapter_ir import AdapterIR, EntityLabelIR, PredicateIR
from services.ontology_adapter.profiler import profile_document
from services.ontology_adapter.schema_compiler import compile_adapter
from services.ontology_adapter.validator import validate_ir

ML_TEXT = """
We present a transformer encoder trained with fine-tuning on a large dataset.
Training used gpu inference benchmarks; the tokenizer and embedding layers were
evaluated with quantization. The model checkpoint achieved low latency on the
benchmark dataset. Neural training and inference costs were measured. The
diffusion decoder generates embeddings during inference. Training datasets and
benchmark metrics are reported. Abstract. Introduction. Methodology.
Conclusion. References [1] [2] [3] et al. Figure 1. Table 2.
# Results
# Analysis
# Discussion
# Evaluation
# Appendix
"""


def test_profiler_is_deterministic_and_gold_blind() -> None:
    a, b = profile_document(ML_TEXT), profile_document(ML_TEXT)
    assert a.term_frequencies == b.term_frequencies
    assert a.structure == b.structure
    assert a.cue_hits(["training", "inference"]) == b.cue_hits(["training", "inference"])


def test_compiler_selects_domain_by_evidence_and_validates() -> None:
    ir = compile_adapter(profile_document(ML_TEXT), adapter_id="test-ml")
    assert "ai_ml" in ir.profile_domains
    assert "media" not in ir.profile_domains          # no evidence, no pack
    assert "research" in ir.profile_genres
    assert validate_ir(ir) == []
    assert ir.schema_hash() == ir.schema_hash()
    labels = {e.label for e in ir.entity_labels}
    assert "Model_Architecture" in labels
    by_surface = {p.surface: p for p in ir.predicates}
    assert by_surface["trained on"].canonical == "trained_on"
    assert by_surface["trained on"].status == "exact"


def test_unknown_predicate_stays_open_never_nearest_vague() -> None:
    ir = AdapterIR(
        adapter_id="x", profile_domains=(), profile_genres=(),
        entity_labels=(EntityLabelIR("L", "a label", "concept", "l", "t"),),
        predicates=(PredicateIR("frobnicates", None, "OPEN", "t"),),
    )
    assert validate_ir(ir) == []
    bad = AdapterIR(
        adapter_id="x", profile_domains=(), profile_genres=(),
        entity_labels=(),
        predicates=(PredicateIR("frobnicates", None, "exact", "t"),),
    )
    assert any("status != OPEN" in v for v in validate_ir(bad))
    outside = AdapterIR(
        adapter_id="x", profile_domains=(), profile_genres=(),
        entity_labels=(),
        predicates=(PredicateIR("frobnicates", "not_a_predicate", "exact", "t"),),
    )
    assert any("outside frozen set" in v for v in validate_ir(outside))


def test_activation_flows_labels_and_exact_mappings_only() -> None:
    from services.extraction.gliner2_cpu_provider import schema_descriptions
    from services.extraction.graphify_relations import (
        _relex_relation_labels,
        predicate_compiler,
    )
    from services.ontology_adapter.providers import relex as shim

    ir = compile_adapter(profile_document(ML_TEXT), adapter_id="test-ml")
    name = shim.activate(ir)
    try:
        merged = schema_descriptions((name,))
        assert "Model_Architecture" in merged
        assert "trained on" in _relex_relation_labels()
        compiler = predicate_compiler()
        assert compiler.synonyms.get("trained on") == "trained_on"
        # broader/OPEN mappings must NOT enter the synonym tables
        assert "evaluated on" not in compiler.synonyms
    finally:
        shim.deactivate()
    assert "trained on" not in _relex_relation_labels()
