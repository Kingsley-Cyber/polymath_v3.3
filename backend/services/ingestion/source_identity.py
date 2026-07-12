"""Deterministic source identity helpers for ingestion guardrails.

Content hashes catch exact duplicate bytes, but agent workflows often fetch
the same source through different transcript exporters or temporary file URLs.
This module gives ingestion a stable external identity first (YouTube video id,
canonical URL), then falls back to content hash / filename when no better source
is available.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse


_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}
_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
_ANY_YOUTUBE_URL_RE = re.compile(
    r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be|youtube-nocookie\.com)/[^\s<>)\"']+",
    re.IGNORECASE,
)
_DECLARED_URL_RE = re.compile(
    r"(?im)^\s*(?:video\s+url|youtube\s+url|source\s+url|url|source)\s*:\s*(https?://\S+)\s*$"
)
_TITLE_RE = re.compile(
    r"(?im)^\s*(?:video|title|document|doc|name)\s*:\s*(.+?)\s*$"
)
_MEANINGLESS_STEMS = {
    "",
    "download",
    "file",
    "upload",
    "document",
    "doc",
    "untitled",
    "transcript",
    "captions",
    "subtitles",
    "export",
    "data",
}
_TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "si",
    "spm",
}


def _clean_url_token(value: str) -> str:
    return value.strip().strip("<>()[]{}\"'.,;")


def extract_declared_source_url(data: bytes | str | None) -> str | None:
    """Return a source URL declared in the first part of transcript text.

    Many agent-created transcript files start with a small header, e.g.
    ``URL: https://youtu.be/...``. Prefer that URL over a temporary CDN/file
    URL because it identifies the video, not the transport.
    """
    if data is None:
        return None
    if isinstance(data, bytes):
        text = data[:32768].decode("utf-8", errors="ignore")
    else:
        text = str(data)[:32768]
    match = _DECLARED_URL_RE.search(text)
    if match:
        return _clean_url_token(match.group(1))
    match = _ANY_YOUTUBE_URL_RE.search(text)
    if match:
        candidate = _clean_url_token(match.group(0))
        # An unlabeled channel/profile link in an ebook footer is provenance,
        # not the identity of the uploaded document. Only a concrete video URL
        # is strong enough to override the content hash without a declaration.
        if extract_youtube_video_id(candidate):
            return candidate
    return None


def extract_youtube_video_id(url: str | None) -> str | None:
    """Extract a YouTube video id from common public URL shapes."""
    if not url:
        return None
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return None
    host = (parsed.hostname or "").lower()
    if host not in _YOUTUBE_HOSTS:
        return None

    if host.endswith("youtu.be"):
        candidate = parsed.path.strip("/").split("/", 1)[0]
        return candidate if _YOUTUBE_ID_RE.match(candidate) else None

    query = dict(parse_qsl(parsed.query, keep_blank_values=False))
    candidate = query.get("v")
    if candidate and _YOUTUBE_ID_RE.match(candidate):
        return candidate

    parts = [p for p in parsed.path.split("/") if p]
    for marker in ("embed", "shorts", "live", "v"):
        if marker in parts:
            idx = parts.index(marker)
            if idx + 1 < len(parts):
                candidate = parts[idx + 1]
                return candidate if _YOUTUBE_ID_RE.match(candidate) else None
    return None


def canonicalize_source_url(url: str | None) -> str | None:
    """Return a stable URL key, stripping fragments and tracker params."""
    if not url:
        return None
    raw = url.strip()
    if not raw:
        return None
    youtube_id = extract_youtube_video_id(raw)
    if youtube_id:
        return f"https://www.youtube.com/watch?v={youtube_id}"
    try:
        parsed = urlparse(raw)
    except Exception:
        return raw
    scheme = (parsed.scheme or "https").lower()
    host = (parsed.hostname or "").lower()
    if not host:
        return raw
    netloc = host
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    path = quote(parsed.path or "/", safe="/%:@")
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ]
    query = urlencode(sorted(query_pairs), doseq=True)
    return urlunparse((scheme, netloc, path, "", query, ""))


def _normalized_filename(filename: str | None) -> str | None:
    if not filename:
        return None
    return re.sub(r"\s+", " ", filename.strip()).lower() or None


def extract_source_title(data: bytes | str | None) -> str | None:
    """Extract a document/video title from a small text header."""
    if data is None:
        return None
    if isinstance(data, bytes):
        text = data[:32768].decode("utf-8", errors="ignore")
    else:
        text = str(data)[:32768]
    for match in _TITLE_RE.finditer(text):
        value = match.group(1).strip()
        if not value or value.lower().startswith(("http://", "https://")):
            continue
        return re.sub(r"\s+", " ", value).strip(" ._-")
    return None


def _slugify_filename_stem(value: str | None, *, default: str = "document") -> str:
    value = (value or "").strip()
    value = re.sub(r"[^\w\s.-]+", " ", value, flags=re.ASCII)
    value = re.sub(r"[_\s]+", "-", value.lower())
    value = re.sub(r"-{2,}", "-", value).strip(".-")
    return value or default


def _filename_extension(filename: str | None, content_type: str | None) -> str:
    stem, ext = os.path.splitext(filename or "")
    if ext and re.match(r"^\.[A-Za-z0-9]{1,8}$", ext):
        return ext.lower()
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
        if guessed:
            return guessed.lower()
    return ".txt"


def _url_path_stem(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    tail = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if not tail:
        return parsed.hostname
    stem, _ext = os.path.splitext(tail)
    return stem or tail


def _filename_stem(filename: str | None) -> str | None:
    if not filename:
        return None
    stem, _ext = os.path.splitext(os.path.basename(filename.strip()))
    return stem or None


def _is_meaningless_stem(value: str | None) -> bool:
    if value is None:
        return True
    stem = _slugify_filename_stem(value, default="")
    stem = re.sub(r"[-_.]+", "-", stem).strip("-")
    return stem in _MEANINGLESS_STEMS or stem.startswith(("tmp-", "temp-"))


def _cap_filename_stem(stem: str, suffix: str | None, max_len: int = 120) -> str:
    if len(stem) <= max_len:
        return stem
    if suffix and stem.endswith(f"-{suffix}"):
        room = max(16, max_len - len(suffix) - 1)
        return f"{stem[:room].rstrip('-')}-{suffix}"
    return stem[:max_len].rstrip("-")


def build_deterministic_filename(
    *,
    filename: str | None = None,
    source_url: str | None = None,
    content_type: str | None = None,
    data: bytes | str | None = None,
    source_identity: dict[str, Any] | None = None,
) -> str:
    """Return the enforced filename for ingestion.

    The name is deterministic and source-derived:
      - YouTube/video transcript: title/header + ``yt-<video_id>``.
      - URL document: title or URL basename + short URL hash.
      - Raw upload: preserve a meaningful filename; otherwise use title/hash.
    """
    identity = source_identity or build_source_identity(
        filename=filename,
        source_url=source_url,
        content_type=content_type,
        data=data,
    )
    ext = _filename_extension(filename, content_type)
    title = extract_source_title(data)
    original_stem = _filename_stem(filename)
    source_kind = identity.get("source_kind")
    youtube_id = identity.get("youtube_video_id")
    canonical_url = identity.get("source_url_canonical")
    content_sha = identity.get("content_sha256")

    suffix = None
    if source_kind == "youtube_video" and youtube_id:
        base = (
            title
            or (original_stem if not _is_meaningless_stem(original_stem) else None)
            or "youtube-video"
        )
        suffix = f"yt-{youtube_id}"
        ext = ".txt" if ext in ("", ".bin") else ext
    elif canonical_url:
        base = (
            title
            or _url_path_stem(canonical_url)
            or (original_stem if not _is_meaningless_stem(original_stem) else None)
            or "web-document"
        )
        suffix = hashlib.sha1(canonical_url.encode("utf-8")).hexdigest()[:10]
    elif title:
        base = title
        if content_sha and _is_meaningless_stem(original_stem):
            suffix = f"sha-{content_sha[:10]}"
    elif original_stem and not _is_meaningless_stem(original_stem):
        base = original_stem
    else:
        base = "document"
        if content_sha:
            suffix = f"sha-{content_sha[:10]}"

    stem = _slugify_filename_stem(base)
    if suffix and not stem.endswith(suffix.lower()):
        stem = f"{stem}-{suffix.lower()}"
    stem = _cap_filename_stem(stem, suffix.lower() if suffix else None)
    return f"{stem}{ext}"


def build_source_identity(
    *,
    filename: str | None = None,
    source_url: str | None = None,
    content_type: str | None = None,
    data: bytes | str | None = None,
) -> dict[str, Any]:
    """Build the source identity payload stored on each document.

    Priority:
      1. YouTube video id from source_url or declared transcript header.
      2. Canonical public URL.
      3. Content hash for raw uploads.
      4. Normalized filename as a weak planning hint.
    """
    declared_url = extract_declared_source_url(data)
    urls = [u for u in (declared_url, source_url) if u]
    youtube_url = next((u for u in urls if extract_youtube_video_id(u)), None)
    chosen_url = youtube_url or source_url or declared_url
    canonical_url = canonicalize_source_url(chosen_url)
    youtube_id = extract_youtube_video_id(chosen_url)

    content_sha256 = None
    if data is not None:
        raw = data if isinstance(data, bytes) else str(data).encode("utf-8")
        content_sha256 = hashlib.sha256(raw).hexdigest()

    filename_norm = _normalized_filename(filename)
    if youtube_id:
        source_kind = "youtube_video"
        source_key = f"youtube:{youtube_id}"
    elif canonical_url:
        source_kind = "url"
        source_key = f"url:{canonical_url}"
    elif content_sha256:
        source_kind = "content_hash"
        source_key = f"sha256:{content_sha256}"
    elif filename_norm:
        source_kind = "filename"
        source_key = f"filename:{filename_norm}"
    else:
        source_kind = "unknown"
        source_key = None

    return {
        "source_kind": source_kind,
        "source_key": source_key,
        "source_url": source_url,
        "declared_source_url": declared_url,
        "source_url_canonical": canonical_url,
        "youtube_video_id": youtube_id,
        "content_sha256": content_sha256,
        "filename_normalized": filename_norm,
        "content_type": content_type,
        "identity_version": "source_identity.v1",
    }


def source_identity_doc_fields(
    *,
    source_url: str | None = None,
    source_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Flatten source identity into a compact Mongo document patch."""
    fields: dict[str, Any] = {}
    if source_url:
        fields["source_url"] = source_url
    if source_identity:
        clean = {k: v for k, v in source_identity.items() if v is not None}
        if clean:
            fields["source_identity"] = clean
        for key in (
            "source_key",
            "source_kind",
            "source_url_canonical",
            "youtube_video_id",
            "deterministic_filename",
            "original_filename",
        ):
            value = source_identity.get(key)
            if value is not None:
                fields[key] = value
    return fields
