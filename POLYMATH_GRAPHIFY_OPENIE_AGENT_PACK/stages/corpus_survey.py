from pathlib import Path
from collections import Counter
import hashlib, re, json
from .common import utc_now, write_json


def block_hash(block: str) -> str:
    norm = re.sub(r"\s+", " ", block.strip().lower())
    return hashlib.sha256(norm.encode()).hexdigest()[:16]


def run(context):
    started = utc_now()
    pack_root = Path(context["pack_root"])
    fixture_dir = pack_root / "fixtures"
    blocks = []
    headings = []
    terms = []
    for md in fixture_dir.glob("*.md"):
        text = md.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("#"):
                headings.append(line.strip())
            for m in re.finditer(r"\b[A-Z][A-Za-z0-9_.-]{2,}\b|\b[A-Za-z]+\d[A-Za-z0-9_.-]*\b", line):
                terms.append(m.group(0))
        for block in re.split(r"\n\s*\n", text):
            if block.strip():
                blocks.append(block.strip())
    counts = Counter(block_hash(b) for b in blocks)
    survey = {
        "generated_at": utc_now(),
        "fixture_blocks": len(blocks),
        "repeated_block_hashes": {k: v for k, v in counts.items() if v > 1},
        "heading_inventory": headings[:500],
        "candidate_terms_top": Counter(terms).most_common(200),
        "gazetteer_tiers": ["CURATED", "EXPLICIT_DOCUMENT", "SURVEY_DERIVED"],
        "note": "Repo agent must run equivalent survey over target corpus before GLiNER2 census.",
    }
    write_json(pack_root/"work/survey/corpus_survey.json", survey)
    return {"status": "PASSED", "started_at": started, "metrics": {"fixture_blocks": len(blocks), "candidate_terms": len(set(terms))}}
