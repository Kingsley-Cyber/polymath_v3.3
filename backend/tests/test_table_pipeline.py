from services.ghost_b import ExtractionTask, build_json_object_prompt, build_user_prompt


TABLE_METADATA = {
    "caption": "Table 2. Double Qwen performance on MeetingBank.",
    "columns": ["Component", "Model", "Size", "Role"],
    "row_count": 2,
    "source_format": "markdown_pipe_table",
}


TABLE_TEXT = """Table: Table 2
Section: Evaluation Results
Caption: Table 2. Double Qwen performance on MeetingBank.
Columns: Component | Model | Size | Role

Row 1: Component=Embedder; Model=Qwen3-Embedding-0.6B; Size=0.6B; Role=vector embeddings
Row 2: Component=Reranker; Model=Qwen3-Reranker-0.6B; Size=0.6B; Role=cross-encoder reranking
"""


def test_extraction_task_carries_table_kind_and_metadata():
    task = ExtractionTask(
        chunk_id="chunk_table",
        doc_id="doc",
        corpus_id="corpus",
        text=TABLE_TEXT,
        chunk_kind="table",
        metadata=TABLE_METADATA,
    )

    assert task.chunk_kind == "table"
    assert task.metadata["columns"] == ["Component", "Model", "Size", "Role"]


def test_jsonl_prompt_adds_table_extraction_rules():
    prompt = build_user_prompt(
        chunk_id="chunk_table",
        doc_id="doc",
        corpus_id="corpus",
        text=TABLE_TEXT,
        chunk_kind="table",
        metadata=TABLE_METADATA,
        enable_facts=True,
        max_facts=5,
    )

    assert "Table chunk rules:" in prompt
    assert "Use column headers as property_name" in prompt
    assert "Do not extract table numbers, captions, or column headers as standalone entities." in prompt
    assert "Qwen3-Embedding-0.6B" in prompt
    assert "Qwen3-Reranker-0.6B" in prompt


def test_json_object_prompt_adds_table_extraction_rules():
    prompt = build_json_object_prompt(
        chunk_id="chunk_table",
        doc_id="doc",
        corpus_id="corpus",
        text=TABLE_TEXT,
        chunk_kind="table",
        metadata=TABLE_METADATA,
        enable_facts=True,
        max_facts=5,
    )

    assert "Table chunk rules:" in prompt
    assert "Columns: Component, Model, Size, Role." in prompt
    assert "Prefer facts for numeric, categorical, size, status, score" in prompt
