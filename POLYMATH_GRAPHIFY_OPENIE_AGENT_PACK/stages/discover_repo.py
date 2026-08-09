from pathlib import Path
import json, re
from .common import utc_now, write_json, write_text, scan_files

CAPABILITY_PATTERNS = {
    "graphify_entrypoint": r"graphify|graph[_-]?ify|knowledge graph|extract.*graph",
    "ingestion_orchestrator": r"ingest|chunk|pipeline|orchestrator|control_plane",
    "model_loader": r"from_pretrained|GLiNER|glirel|relex|triplet|OpenIE|BART|REBEL",
    "spacy_construction": r"spacy\.load|Language\(|nlp\(",
    "dependency_matcher": r"DependencyMatcher|Matcher\(|PhraseMatcher|EntityRuler",
    "frame_extractor": r"FrameExtractor|frame extractor|frame_extractor",
    "svo": r"\bSVO\b|subject.*object|nsubj|dobj",
    "predicate_mapping": r"predicate|relation_acceptance|endpoint_signature|ontology|synonym",
    "claim_assertion": r"Claim|Assertion|polarity|modality|attribution",
    "mongo_writer": r"Mongo|pymongo|motor|mongo_writer",
    "neo4j_writer": r"Neo4j|neo4j|MERGE|UNWIND",
    "tests": r"pytest|unittest|gold|fixture|benchmark",
    "provider_aliases": r"GLiREL|RELEX|GLINER|LFM|REBEL|provider|engine",
}

EXPECTED = [
    "Graphify entrypoint", "ingestion orchestrator", "extraction model loader", "spaCy construction",
    "DependencyMatcher / PhraseMatcher / EntityRuler", "FrameExtractor", "SVO / dep-path",
    "predicate mappings", "claim/assertion schemas", "Mongo writers", "Neo4j writers", "tests", "provider aliases",
]


def run(context):
    started = utc_now()
    pack_root = Path(context["pack_root"])
    repo_root = Path(context["repo_root"])
    hits = scan_files(repo_root, CAPABILITY_PATTERNS)
    capability_map = {}
    for h in hits:
        capability_map.setdefault(h["capability"], []).append({"path": h["path"], "matches": h["matches"]})
    records = []
    for cap, files in capability_map.items():
        records.append({
            "capability": cap,
            "actual_paths": sorted(files, key=lambda x: (-x["matches"], x["path"]))[:20],
            "symbols_or_functions": [],
            "current_responsibility": "discovered_by_keyword_scan",
            "downstream_callers": [],
            "tests_protecting_it": [],
            "action": "INSPECT",
            "reason": "Repository agent must inspect these files and assign KEEP/EXTEND/ADAPT/REPLACE/REMOVE/NEW.",
        })
    out = {"generated_at": utc_now(), "repo_root": str(repo_root), "records": records, "hit_count": len(hits)}
    write_json(pack_root/"work/repository_map.json", out)
    md = ["# Repository discovery map", "", f"Generated: {out['generated_at']}", "", "## Discovered capabilities", ""]
    for rec in records:
        md.append(f"### {rec['capability']}")
        for f in rec["actual_paths"][:10]:
            md.append(f"- `{f['path']}` ({f['matches']} matches)")
        md.append("")
    if not records:
        md.append("No capabilities discovered. Verify repo root and scan rules.")
    write_text(pack_root/"work/repository_map.md", "\n".join(md))
    status = "PASSED" if records else "NEEDS_AGENT"
    return {"status": status, "started_at": started, "metrics": {"hit_count": len(hits), "capability_count": len(records)}, "warnings": [] if records else ["No capabilities discovered; repo agent must inspect repo root."]}
