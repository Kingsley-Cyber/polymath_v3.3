"""Codec for Graphify stage artifacts that outgrow one Mongo document.

Small payloads persist inline (``payload`` field) exactly as before — every
existing reader keeps working. A payload whose JSON encoding exceeds the
inline threshold is zlib-compressed and sharded into fixed-size binary parts
stored in ``graphify_stage_artifact_parts``; the head document records the
codec and part count instead of an inline payload. Encoding is deterministic
(sorted-key-free JSON of an insertion-ordered dict, fixed compression level,
fixed shard size) so identical payloads always produce identical blobs.
"""
from __future__ import annotations

import json
import zlib
from typing import Any, Callable, Sequence

PARTS_COLLECTION = "graphify_stage_artifact_parts"
PAYLOAD_CODEC = "zlib-json/1"
INLINE_LIMIT_BYTES = 8 * 1024 * 1024
PART_SIZE_BYTES = 12 * 1024 * 1024
_COMPRESSION_LEVEL = 6


def encode_stage_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], list[bytes]]:
    """Return (head document fields, ordered part blobs; empty when inline)."""
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) <= INLINE_LIMIT_BYTES:
        return {"payload": payload}, []
    blob = zlib.compress(raw, _COMPRESSION_LEVEL)
    parts = [blob[index:index + PART_SIZE_BYTES] for index in range(0, len(blob), PART_SIZE_BYTES)]
    return {
        "payload_codec": PAYLOAD_CODEC,
        "payload_parts": len(parts),
        "payload_raw_bytes": len(raw),
        "payload_compressed_bytes": len(blob),
    }, parts


def decode_stage_payload(
    head: dict[str, Any],
    fetch_parts: Callable[[str, int], Sequence[bytes]],
) -> dict[str, Any]:
    """Materialize a head document's payload, inline or sharded.

    ``fetch_parts(artifact_id, expected_count)`` must return the part blobs
    in part order.
    """
    if "payload" in head:
        return head["payload"]
    codec = head.get("payload_codec")
    if codec != PAYLOAD_CODEC:
        raise RuntimeError(
            f"artifact {head.get('artifact_id')} has no payload and unknown codec {codec!r}"
        )
    expected = int(head["payload_parts"])
    parts = list(fetch_parts(str(head["artifact_id"]), expected))
    if len(parts) != expected:
        raise RuntimeError(
            f"artifact {head.get('artifact_id')} expected {expected} parts, got {len(parts)}"
        )
    raw = zlib.decompress(b"".join(bytes(part) for part in parts))
    if len(raw) != int(head.get("payload_raw_bytes", len(raw))):
        raise RuntimeError(f"artifact {head.get('artifact_id')} decoded size mismatch")
    return json.loads(raw.decode("utf-8"))
