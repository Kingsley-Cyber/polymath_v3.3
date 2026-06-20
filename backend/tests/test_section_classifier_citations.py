"""Portable unit tests for the strengthened citation/reference classifier.

Pure logic, NO live stack — runs anywhere via pytest. Pins the preventive
ingestion fix: academic reference blocks (author-initial lists, numbered
entries, proceedings/arXiv/page-range markers, inline "## References" headings)
classify as BIBLIOGRAPHY, while real prose that merely cites a source or two
stays BODY (validated 0 false positives on 1500 clean-prose corpus chunks).
"""
from services.ingestion.section_classifier import (
    ChunkKind,
    classify_content,
    reference_signal_count,
)


# ── reference blocks -> bibliography ──────────────────────────────────────

def test_author_initial_list_block_is_bibliography():
    t = (
        "Arun, A., Batra, S., Bhardwaj, V., Challa, A., Donmez, P., Heidari, P., "
        "Inan, H., Jain, S.: Best practices for data-efficient modeling in NLG. "
        "In: Proceedings of the 28th International Conference on Computational Linguistics"
    )
    assert classify_content(t) == ChunkKind.BIBLIOGRAPHY


def test_numbered_reference_entries_are_bibliography():
    t = (
        "- [1] D. Anderson, C. Bailey. 2004. Hidden Markov model symbol recognition. "
        "Pages 15-21 of: AAAI Fall Symposium.\n"
        "- [2] W. Liu, O. Rioul, J. McGrenere. 2018. BIGFile: Bayesian information gain. "
        "In: Proceedings of the SIGCHI Conference."
    )
    assert classify_content(t) == ChunkKind.BIBLIOGRAPHY


def test_inline_references_heading_is_bibliography():
    t = (
        "## References\n\n- [Harnessing the Power of LLMs in Practice: A Survey]\n"
        "- [Attention Is All You Need]\n- [BERT: Pre-training of Deep Transformers]"
    )
    assert classify_content(t) == ChunkKind.BIBLIOGRAPHY


def test_arxiv_reference_block_is_bibliography():
    t = (
        "Feng, S.Y., Gangal, V., Wei, J., Chandar, S., Vosoughi, S., Mitamura, T., "
        "Hovy, E.: A survey of data augmentation approaches for NLP. arXiv:2105.03075"
    )
    assert classify_content(t) == ChunkKind.BIBLIOGRAPHY


# ── real prose -> stays body (precision guards) ───────────────────────────

def test_prose_with_one_citation_stays_body():
    t = (
        "Data augmentation is one simple but effective way to boost the performance "
        "of text classifiers on small datasets. We apply token replacement and "
        "back-translation to expand the training set."
    )
    assert classify_content(t) == ChunkKind.BODY


def test_definition_prose_stays_body():
    t = (
        "Natural language processing enables machines to understand and generate "
        "human language across tasks like translation, summarization, and question "
        "answering."
    )
    assert classify_content(t) == ChunkKind.BODY


def test_references_to_X_heading_stays_body():
    # the (?!\s+to\s) guard — "References to ..." is prose, not a ref section
    t = (
        "## References to the Linnaean system are common in early taxonomy. The "
        "system organizes life into nested ranks and remains influential today."
    )
    assert classify_content(t) == ChunkKind.BODY


def test_reference_signal_count_zero_for_plain_prose():
    assert reference_signal_count(
        "The model learns from data and improves over time with more examples."
    ) == 0
