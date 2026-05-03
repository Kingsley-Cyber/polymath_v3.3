from __future__ import annotations


class IngestCancelled(RuntimeError):
    """Raised when queued/running ingest work is cancelled by control-plane state."""
