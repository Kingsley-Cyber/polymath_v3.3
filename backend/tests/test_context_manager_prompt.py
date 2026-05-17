import os

os.environ.setdefault("LITELLM_MASTER_KEY", "test-litellm-master-key")
os.environ.setdefault("AUTH_SECRET_KEY", "test-auth-secret-key")
os.environ.setdefault("DEFAULT_ADMIN_PASSWORD", "test-admin-password")

from models.schemas import SourceChunk
from services.context_manager import context_manager


def test_augmented_prompt_hides_internal_corpus_names():
    source = SourceChunk(
        chunk_id="chunk-1",
        parent_id="parent-1",
        doc_id="doc-1",
        corpus_id="corpus-1",
        text="Gemma-class small models can run on-device with quantization.",
        score=0.9,
        source_tier="chunk",
        corpus_name="Phase5_Luau_v4",
        doc_name="mobile-notes.md",
    )

    prompt = context_manager.build_augmented_prompt(
        "How should small models be deployed on mobile?",
        [source],
    )

    assert 'from "mobile-notes.md"' in prompt
    assert "Phase5_Luau_v4" not in prompt
    assert 'in "Phase5_Luau_v4"' not in prompt
