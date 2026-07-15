from services.ingestion.gliner_mentions import select_gliner_mentions


def test_select_gliner_mentions_is_controlled_exact_and_deterministic():
    text = "Discounting lowers reference prices."
    rows = [
        {"start": 0, "end": 11, "text": "Discounting", "label": "PROCESS", "score": 0.7},
        {"start": 0, "end": 11, "text": "Discounting", "label": "METHOD", "score": 0.8},
        {"start": 19, "end": 35, "text": "reference prices", "label": "CONCEPT", "score": 0.9},
        {"start": 19, "end": 28, "text": "reference", "label": "QUALITY", "score": 0.6},
        {"start": 0, "end": 4, "text": "wrong", "label": "METHOD", "score": 0.99},
        {"start": 12, "end": 18, "text": "lowers", "label": "NOT_ALLOWED", "score": 0.9},
    ]

    first, counts = select_gliner_mentions(
        document_id="doc:test",
        child_id="child:test",
        text=text,
        raw_entities=rows,
        controlled_types=["METHOD", "PROCESS", "QUALITY", "CONCEPT"],
    )
    second, second_counts = select_gliner_mentions(
        document_id="doc:test",
        child_id="child:test",
        text=text,
        raw_entities=reversed(rows),
        controlled_types=["METHOD", "PROCESS", "QUALITY", "CONCEPT"],
    )

    assert first == second
    assert counts == second_counts
    assert [(item.text, item.entity_type) for item in first] == [
        ("Discounting", "METHOD"),
        ("reference prices", "CONCEPT"),
    ]
    assert all(text[item.start_char : item.end_char] == item.text for item in first)
    assert all(item.mention_id.startswith("mention:") for item in first)
    assert counts == {
        "raw": 6,
        "same_span_dropped": 1,
        "offset_violations": 1,
        "label_violations": 1,
        "overlap_dropped": 1,
        "selected": 2,
    }


def test_select_gliner_mentions_rejects_non_finite_or_out_of_range_scores():
    mentions, counts = select_gliner_mentions(
        document_id="doc:test",
        child_id="child:test",
        text="alpha",
        raw_entities=[
            {"start": 0, "end": 5, "text": "alpha", "label": "CONCEPT", "score": 1.1},
            {"start": 0, "end": 5, "text": "alpha", "label": "CONCEPT", "score": float("nan")},
        ],
        controlled_types=["CONCEPT"],
    )

    assert mentions == []
    assert counts == {"raw": 2, "score_violations": 2, "selected": 0}
