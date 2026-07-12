from services.retriever.query_semantics import (
    concept_support_phrases,
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


def test_uncurated_concept_does_not_gain_static_domain_aliases():
    assert concept_support_phrases("sticky_message") == ("sticky message",)


def test_used_is_query_scaffolding_not_an_evidence_concept():
    atoms = required_atoms_for_query(
        "How is emotional contrast used in a sticky message?"
    )

    assert "concept:used" not in atoms


def test_multi_hop_command_verbs_do_not_become_evidence_concepts():
    atoms = required_atoms_for_query(
        "Find the Purple Ocean differentiation mechanism, then use it to "
        "evaluate sticky messaging for a product page."
    )

    assert "concept:find" not in atoms
    assert "concept:use" not in atoms
    assert "concept:evaluate" not in atoms
    assert "concept:purple" in atoms
    assert "concept:product_page" in atoms


def test_explicit_graph_route_words_do_not_become_evidence_concepts():
    atoms = required_atoms_for_query(
        "What relationship does the graph establish between product "
        "positioning and memorable messaging?"
    )

    assert "concept:graph" not in atoms
    assert "concept:establish" not in atoms
    assert "relationship" in atoms
    assert "concept:product_positioning" in atoms


def test_cardinal_count_words_do_not_become_evidence_concepts():
    atoms = required_atoms_for_query(
        "What are the six SUCCESs principles in Made to Stick?"
    )

    assert "concept:six" not in atoms


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

    keys2 = [
        group.key for group in concept_groups("what does Cialdini say about persuasion")
    ]
    assert "say" not in keys2
    assert "says" not in keys2
    assert "cialdini" in keys2
    assert "persuasion" in keys2

    # verbs-of-saying family (post-deploy probe caught 'describes' surviving)
    keys3 = [
        group.key
        for group in concept_groups(
            "what is the payoff of a game as Eric Berne describes it"
        )
    ]
    assert "describes" not in keys3
    assert "berne" in keys3
    assert "payoff" in keys3


def test_generic_descriptors_do_not_anchor_lanes():
    # Live regression (2026-07-02): "a COMMON trait that GREAT seducers
    # EXECUTE and LEARN EARLY, MID..." minted lanes for common/great and a
    # 'mid' concept that lexically matched LaTeX "\\mid" in a math book,
    # dragging 5 off-topic docs into an Art of Seduction question.
    keys = [
        g.key
        for g in concept_groups(
            "What is a common trait that great seducers execute and learn "
            "early, mid, and change during their life. How do they show this"
        )
    ]
    for junk in ("common", "great", "mid", "early", "execute", "learn", "show"):
        assert junk not in keys, f"{junk} must not anchor a lane"
    assert "seducers" in keys
    assert "trait" in keys


def test_business_query_scaffolding_does_not_displace_substantive_concepts():
    keys = [
        g.key
        for g in concept_groups(
            "What repeated advice appears in this corpus about creating offers, "
            "product positioning, and conversion?"
        )
    ]

    for junk in ("repeated", "advice", "appears", "creating"):
        assert junk not in keys
    assert keys[:3] == ["conversion", "offers", "product_positioning"]


def test_cross_corpus_media_scope_words_do_not_become_evidence_lanes():
    keys = [
        g.key
        for g in concept_groups(
            "Compare what the marketing transcripts and ecommerce PDFs say about "
            "offers, positioning, conversion, and customer acquisition."
        )
    ]

    for junk in ("marketing", "transcripts", "ecommerce", "pdfs"):
        assert junk not in keys
    assert keys[:4] == [
        "conversion",
        "customer_acquisition",
        "offers",
        "product_positioning",
    ]
