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
        (["Dedication"], ChunkKind.FRONT_MATTER),
        (["Acknowledgments"], ChunkKind.FRONT_MATTER),
        (["Acknowledgements"], ChunkKind.FRONT_MATTER),
        (["About the Author"], ChunkKind.FRONT_MATTER),
        (["About the Authors"], ChunkKind.FRONT_MATTER),
        (["About the Editors"], ChunkKind.FRONT_MATTER),
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


def test_noisy_kinds_excludes_body():
    assert ChunkKind.BODY not in NOISY_KINDS
    assert set(NOISY_KINDS) == set(ALL_KINDS) - {ChunkKind.BODY}


def test_ghost_b_skip_kinds_matches_noisy():
    assert GHOST_B_SKIP_KINDS == frozenset(NOISY_KINDS)


@pytest.mark.parametrize(
    "kind, expected",
    [
        (ChunkKind.BODY, False),
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
        (ChunkKind.TOC, True),
        (ChunkKind.BIBLIOGRAPHY, True),
        (ChunkKind.APPENDIX, True),
        (None, False),
    ],
)
def test_should_skip_ghost_b(kind, expected):
    assert should_skip_ghost_b(kind) is expected
