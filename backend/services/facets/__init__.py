"""Canonical facet metadata helpers.

Facet creation is ingestion-time metadata only. Query-time routing can consume
these records later without depending on filename capitalization or spelling.
"""

from .normalizer import (
    FACET_SCHEMA_VERSION,
    build_ingest_facet_profile,
    canonical_display_name,
    normalize_facet_id,
)
from .final_selector import FacetCandidate, select_facet_final
from .runtime import (
    matching_ingest_facets,
    matching_vector_facets,
    metadata_facet_terms,
    metadata_with_facets,
)

__all__ = [
    "FACET_SCHEMA_VERSION",
    "build_ingest_facet_profile",
    "canonical_display_name",
    "FacetCandidate",
    "matching_ingest_facets",
    "matching_vector_facets",
    "metadata_facet_terms",
    "metadata_with_facets",
    "normalize_facet_id",
    "select_facet_final",
]
