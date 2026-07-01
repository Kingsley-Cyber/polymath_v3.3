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


def test_attribution_scaffolding_does_not_mint_concepts():
    # Live-probe regression (2026-07-01): "According to Eric Berne..." minted
    # 'according' as a concept -> fake evidence lane -> wasted gap-fill
    # support retrieval. Attribution verbs describe the query's relationship
    # to a source, never a corpus concept. Proper names must still survive.
    query = "according to Eric Berne what is transactional analysis"
    keys = [group.key for group in concept_groups(query)]
    assert "according" not in keys
    assert "eric" in keys
    assert "berne" in keys

    keys2 = [group.key for group in concept_groups("what does Cialdini say about persuasion")]
    assert "say" not in keys2
    assert "says" not in keys2
    assert "cialdini" in keys2
    assert "persuasion" in keys2

    # verbs-of-saying family (post-deploy probe caught 'describes' surviving)
    keys3 = [group.key for group in concept_groups(
        "what is the payoff of a game as Eric Berne describes it"
    )]
    assert "describes" not in keys3
    assert "berne" in keys3
    assert "payoff" in keys3
