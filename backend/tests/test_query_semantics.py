from services.retriever.query_semantics import (
    concept_groups,
    lexical_terms,
    required_atoms_for_query,
)


def test_relationship_operators_are_not_content_concepts():
    query = "how does personality correlate with seduction"

    assert [group.key for group in concept_groups(query)] == [
        "personality",
        "seduction",
    ]
    assert lexical_terms(query) == ["personality", "seduction"]
    assert required_atoms_for_query(query) == {
        "concept:personality",
        "concept:seduction",
        "relationship",
    }


def test_generic_context_words_do_not_anchor_retrieval():
    query = "different personality with people as men dating women"

    assert [group.key for group in concept_groups(query)] == [
        "personality_framework",
        "personality",
    ]
    assert lexical_terms(query) == ["personality"]


def test_domain_aliases_cover_common_vocabulary_mismatch():
    groups = concept_groups("personality seduction")
    aliases = {group.key: set(group.aliases) for group in groups}

    assert {"character", "traits", "type"}.issubset(aliases["personality"])
    assert {"seductive", "seducer"}.issubset(aliases["seduction"])


def test_phrase_aliases_collapse_to_semantic_concepts():
    groups = concept_groups("what is natural language processing")

    assert [group.key for group in groups] == ["nlp"]
    assert required_atoms_for_query("what is natural language processing") == {
        "concept:nlp",
        "definition",
    }


def test_art_of_seduction_does_not_make_art_a_required_concept():
    query = "how does different personality correlate to the art of seduction"

    assert [group.key for group in concept_groups(query)] == [
        "personality_framework",
        "personality",
        "seduction",
    ]
    assert "concept:art" not in required_atoms_for_query(query)
    assert "concept:personality_framework" in required_atoms_for_query(query)
