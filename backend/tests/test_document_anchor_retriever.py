import pytest

from services.conversation import conversation_service
from services.retriever.document_anchor import (
    _chunk_search_terms,
    _doc_labels,
    _score_doc_match,
    document_anchor_retriever,
)


class _Cursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self._limit = None

    def sort(self, *_args, **_kwargs):
        return self

    def limit(self, value):
        self._limit = value
        return self

    async def to_list(self, length=None):
        limit = self._limit or length
        if limit is None:
            return list(self.rows)
        return list(self.rows)[:limit]


class _Collection:
    def __init__(self, rows):
        self.rows = rows

    def find(self, query, projection=None):
        del projection
        rows = self.rows
        if "corpus_id" in query:
            corpus_filter = query["corpus_id"]
            if isinstance(corpus_filter, dict) and "$in" in corpus_filter:
                allowed = set(corpus_filter["$in"])
                rows = [row for row in rows if row.get("corpus_id") in allowed]
            else:
                rows = [row for row in rows if row.get("corpus_id") == corpus_filter]
        if "doc_id" in query:
            rows = [row for row in rows if row.get("doc_id") == query["doc_id"]]
        if "$text" in query:
            terms = query["$text"]["$search"].lower().split()
            rows = [
                {**row, "score": sum(term in row.get("text", "").lower() for term in terms)}
                for row in rows
                if any(term in row.get("text", "").lower() for term in terms)
            ]
            rows.sort(key=lambda row: row.get("score", 0), reverse=True)
        return _Cursor(rows)


class _Db(dict):
    def __getitem__(self, key):
        return super().__getitem__(key)


def test_document_title_match_scores_embedded_book_title():
    query = (
        "Based on Fowler's Patterns of Enterprise Application Architecture and "
        "Myers Briggs Gifts Differing, compare layering and cognitive preference."
    )

    assert _score_doc_match(query, "Patterns of Enterprise Application Architecture") >= 0.95
    assert _score_doc_match(query, "Gifts Differing") >= 0.95
    assert _score_doc_match(query, "Unrelated Gardening Handbook") == 0.0


def test_document_labels_extract_short_title_from_long_archive_filename():
    doc = {
        "filename": (
            "Gifts Differing_ Understanding Personality Type - The -- Myers, "
            "I_B_;Myers, P_B_ -- London, 2010 -- John Murray Press -- "
            "9781473643796 -- Anna's Archive.md"
        )
    }

    labels = _doc_labels(doc)

    assert "Gifts Differing" in labels
    assert "Gifts Differing Understanding Personality Type" in labels


def test_document_title_match_does_not_trigger_on_generic_topic_overlap():
    query = "How should enterprise architecture handle service gateways?"

    assert _score_doc_match(
        query,
        "Patterns of Enterprise Application Architecture",
    ) == 0.0


def test_document_anchor_chunk_terms_prioritize_concepts_over_prompt_filler():
    query = (
        "Based on the retrieved excerpts from Fowler's Patterns of Enterprise "
        "Application Architecture and Myers/Briggs' Gifts Differing, identify "
        "any defensible intersection between enterprise application structure "
        "and cognitive preference theory. How could architectural concepts such "
        "as layering, domain logic, gateways, and mapping inform UI/UX decisions "
        "about information density, navigation depth, and workflow flexibility "
        "for different personality types? Distinguish direct textual support "
        "from inferred design recommendations."
    )

    terms = _chunk_search_terms(
        query,
        {"patterns", "enterprise", "application", "architecture"},
    )

    assert {"layering", "gateway", "gateways", "mapping"} & set(terms)
    assert {"cognitive", "preference", "navigation", "workflow"} <= set(terms)
    assert "retrieved" not in terms
    assert "excerpts" not in terms


@pytest.mark.asyncio
async def test_document_anchor_retriever_returns_chunks_from_named_books(monkeypatch):
    fake_db = _Db(
        documents=_Collection(
            [
                {
                    "corpus_id": "c1",
                    "doc_id": "fowler",
                    "filename": "Patterns of Enterprise Application Architecture.pdf",
                },
                {
                    "corpus_id": "c1",
                    "doc_id": "gifts",
                    "filename": "Gifts Differing.pdf",
                },
            ]
        ),
        chunks=_Collection(
            [
                {
                    "corpus_id": "c1",
                    "doc_id": "fowler",
                    "chunk_id": "f1",
                    "parent_id": "fp1",
                    "text": "Layering separates domain logic from gateways and mapping code.",
                },
                {
                    "corpus_id": "c1",
                    "doc_id": "gifts",
                    "chunk_id": "g1",
                    "parent_id": "gp1",
                    "text": "Personality types differ in preference and perception.",
                },
            ]
        ),
    )
    monkeypatch.setattr(conversation_service, "_db", fake_db)

    chunks = await document_anchor_retriever.search(
        "Fowler Patterns of Enterprise Application Architecture and Gifts Differing: "
        "layering domain logic gateways mapping personality types",
        ["c1"],
        top_k=4,
    )

    assert {chunk.doc_id for chunk in chunks} == {"fowler", "gifts"}
    assert all(chunk.source_tier == "document_anchor+lexical" for chunk in chunks)
    assert all(chunk.text.startswith("Document: ") for chunk in chunks)


@pytest.mark.asyncio
async def test_doc_label_table_is_cached_across_retrievals(monkeypatch):
    # Speed campaign (2026-07-02): _matching_docs used to fetch EVERY document
    # record from Mongo on EVERY retrieval (main + each support pass), which
    # stalled the event loop under concurrency. The label table must be built
    # once per corpus set and served from cache afterwards.
    import services.retriever.document_anchor as da

    find_calls = {"documents": 0}

    class _CountingCollection(_Collection):
        def find(self, query, projection=None):
            find_calls["documents"] += 1
            return super().find(query, projection)

    docs = [
        {
            "doc_id": "doc-1",
            "corpus_id": "corpus-1",
            "title": "Patterns of Enterprise Application Architecture",
            "filename": "fowler.md",
        }
    ]
    db = _Db({"documents": _CountingCollection(docs)})
    monkeypatch.setattr(
        da, "_DOC_LABEL_CACHE", da.TTLCache(maxsize=8, ttl_seconds=60.0)
    )

    table1 = await document_anchor_retriever._doc_label_table(db, ["corpus-1"])
    table2 = await document_anchor_retriever._doc_label_table(db, ["corpus-1"])

    assert find_calls["documents"] == 1  # second call served from cache
    assert table1 == table2
    labels, slim = table1[0]
    assert any("enterprise" in label.lower() for label in labels)
    assert set(slim) == {"doc_id", "corpus_id"}  # metadata blobs excluded
