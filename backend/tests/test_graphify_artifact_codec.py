from services.storage.graphify_artifact_codec import (
    INLINE_LIMIT_BYTES,
    decode_stage_payload,
    encode_stage_payload,
)


def _roundtrip(payload):
    fields, parts = encode_stage_payload(payload)
    head = {"artifact_id": "a1", **fields}
    return fields, parts, decode_stage_payload(head, lambda _a, _n: parts)


def test_small_payload_stays_inline_verbatim() -> None:
    payload = {"propositions": [{"s": "Harbor", "o": "SQLite"}], "report": {"n": 1}}
    fields, parts, decoded = _roundtrip(payload)
    assert fields == {"payload": payload}
    assert parts == []
    assert decoded == payload


def test_oversized_payload_shards_and_roundtrips_exactly() -> None:
    payload = {"rows": [{"evidence": "x" * 1000, "i": i} for i in range(12000)]}
    fields, parts, decoded = _roundtrip(payload)
    assert "payload" not in fields
    assert fields["payload_codec"] == "zlib-json/1"
    assert fields["payload_parts"] == len(parts) >= 1
    assert fields["payload_raw_bytes"] > INLINE_LIMIT_BYTES
    assert decoded == payload


def test_missing_part_is_a_hard_error() -> None:
    payload = {"rows": [{"evidence": "y" * 1000, "i": i} for i in range(12000)]}
    fields, parts = encode_stage_payload(payload)
    head = {"artifact_id": "a1", **fields}
    try:
        decode_stage_payload(head, lambda _a, _n: parts[:-1] if len(parts) > 1 else [])
    except RuntimeError as exc:
        assert "parts" in str(exc)
    else:
        raise AssertionError("short part list must not decode silently")
