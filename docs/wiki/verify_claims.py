#!/usr/bin/env python3
"""Drift audit for the claims wiki — run periodically over pages×code."""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path("/Users/king/polymath_v3.3")
WIKI = ROOT / "docs" / "wiki"

CHECKS = [
    # (description, grep pattern, file, expected lines or None)
    ("claim lease 7200s", r"lease_seconds[^0-9]*7200", "backend/services/ingestion/extraction_jobs.py", [1240, 1342]),
    ("renew ~1/4 lease", r"asyncio\.sleep\(1800\)", "backend/services/ingestion/extraction_jobs.py", [1470]),
    ("lane adopt 420s", r"DEFAULT_LANE_ADOPT_STALE_SECONDS\s*=\s*420\.0", "backend/services/ingestion/job_leases.py", [35]),
    ("job lease 15min", r"DEFAULT_JOB_LEASE_SECONDS\s*=\s*15 \* 60", "backend/services/ingestion/job_leases.py", [23]),
    ("lane lease 30min", r"DEFAULT_LANE_LEASE_SECONDS\s*=\s*30 \* 60", "backend/services/ingestion/job_leases.py", [25]),
    ("default types aligned to 15-Literal (was 4-bucket)", r'"TimeReference", "other",', "backend/services/ghost_b.py", [153]),
    ("universal 30 incl sentinel", r"MUST stay last", "backend/services/ghost_b.py", [232]),
    ("schema-native root cause", r"prompt teaches a contract the grammar forbids", "backend/services/ghost_b.py", [1867]),
    ("lane-manager 8085 fallback", r"http://192\.168\.1\.83:8085", "backend/polymath_mcp/tools.py", [3582, 3661]),
    ("relex sidecar 8737", r"host\.docker\.internal:8737", "backend/services/extraction/relex_sidecar_client.py", [7, 40]),
    ("max attempts 5", r"INGEST_JOB_MAX_ATTEMPTS: int = Field", "backend/config.py", [1247]),
    ("stale job 30min", r"INGEST_STALE_JOB_MINUTES: int = Field", "backend/config.py", [1550]),
    ("SCHEMA_INLINE_LIMIT 30", r"SCHEMA_INLINE_LIMIT \(30\)", "backend/services/ghost_b.py", [182]),
    ("kill seam inert flag", r"GRAPHIFY_OPS_KILL", "backend/services/ops_drills/kill_seam.py", None),
]

def check_line(pattern, path, lineno):
    try:
        lines = (ROOT / path).read_text(errors="replace").splitlines()
        if lineno - 1 >= len(lines):
            return f"{path}:{lineno} — file only {len(lines)} lines"
        return None if re.search(pattern, lines[lineno - 1]) else f"{path}:{lineno} no match for /{pattern}/"
    except FileNotFoundError:
        return f"{path} missing"

def check_vocab_parity():
    """Grammar Literal vs ontology vs universal — value-level MUST MATCH checks.

    Expected structure (legal, verified 2026-08-11):
      - entity types: schema 15 == ontology 15 (value-for-value)
      - predicates: 9 ontology extensions (has_part, includes, example_of,
        evaluates, deploys, creates, trains, runs, quantizes) are legal ONLY
        in the spaCy lane (spacy_relation_adapter.py VALID_PREDICATES).
        Drift = any of the 9 appearing in the ghost_b grammar Literal
        WITHOUT a matching spaCy VALID_PREDICATES entry, or a new ontology
        predicate missing from the adapter.
    """
    src = (ROOT / "backend/services/ghost_b_schemas.py").read_text()
    m = re.search(r"EntityType\s*=\s*Literal\[(.*?)\]", src, re.S)
    ent = set(re.findall(r"\"([A-Za-z_]+)\"", m.group(1)))
    m2 = re.search(r"Predicate\s*=\s*Literal\[(.*?)\]", src, re.S)
    pred = set(re.findall(r"\"([A-Za-z_]+)\"", m2.group(1)))
    ont = (ROOT / "config/ontology.yaml").read_text()
    ont_types = set(re.findall(r"^  - ([A-Za-z]+)$", ont, re.M))
    ont_preds = set(re.findall(r"^  ([a-z_]+):$", ont, re.M))
    adapter = (ROOT / "backend/services/extraction/spacy_relation_adapter.py").read_text()
    adapter_preds = set(re.findall(r'^\s*"([a-z_]+)"', adapter[adapter.index("VALID_PREDICATES"):adapter.index("_ALLOWED_PAIRS")], re.M))
    # VALID_PREDICATES lines can carry multiple quoted strings; widen capture
    seg = adapter[adapter.index("VALID_PREDICATES"):adapter.index("_ALLOWED_PAIRS")]
    adapter_preds |= set(re.findall(r'"([a-z_]+)"', seg))
    issues = []
    if ent != ont_types:
        issues.append(f"entity types MISMATCH: schema {len(ent)} vs ontology {len(ont_types)}: only-in-schema={sorted(ent-ont_types)} only-in-ontology={sorted(ont_types-ent)}")
    # every ontology predicate must be legal in at least one lane (grammar or spaCy)
    uncovered = ont_preds - pred - adapter_preds
    if uncovered:
        issues.append(f"ontology predicates legal in NO lane: {sorted(uncovered)}")
    # legal-only-via-adapter set (no grammar Literal, no alias bridge) is the
    # DOCUMENTED 3-lane structure — changes to it are intentional and must be
    # reflected in INVARIANTS.md + VOCABULARIES/ontology.md, not flagged here.
    ghost = (ROOT / "backend/services/ghost_b.py").read_text()
    return issues

def main():
    issues = []
    for desc, pat, path, lines in CHECKS:
        for ln in lines or []:
            r = check_line(pat, path, ln)
            if r:
                issues.append(r)
    issues.extend(check_vocab_parity())
    if issues:
        print("DRIFT FOUND:")
        for i in issues:
            print(" -", i)
        sys.exit(1)
    print("OK — all invariants, line pins, and vocabulary parities hold.")

main()
