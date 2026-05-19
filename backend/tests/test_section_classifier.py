"""
Tests for `services.ingestion.section_classifier`.

Covers:
  • Each ChunkKind has at least one positive heading example
  • Docling-style heading prefixes are normalized away before matching
  • False-positive guards — body sections that *mention* biblio words
  • Default to BODY when path is empty / None / first segment is body
  • NOISY_KINDS / GHOST_B_SKIP_KINDS / is_noisy / should_skip_ghost_b helpers
"""
from __future__ import annotations

import pytest

from services.ingestion.section_classifier import (
    ALL_KINDS,
    GHOST_B_SKIP_KINDS,
    NOISY_KINDS,
    ChunkKind,
    classify_chunk,
    classify_content,
    classify_heading,
    is_noisy,
    should_skip_ghost_b,
)


@pytest.mark.parametrize(
    "heading_path, expected",
    [
        # Bibliography variants
        (["Bibliography"], ChunkKind.BIBLIOGRAPHY),
        (["bibliographies"], ChunkKind.BIBLIOGRAPHY),
        (["References"], ChunkKind.BIBLIOGRAPHY),
        (["Works Cited"], ChunkKind.BIBLIOGRAPHY),
        (["Further Reading"], ChunkKind.BIBLIOGRAPHY),
        (["Citations"], ChunkKind.BIBLIOGRAPHY),
        # TOC variants
        (["Table of Contents"], ChunkKind.TOC),
        (["Contents"], ChunkKind.TOC),
        (["List of Figures"], ChunkKind.TOC),
        (["List of Tables"], ChunkKind.TOC),
        (["List of Abbreviations"], ChunkKind.TOC),
        # Index
        (["Index"], ChunkKind.INDEX),
        (["Subject Index"], ChunkKind.INDEX),
        (["Name Index"], ChunkKind.INDEX),
        # Appendix
        (["Appendix"], ChunkKind.APPENDIX),
        (["Appendix A: Source Code"], ChunkKind.APPENDIX),
        (["Appendices"], ChunkKind.APPENDIX),
        # Front matter
        (["Copyright"], ChunkKind.FRONT_MATTER),
        (["Preface"], ChunkKind.FRONT_MATTER),
        (["Foreword"], ChunkKind.FRONT_MATTER),
        (["Prologue"], ChunkKind.FRONT_MATTER),
        (["Dedication"], ChunkKind.FRONT_MATTER),
        (["Acknowledgments"], ChunkKind.FRONT_MATTER),
        (["Acknowledgements"], ChunkKind.FRONT_MATTER),
        (["About the Author"], ChunkKind.FRONT_MATTER),
        (["About the Authors"], ChunkKind.FRONT_MATTER),
        (["About the Editors"], ChunkKind.FRONT_MATTER),
        # Critically: "Introduction" stays as BODY — it's the substantive
        # first section in most papers/books, not preface material.
        (["Introduction"], ChunkKind.BODY),
        (["Introduction:"], ChunkKind.BODY),
        (["Introduction to the SQLite database"], ChunkKind.BODY),
        # Back matter
        (["Glossary"], ChunkKind.BACK_MATTER),
        (["Errata"], ChunkKind.BACK_MATTER),
        (["Endnotes"], ChunkKind.BACK_MATTER),
        (["Epilogue"], ChunkKind.BACK_MATTER),
        (["Afterword"], ChunkKind.BACK_MATTER),
        # Body
        (["Chapter 1: Introduction"], ChunkKind.BODY),
        (["The architecture of the SQLite database"], ChunkKind.BODY),
        (["Synchronous writes"], ChunkKind.BODY),
        # False-positive guards — body sections that mention noise words
        (["References to the Linnaean system"], ChunkKind.BODY),
        (["Bibliographical genealogy in 19th century France"], ChunkKind.BODY),
    ],
)
def test_classify_heading_matrix(heading_path, expected):
    assert classify_heading(heading_path) == expected


@pytest.mark.parametrize(
    "heading_path, expected",
    [
        # Empty cases
        ([], ChunkKind.BODY),
        (None, ChunkKind.BODY),
        ([""], ChunkKind.BODY),
        (["", "References"], ChunkKind.BIBLIOGRAPHY),  # walks past empty segment
        # Body parent should NOT be reclassified by deeper headings
        (["Chapter 5", "References to past work"], ChunkKind.BODY),
        (["Chapter 5", "Appendix-style aside"], ChunkKind.BODY),
    ],
)
def test_classify_walks_segments_correctly(heading_path, expected):
    assert classify_heading(heading_path) == expected


@pytest.mark.parametrize(
    "raw_heading, expected",
    [
        # Docling-style HTML chapter anchors get stripped
        ("[]{#ch12.html_ch12}References {.title}", ChunkKind.BIBLIOGRAPHY),
        ("[]{#toc1}Table of Contents {.title}", ChunkKind.TOC),
        ("{.title}Bibliography", ChunkKind.BIBLIOGRAPHY),
        ("Index   {.title}", ChunkKind.INDEX),
        # Mixed whitespace
        ("  References  ", ChunkKind.BIBLIOGRAPHY),
        ("\tAppendix B\n", ChunkKind.APPENDIX),
    ],
)
def test_normalization_strips_docling_artifacts(raw_heading, expected):
    assert classify_heading([raw_heading]) == expected


def test_all_kinds_includes_body():
    assert ChunkKind.BODY in ALL_KINDS


def test_noisy_kinds_excludes_body_code_and_table():
    # Code and table chunks are retrievable first-class content, not noise.
    assert ChunkKind.BODY not in NOISY_KINDS
    assert ChunkKind.CODE not in NOISY_KINDS
    assert ChunkKind.TABLE not in NOISY_KINDS
    assert set(NOISY_KINDS) == set(ALL_KINDS) - {
        ChunkKind.BODY,
        ChunkKind.CODE,
        ChunkKind.TABLE,
    }


def test_ghost_b_skip_kinds_includes_code():
    # CODE is retrievable but Ghost B is skipped on it (hallucinates Method
    # /Artifact entities from raw code). Skip set = noisy + code.
    assert GHOST_B_SKIP_KINDS == frozenset(list(NOISY_KINDS) + [ChunkKind.CODE])
    assert ChunkKind.CODE in GHOST_B_SKIP_KINDS
    assert ChunkKind.TABLE not in GHOST_B_SKIP_KINDS


@pytest.mark.parametrize(
    "kind, expected",
    [
        (ChunkKind.BODY, False),
        (ChunkKind.CODE, False),  # code is retrievable, not noisy
        (ChunkKind.TABLE, False),
        (ChunkKind.TOC, True),
        (ChunkKind.BIBLIOGRAPHY, True),
        (ChunkKind.INDEX, True),
        (ChunkKind.APPENDIX, True),
        (ChunkKind.FRONT_MATTER, True),
        (ChunkKind.BACK_MATTER, True),
        (None, False),
        ("", False),
        ("unknown_future_kind", False),  # unknown kind treated as benign
    ],
)
def test_is_noisy(kind, expected):
    assert is_noisy(kind) is expected


@pytest.mark.parametrize(
    "kind, expected",
    [
        (ChunkKind.BODY, False),
        (ChunkKind.CODE, True),  # Ghost B is skipped on code chunks (Phase 1)
        (ChunkKind.TABLE, False),
        (ChunkKind.TOC, True),
        (ChunkKind.BIBLIOGRAPHY, True),
        (ChunkKind.APPENDIX, True),
        (None, False),
    ],
)
def test_should_skip_ghost_b(kind, expected):
    assert should_skip_ghost_b(kind) is expected


# ─── Content-based classifier (fallback for OCR pages / tier_c) ─────────────


def test_classify_content_empty_inputs():
    assert classify_content(None) == ChunkKind.BODY
    assert classify_content("") == ChunkKind.BODY
    assert classify_content("   \n\t  ") == ChunkKind.BODY


def test_classify_content_dense_bibliography_page():
    biblio_text = """
    Brown, A., & Smith, J. (2018). Foundations of SQLite. Journal of DB, 12(3), 45-67.
    Carter, P. (2020). iOS persistence patterns. New York: Apress. ISBN 978-1-4842-5111-1
    Davis, R., et al. (2019). Mobile data layers. doi:10.1145/3356467
    Evans, M. (2021). pp. 12-34 of "Lightweight DBs" (Vol. 4).
    Foster, K. (2017). Retrieved from https://example.com/sqlite-paper. [12]
    Garcia, S. (2015). pp. 88. ISBN: 0-13-110362-8. [42]
    """.strip()
    assert classify_content(biblio_text) == ChunkKind.BIBLIOGRAPHY


def test_classify_content_normal_chapter_with_one_citation_stays_body():
    chapter_text = """
    SQLite is a lightweight, embedded relational database engine that ships
    with iOS. Unlike client-server databases such as PostgreSQL or MySQL,
    SQLite runs in-process — there is no separate daemon. This makes it
    ideal for mobile applications where battery and memory are constrained.
    Smith (2018) showed that SQLite can outperform Core Data for read-heavy
    workloads on iPhone hardware.

    The query planner uses cost-based optimization to choose between index
    scans and full table scans. We will explore each strategy in the
    following sections.
    """.strip()
    assert classify_content(chapter_text) == ChunkKind.BODY


def test_classify_content_toc_with_dot_leaders():
    toc_text = "\n".join([
        "Chapter 1: Introduction ...................... 1",
        "Chapter 2: SQLite Internals ................. 12",
        "Chapter 3: Query Planning ................... 45",
        "Chapter 4: iOS Integration .................. 78",
        "Chapter 5: Performance Tuning ............. 112",
        "Chapter 6: Concurrency Patterns ........... 145",
    ])
    assert classify_content(toc_text) == ChunkKind.TOC


def test_classify_content_index_with_comma_pages():
    index_text = "\n".join([
        "Apple, 23, 45-47",
        "Backup, 88, 102",
        "Cursor, 12, 34, 56",
        "Database file, 4, 19, 200",
        "Encryption, 67, 89-91",
        "Foreign keys, 23, 45",
    ])
    assert classify_content(index_text) == ChunkKind.INDEX


def test_classify_content_short_input_does_not_false_fire():
    # Three citation-shaped lines isn't enough to flip — we require ≥5 lines
    # for the line-ratio rules and a density threshold for citations.
    short = "Smith (2018). Brown (2020)."
    assert classify_content(short) == ChunkKind.BODY


# ─── classify_chunk: heading first, content fallback ────────────────────────


def test_classify_chunk_heading_decisive():
    # Heading wins outright — content sample is irrelevant
    assert classify_chunk(["References"], "this is body content") == ChunkKind.BIBLIOGRAPHY


def test_classify_chunk_no_heading_uses_content():
    biblio_text = " ".join([
        "Smith, J. (2018). Foundations. Journal of DB, 12(3), 45-67.",
        "Brown, A., et al. (2020). Mobile SQLite. doi:10.1145/abc.",
        "Carter, P. (2019). pp. 12-34. ISBN 978-1-4842-5111-1.",
        "Davis, R. (2021). Vol. 4, pp. 88. Retrieved from https://example.com.",
    ])
    assert classify_chunk(None, biblio_text) == ChunkKind.BIBLIOGRAPHY


def test_classify_chunk_pdf_page_heading_uses_content():
    # OCR PDFs emit ["page_178"] — must fall through to content classifier
    biblio_text = " ".join([
        "[1] Smith, J. (2018). Foundations of SQLite. Journal of DB, 12(3), 45-67.",
        "[2] Brown, A., et al. (2019). Mobile data layers. doi:10.1145/3356467.",
        "[3] Carter, P. (2020). pp. 12-34. ISBN 978-1-4842-5111-1.",
        "[4] Davis, R. (2021). Vol. 4, pp. 88. Retrieved from https://example.com.",
    ])
    assert classify_chunk(["page_178"], biblio_text) == ChunkKind.BIBLIOGRAPHY
    assert classify_chunk(["pages_178-180"], biblio_text) == ChunkKind.BIBLIOGRAPHY


def test_classify_chunk_pdf_page_with_body_content_stays_body():
    body_text = (
        "SQLite uses a B-tree structure to store rows. The cost-based "
        "query planner evaluates index scans against full table scans. "
        "On iOS, persistent connections are managed via the FMDB wrapper. "
        "Each transaction acquires a write lock that blocks concurrent "
        "writers but allows multiple readers."
    )
    assert classify_chunk(["page_42"], body_text) == ChunkKind.BODY


def test_classify_chunk_real_heading_does_not_run_content_fallback():
    # If heading was conclusive (Chapter 5), even citation-heavy content
    # in that chapter shouldn't reclassify as biblio.
    citation_heavy = " ".join([
        "Smith (2018). Brown (2020). Carter (2021). Davis (2019). doi:10.x [1]"
    ] * 6)
    assert classify_chunk(["Chapter 5: Citations in academic writing"], citation_heavy) == ChunkKind.BODY
