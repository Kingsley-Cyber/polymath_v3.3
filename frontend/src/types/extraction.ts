// Extraction / graph types for Phase 9 — maps to Neo4j reader API responses

export interface EntityResult {
  entity_id: string;
  normalized_name: string;
  display_name: string;
  entity_type: "person" | "org" | "concept" | "other";
  confidence: number;
  mention_count: number;
}

export interface RelationEdge {
  subject_id: string;
  subject_name: string;
  predicate: string;
  relation_family?: string | null;
  object_id: string;
  object_name: string;
  confidence: number;
}

export interface ChunkExtractionResponse {
  chunk_id: string;
  corpus_id: string;
  entities: EntityResult[];
  relations: RelationEdge[];
}

export interface DocExtractionItem {
  chunk_id: string;
  entity_count: number;
  relation_count: number;
}
