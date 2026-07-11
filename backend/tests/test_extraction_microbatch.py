from services.ghost_b import parse_extraction_microbatch_response


def test_extraction_microbatch_salvages_valid_siblings_and_preserves_ids() -> None:
    parsed = parse_extraction_microbatch_response(
        "prefix <json_payload>"
        '{"items":['
        '{"target_id":"chunk-1","artifact":{"entities":[],"relations":[],"facts":[]}},'
        '{"target_id":"chunk-2","artifact":"bad"},'
        '{"target_id":"unknown","artifact":{"entities":[]}}'
        "]}</json_payload> suffix",
        allowed_target_ids={"chunk-1", "chunk-2"},
    )

    assert parsed == {
        "chunk-1": {"entities": [], "relations": [], "facts": []}
    }
