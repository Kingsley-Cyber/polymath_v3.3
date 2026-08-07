"""Dependency-path extraction subsystem.

spaCy dependency-parse relation extraction (the structural lane of the
canonical Relex extractor). Each triple carries layered provenance,
confidence tier, and qualifiers.
"""

from services.extraction.dep_path_extractor import DepPathExtractor, EntitySpan, ExtractedTriple

__all__ = [
    "DepPathExtractor",
    "EntitySpan",
    "ExtractedTriple",
]
