"""T-HOOK-3 unit tests — date de-conflation rule + deterministic parsers.

Portable invariants: pure logic, NO live stack. Run:
    cd backend && pytest tests/test_bibliographic_capture.py -q
"""

from __future__ import annotations

from services.ingestion.bibliographic import (
    KIND_AMBIGUOUS,
    KIND_FILE_CREATION,
    KIND_PUBLICATION,
    KIND_REVISION,
    REASON_FILE_DATE_ONLY,
    REASON_NO_DATE_SOURCE,
    REASON_UNPARSEABLE_DATE,
    DateCandidate,
    extract_text_head_biblio,
    filename_year_candidate,
    normalize_date_string,
    normalize_language,
    parse_citation_name,
    merge_persisted_bibliographic,
    promote_bibliographic,
    resolve_document_dates,
)


# ─── The de-conflation rule ─────────────────────────────────────────────────

class TestDeconflation:
    def test_file_creation_never_becomes_document_date(self):
        """THE T-HOOK-3 contract: document_date must never silently mean mtime."""
        res = resolve_document_dates([
            DateCandidate("2024-03-01", KIND_FILE_CREATION,
                          "pdf_creation_date", "pdf:/CreationDate"),
            DateCandidate("2024-03-02", KIND_REVISION,
                          "pdf_mod_date", "pdf:/ModDate"),
            DateCandidate("2024-03-03", KIND_FILE_CREATION,
                          "docx_core_created", "docx:core_properties.created"),
        ])
        assert res["document_date"] is None
        assert res["source_published_at"] is None
        assert res["date_confidence"] is None
        assert res["reason"] == REASON_FILE_DATE_ONLY

    def test_publication_beats_file_creation(self):
        res = resolve_document_dates([
            DateCandidate("2024-03-01", KIND_FILE_CREATION,
                          "pdf_creation_date", "pdf:/CreationDate"),
            DateCandidate("2020-05-17", KIND_PUBLICATION,
                          "frontmatter_published", "frontmatter:published"),
        ])
        assert res["document_date"] == "2020-05-17"
        assert res["source_published_at"] == "2020-05-17"
        assert res["date_confidence"] == "high"
        assert res["reason"] is None

    def test_high_confidence_beats_low_regardless_of_order(self):
        res = resolve_document_dates([
            DateCandidate("2016", KIND_AMBIGUOUS, "filename_year", "slug-2016"),
            DateCandidate("2020-01-02", KIND_PUBLICATION,
                          "html_meta_published", "html_meta:article:published_time"),
        ])
        assert res["document_date"] == "2020-01-02"
        assert res["date_confidence"] == "high"

    def test_ambiguous_date_key_is_medium(self):
        res = resolve_document_dates([
            DateCandidate("2021-11-30", KIND_AMBIGUOUS,
                          "frontmatter_date", "frontmatter:date"),
        ])
        assert res["document_date"] == "2021-11-30"
        assert res["date_confidence"] == "medium"

    def test_no_candidates_yields_null_with_reason(self):
        res = resolve_document_dates([])
        assert res["document_date"] is None
        assert res["reason"] == REASON_NO_DATE_SOURCE

    def test_unparseable_candidate_yields_reason_not_guess(self):
        res = resolve_document_dates([
            DateCandidate("last spring", KIND_PUBLICATION,
                          "frontmatter_published", "frontmatter:published"),
        ])
        assert res["document_date"] is None
        assert res["reason"] == REASON_UNPARSEABLE_DATE

    def test_ties_broken_by_input_order(self):
        res = resolve_document_dates([
            DateCandidate("2010", KIND_AMBIGUOUS, "citation_pattern", "first"),
            DateCandidate("2011", KIND_AMBIGUOUS, "citation_pattern", "second"),
        ])
        assert res["document_date"] == "2010-01-01"
        assert res["source"] == "first"

    def test_year_precision_recorded(self):
        res = resolve_document_dates([
            DateCandidate("2016", KIND_AMBIGUOUS, "filename_year", "slug"),
        ])
        assert res["document_date"] == "2016-01-01"
        assert res["precision"] == "year"
        assert res["date_confidence"] == "low"

    def test_dict_candidates_accepted(self):
        res = resolve_document_dates([
            {"raw": "2019-04", "kind": KIND_PUBLICATION,
             "method": "epub_dc_date", "source": "epub:dc:date"},
        ])
        assert res["document_date"] == "2019-04-01"
        assert res["precision"] == "month"


# ─── Date normalization ─────────────────────────────────────────────────────

class TestNormalizeDateString:
    def test_iso_day(self):
        assert normalize_date_string("2020-05-17") == ("2020-05-17", "day")

    def test_iso_with_timestamp(self):
        assert normalize_date_string("2020-05-17T10:00:00Z") == ("2020-05-17", "day")

    def test_month_name_day_year(self):
        assert normalize_date_string("Dec 24, 2025") == ("2025-12-24", "day")
        assert normalize_date_string("December 24 2025") == ("2025-12-24", "day")

    def test_day_month_name_year(self):
        assert normalize_date_string("24 Dec 2025") == ("2025-12-24", "day")

    def test_iso_month(self):
        assert normalize_date_string("2019-04") == ("2019-04-01", "month")

    def test_year_month_name(self):
        assert normalize_date_string("1975 January") == ("1975-01-01", "month")

    def test_year_only(self):
        assert normalize_date_string("2016") == ("2016-01-01", "year")
        assert normalize_date_string("(2020, Wiley)") == ("2020-01-01", "year")

    def test_garbage(self):
        assert normalize_date_string("not a date") == (None, None)
        assert normalize_date_string("") == (None, None)

    def test_invalid_calendar_day_degrades_to_month_precision(self):
        # Feb 31 does not exist; the year-month portion is still honest.
        iso, precision = normalize_date_string("2021-02-31")
        assert (iso, precision) == ("2021-02-01", "month")


# ─── Citation-style filename patterns (real corpus shapes) ──────────────────

class TestParseCitationName:
    def test_libgen_author_title_year_publisher(self):
        name = ("[(Architectural Design)] Ian Ritchie - Neuroarchitecture_ "
                "Designing with the Mind in Mind (2020, Wiley) - libgen.li.epub")
        cite = parse_citation_name(name)
        assert cite["author"] == "Ian Ritchie"
        assert cite["title"] == "Neuroarchitecture: Designing with the Mind in Mind"
        assert cite["year_raw"] == "2020"

    def test_journal_title_authors_brace_year(self):
        name = ("[International Journal of Reasoning-based Intelligent Systems "
                "vol. 2 iss. 1] Bayesian reasoning for Laban Movement Analysis "
                "used in human-machine interaction{Rett, Jorg_ Dias, Jorge_ "
                "Ahuactzin, Juan Manuel}(2010)[1.pdf")
        cite = parse_citation_name(name)
        assert cite["year_raw"] == "2010"
        assert "Rett, Jorg" in cite["author"]
        assert cite["title"].startswith("Bayesian reasoning for Laban")

    def test_journal_title_year_month(self):
        name = ("[Quest 1975-jan vol. 23 iss. 1] THE BENESH MOVEMENT NOTATION"
                "(1975 January)10.1080_00336297.1975.10519826 libgen.li.pdf")
        cite = parse_citation_name(name)
        assert cite["year_raw"] == "1975 January"
        assert cite["title"] == "THE BENESH MOVEMENT NOTATION"
        assert "author" not in cite

    def test_slug_filename_never_yields_author(self):
        """An author is never guessed out of a slug filename."""
        cite = parse_citation_name(
            "100-things-every-designer-needs-to-know-about-people-susan-weinschenk.md")
        assert cite.get("author") is None

    def test_plain_filename_no_match(self):
        assert parse_citation_name("03-finetune-llama31-unsloth.md") == {}
        assert parse_citation_name("") == {}

    def test_filename_year_candidate(self):
        cand = filename_year_candidate(
            "blain-brown-cinematography-theory-and-practice-2016.md")
        assert cand is not None
        assert cand.raw == "2016"
        assert cand.method == "filename_year"
        # slug without trailing year → None
        assert filename_year_candidate("anatomy-for-sculptors.md") is None
        # youtube id suffix must not produce a year
        assert filename_year_candidate(
            "8-years-of-marketing-advice-in-70-minutes-yt-1w9uywm9bgs.md") is None


# ─── Text-head extraction (real corpus shapes) ──────────────────────────────

class TestExtractTextHead:
    def test_scraped_book_heading_and_source_line(self):
        head = (
            "# [(Architectural Design)] Ian Ritchie - Neuroarchitecture_ "
            "Designing with the Mind in Mind (2020, Wiley) - libgen.li.epub\n\n"
            "**Source:** `E:\\books\\art\\epub\\[(Architectural Design)] Ian "
            "Ritchie - Neuroarchitecture_ Designing with the Mind in Mind "
            "(2020, Wiley) - libgen.li.epub`\n\n## OEBPS/ch1.xhtml\n"
        )
        out = extract_text_head_biblio(head)
        assert out["author"] == "Ian Ritchie"
        assert out["title"].startswith("Neuroarchitecture")
        years = [c.raw for c in out["candidates"]]
        assert "2020" in years

    def test_copyright_line(self):
        head = ("## Page 1\n\nInt. J. Reasoning-based Intelligent Systems, "
                "Vol. 2, No. 1, 2010\n13\nCopyright © 2010 Inderscience "
                "Enterprises Ltd.\n")
        out = extract_text_head_biblio(head)
        methods = {c.method: c.raw for c in out["candidates"]}
        assert methods.get("text_head_copyright") == "2010"

    def test_standalone_date_line_substack(self):
        head = ("# Daily Dose of Data Science\n\n"
                "## [Hands-on] Deploy and Run LLMs on your Phone!\n\n"
                "Avi Chawla\nDec 24, 2025\n7\nShare\n")
        out = extract_text_head_biblio(head)
        methods = {c.method: c.raw for c in out["candidates"]}
        assert methods.get("text_head_date_line") == "Dec 24, 2025"

    def test_labelled_title_page_fields(self):
        head = (
            "# A Short History of Logistics\n\n"
            "Author: Edwin Halvorsen\n"
            "Published: 2004\n"
            "Language: English\n"
        )
        out = extract_text_head_biblio(head)
        assert out["author"] == "Edwin Halvorsen"
        assert out["language"] == "english"
        resolution = resolve_document_dates(out["candidates"])
        assert resolution["document_date"] == "2004-01-01"
        assert resolution["method"] == "text_head_published"
        assert resolution["source"] == "text_head:published"

    def test_plain_transcript_head_yields_nothing(self):
        head = ("## Description\n\nApply for my mentorship Brand Builders "
                "Academy:\n\nJoin my free email list:\n")
        out = extract_text_head_biblio(head)
        assert out["candidates"] == []
        assert "author" not in out

    def test_prose_sentence_dates_are_not_matched(self):
        head = ("# Notes\n\nThe meeting on Dec 24, 2025 was about budgets "
                "and nothing else.\n")
        out = extract_text_head_biblio(head)
        assert out["candidates"] == []


# ─── Language normalization ─────────────────────────────────────────────────

class TestNormalizeLanguage:
    def test_valid(self):
        assert normalize_language("EN") == "en"
        assert normalize_language("en-US") == "en-us"
        assert normalize_language("English") == "english"

    def test_invalid(self):
        assert normalize_language(None) is None
        assert normalize_language("") is None
        assert normalize_language("12345") is None
        assert normalize_language("x" * 64) is None


# ─── Frontmatter capture (docling_adapter integration) ──────────────────────

class TestFrontmatterCapture:
    def _meta(self, text):
        from services.ingestion.docling_adapter import _meta_from_frontmatter

        return _meta_from_frontmatter(text)

    def test_published_becomes_publication_candidate(self):
        meta = self._meta(
            "---\ntitle: My Post\nauthor: Jane Doe\nlanguage: en\n"
            "published: 2023-08-14\n---\nbody\n")
        assert meta["title"] == "My Post"
        assert meta["author"] == "Jane Doe"
        assert meta["language_meta"] == "en"
        cands = meta["date_candidates"]
        assert len(cands) == 1
        assert cands[0].kind == KIND_PUBLICATION
        assert cands[0].method == "frontmatter_published"
        res = resolve_document_dates(cands)
        assert res["document_date"] == "2023-08-14"
        assert res["date_confidence"] == "high"

    def test_created_is_file_time_never_publication(self):
        """The scraped-export `created:`/`extracted:`-era trap: a scrape
        timestamp must not become the document's date."""
        meta = self._meta("---\ntitle: T\ncreated: 2026-03-24\n---\nbody\n")
        cands = meta["date_candidates"]
        assert cands[0].kind == KIND_FILE_CREATION
        res = resolve_document_dates(cands)
        assert res["document_date"] is None
        assert res["reason"] == REASON_FILE_DATE_ONLY

    def test_date_key_is_ambiguous_medium(self):
        meta = self._meta("---\ndate: 2021-01-05\n---\nbody\n")
        res = resolve_document_dates(meta["date_candidates"])
        assert res["document_date"] == "2021-01-05"
        assert res["date_confidence"] == "medium"

    def test_no_frontmatter(self):
        assert self._meta("# just a doc\n") == {}

    def test_html_meta_attributes_are_order_independent(self):
        from services.ingestion.docling_adapter import _meta_from_html

        meta = _meta_from_html(
            b'<html lang="en"><head>'
            b'<meta content="Jane Doe" name="author">'
            b'<meta content="2020-05-17" property="article:published_time">'
            b'</head></html>'
        )
        assert meta["author"] == "Jane Doe"
        assert meta["language_meta"] == "en"
        assert resolve_document_dates(meta["date_candidates"])["document_date"] \
            == "2020-05-17"


# ─── Storage-boundary promotion ─────────────────────────────────────────────

class TestPromoteBibliographic:
    def _doc(self):
        return {
            "doc_id": "d1", "corpus_id": "c1", "title": "existing title",
            "routing_trace": {
                "parser": "local_markdown",
                "bibliographic": {
                    "author": "Jane Doe",
                    "language": "en",
                    "document_date": "2023-08-14",
                    "source_published_at": "2023-08-14",
                    "date_confidence": "high",
                    "bibliographic_provenance": {
                        "method": "frontmatter_published",
                        "source": "frontmatter:published",
                        "captured_at": "2026-07-13T00:00:00+00:00",
                        "origin": "ingest",
                    },
                },
            },
        }

    def test_promotes_and_cleans_routing_trace(self):
        doc = promote_bibliographic(self._doc())
        assert doc["author"] == "Jane Doe"
        assert doc["language"] == "en"
        assert doc["document_date"] == "2023-08-14"
        assert doc["source_published_at"] == "2023-08-14"
        assert doc["date_confidence"] == "high"
        assert doc["bibliographic_provenance"]["origin"] == "ingest"
        assert "bibliographic" not in doc["routing_trace"]
        assert doc["routing_trace"]["parser"] == "local_markdown"

    def test_never_clobbers_existing_top_level(self):
        raw = self._doc()
        raw["author"] = "Preexisting Author"
        doc = promote_bibliographic(raw)
        assert doc["author"] == "Preexisting Author"
        assert doc["title"] == "existing title"

    def test_noop_without_block(self):
        doc = {"doc_id": "d", "corpus_id": "c", "routing_trace": {"parser": "x"}}
        assert promote_bibliographic(dict(doc)) == doc
        assert promote_bibliographic({"doc_id": "d", "corpus_id": "c"}) == {
            "doc_id": "d", "corpus_id": "c"}

    def test_does_not_mutate_shared_routing_trace(self):
        raw = self._doc()
        shared_trace = raw["routing_trace"]
        promote_bibliographic(raw)
        assert "bibliographic" in shared_trace  # caller's dict untouched

    def test_date_identity_is_promoted_as_one_family(self):
        raw = self._doc()
        raw["document_date"] = "2026-01-01"  # unproven legacy value
        doc = promote_bibliographic(raw)
        assert doc["document_date"] == "2023-08-14"
        assert doc["source_published_at"] == "2023-08-14"
        assert doc["date_confidence"] == "high"
        assert doc["bibliographic_provenance"]["method"] \
            == "frontmatter_published"

    def test_persisted_merge_preserves_backfill_on_sparse_replay(self):
        durable = {
            "doc_id": "d1",
            "author": "Backfilled Author",
            "document_date": "2020-01-01",
            "source_published_at": "2020-01-01",
            "date_confidence": "medium",
            "bibliographic_provenance": {
                "method": "citation_pattern", "origin": "backfill_v2",
            },
        }
        merged = merge_persisted_bibliographic({"doc_id": "d1"}, durable)
        assert merged["author"] == "Backfilled Author"
        assert merged["document_date"] == "2020-01-01"
        assert merged["source_published_at"] == "2020-01-01"
        assert merged["bibliographic_provenance"]["method"] == "citation_pattern"

    def test_explicit_ingest_date_beats_inferred_backfill(self):
        durable = {
            "document_date": "2020-01-01",
            "source_published_at": "2020-01-01",
            "date_confidence": "medium",
            "bibliographic_provenance": {
                "method": "citation_pattern", "origin": "backfill_v2",
            },
        }
        incoming = {
            "document_date": "2021-06-02",
            "source_published_at": "2021-06-02",
            "date_confidence": "high",
            "bibliographic_provenance": {
                "method": "frontmatter_published", "origin": "ingest",
            },
        }
        merged = merge_persisted_bibliographic(incoming, durable)
        assert merged["document_date"] == "2021-06-02"
        assert merged["source_published_at"] == "2021-06-02"
        assert merged["bibliographic_provenance"]["origin"] == "ingest"


# ─── finalize_source_meta end-to-end (parse-result → biblio block) ──────────

class TestFinalizeSourceMeta:
    def test_markdown_frontmatter_to_block(self):
        from services.ingestion import docling_adapter as da

        result = da._parse_local_text_document(
            b"---\ntitle: My Post\nauthor: Jane Doe\nlang: en\n"
            b"published: 2023-08-14\ncreated: 2026-03-24\n---\n# H\n\nbody\n",
            "my-post.md",
            "text/markdown",
        )
        assert result is not None
        da.finalize_source_meta(result, "my-post.md")
        blk = result.routing_trace["bibliographic"]
        assert result.document_date == "2023-08-14"
        assert blk["source_published_at"] == "2023-08-14"
        assert blk["date_confidence"] == "high"
        assert blk["author"] == "Jane Doe"
        assert blk["language"] == "en"
        prov = blk["bibliographic_provenance"]
        assert prov["method"] == "frontmatter_published"
        assert prov["origin"] == "ingest"
        assert blk["title"] == "My Post"

        first_block = dict(blk)
        da.finalize_source_meta(result, "my-post.md")
        assert result.routing_trace["bibliographic"] == first_block

    def test_markdown_created_only_stays_null_with_reason(self):
        from services.ingestion import docling_adapter as da

        result = da._parse_local_text_document(
            b"---\ntitle: T\ncreated: 2026-03-24\n---\n# H\n\nbody\n",
            "t.md",
            "text/markdown",
        )
        assert result is not None
        da.finalize_source_meta(result, "t.md")
        blk = result.routing_trace["bibliographic"]
        assert result.document_date is None
        assert blk["document_date"] is None
        assert blk["bibliographic_provenance"]["reason"] == REASON_FILE_DATE_ONLY

    def test_apply_meta_supports_legacy_result_without_candidate_attribute(self):
        from types import SimpleNamespace
        from services.ingestion.docling_adapter import _apply_meta

        result = SimpleNamespace(title=None, author=None, language_meta=None)
        candidate = DateCandidate(
            "2020-01-01", KIND_PUBLICATION,
            "frontmatter_published", "frontmatter:published",
        )
        _apply_meta(result, {"date_candidates": [candidate]})
        assert result.date_candidates == [candidate]
