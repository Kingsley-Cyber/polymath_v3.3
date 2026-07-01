from __future__ import annotations

from services.ingestion.source_identity import (
    build_deterministic_filename,
    build_source_identity,
    canonicalize_source_url,
    extract_declared_source_url,
    extract_youtube_video_id,
)


def test_youtube_video_id_from_common_urls():
    assert extract_youtube_video_id("https://youtu.be/6h4tsfwfyzo?t=22") == "6h4tsfwfyzo"
    assert (
        extract_youtube_video_id("https://www.youtube.com/watch?v=6h4tsfwfyzo&list=x")
        == "6h4tsfwfyzo"
    )
    assert (
        extract_youtube_video_id("https://www.youtube.com/shorts/6h4tsfwfyzo")
        == "6h4tsfwfyzo"
    )


def test_youtube_canonicalization_strips_transport_noise():
    assert (
        canonicalize_source_url("https://youtu.be/6h4tsfwfyzo?si=abc&t=20")
        == "https://www.youtube.com/watch?v=6h4tsfwfyzo"
    )


def test_transcript_header_url_beats_temporary_file_url():
    data = b"""Video: How to build a Modern EDITABLE Grid
URL: https://youtu.be/6h4tsfwfyzo
Duration: 17:33

[0:00] hello everyone
"""
    assert extract_declared_source_url(data) == "https://youtu.be/6h4tsfwfyzo"
    identity = build_source_identity(
        filename="downloaded_transcript.txt",
        source_url="https://temporary.example.com/shopify_vid1_full.txt",
        data=data,
    )

    assert identity["source_kind"] == "youtube_video"
    assert identity["source_key"] == "youtube:6h4tsfwfyzo"
    assert identity["source_url_canonical"] == "https://www.youtube.com/watch?v=6h4tsfwfyzo"
    assert identity["content_sha256"]


def test_deterministic_youtube_filename_uses_video_title_and_id():
    data = b"""Video:    How to build a Modern EDITABLE Grid using Gallery in Power Apps (2025)
URL:      https://youtu.be/6h4tsfwfyzo
Duration: 17:33

[0:00] hello everyone
"""
    identity = build_source_identity(
        filename="shopify_vid1_full.txt",
        source_url="https://temporary.example.com/shopify_vid1_full.txt",
        data=data,
    )

    assert build_deterministic_filename(
        filename="shopify_vid1_full.txt",
        source_url="https://temporary.example.com/shopify_vid1_full.txt",
        data=data,
        source_identity=identity,
    ) == "how-to-build-a-modern-editable-grid-using-gallery-in-power-apps-2025-yt-6h4tsfwfyzo.txt"


def test_deterministic_youtube_filename_ignores_generic_transcript_name():
    assert build_deterministic_filename(
        filename="transcript.txt",
        source_url="https://youtu.be/6h4tsfwfyzo",
    ) == "youtube-video-yt-6h4tsfwfyzo.txt"


def test_deterministic_raw_upload_preserves_meaningful_filename():
    assert build_deterministic_filename(
        filename="Shopify Product Research Notes.txt",
        data=b"same bytes every time",
    ) == "shopify-product-research-notes.txt"


def test_raw_upload_falls_back_to_content_hash():
    identity = build_source_identity(
        filename="notes.txt",
        data=b"same bytes every time",
    )

    assert identity["source_kind"] == "content_hash"
    assert identity["source_key"].startswith("sha256:")
