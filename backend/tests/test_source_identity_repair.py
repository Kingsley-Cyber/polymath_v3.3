from __future__ import annotations

from scripts.repair_incidental_source_identities import repair_patch


def test_repair_patch_rekeys_colliding_channel_url_to_content_hash():
    content_hash = "a" * 64
    patch = repair_patch(
        {
            "source_key": "url:https://www.youtube.com/oreillymedia",
            "source_identity": {
                "source_kind": "url",
                "source_key": "url:https://www.youtube.com/oreillymedia",
                "source_url_canonical": "https://www.youtube.com/oreillymedia",
                "content_sha256": content_hash,
            },
        }
    )

    assert patch is not None
    assert patch["$set"]["source_key"] == f"sha256:{content_hash}"
    assert patch["$set"]["source_identity.source_kind"] == "content_hash"
    assert "source_identity.source_url_canonical" in patch["$unset"]


def test_repair_patch_never_rekeys_concrete_youtube_video():
    assert (
        repair_patch(
            {
                "source_identity": {
                    "source_kind": "url",
                    "source_key": "url:https://www.youtube.com/watch?v=6h4tsfwfyzo",
                    "source_url_canonical": "https://www.youtube.com/watch?v=6h4tsfwfyzo",
                    "content_sha256": "b" * 64,
                }
            }
        )
        is None
    )
