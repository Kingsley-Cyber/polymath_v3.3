"""Mongo read-boundary normalization for strict immutable contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def restore_bson_utc_awareness(value: Any) -> Any:
    """Restore timezone awareness stripped by the shared Motor client.

    BSON datetimes are UTC by definition. This helper is intentionally limited
    to Mongo read results immediately before strict contract parsing; it must
    not be used to reinterpret arbitrary application timestamps.
    """

    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, dict):
        return {
            key: restore_bson_utc_awareness(item) for key, item in value.items()
        }
    if isinstance(value, list):
        return [restore_bson_utc_awareness(item) for item in value]
    if isinstance(value, tuple):
        return tuple(restore_bson_utc_awareness(item) for item in value)
    return value
