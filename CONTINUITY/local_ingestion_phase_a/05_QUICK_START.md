# Quick Start — resume Phase A

This is the exact "what to do next" guide. Follow in order.

## 0. Sanity checks (run these first)

```bash
# Confirm repo state
cd /Users/king/polymath_v3.3
git status                        # should be roughly clean (some pre-existing untracked, expected)
git log --oneline -3              # HEAD should be 53b04ca or later

# Confirm GLiREL weights are present
ls -lh models/glirel_ghost_b_v1/best/    # pytorch_model.bin should be ~1.87 GB

# Confirm GLiNER + transformers available
cd local_ghost_b
.venv/bin/python -c "import gliner, glirel, transformers, torch; print('OK')"

# Confirm Mac MPS available (for the in-process GLiNER + GLiREL)
.venv/bin/python -c "import torch; print('mps:', torch.backends.mps.is_available())"
```

## 1. Read the continuity folder in order

```
CONTINUITY/local_ingestion_phase_a/
  README.md
  00_LOCKED_DECISIONS.md
  01_ARCHITECTURE.md
  02_PHASE_A_TASKS.md             ← detailed task list
  03_FILE_MAP.md                  ← what to touch / not touch
  04_PRIOR_EXPERIMENTS.md         ← don't re-run these
  05_QUICK_START.md               ← this file
  06_BEHAVIORAL_RULES.md          ← user-set rules
```

## 2. Start Phase A.1 — Inspect cloud emit path

```bash
# Read the cloud extractor's data layer
sed -n '1670,1790p' /Users/king/polymath_v3.3/backend/services/ghost_b.py
# This shows EntityItem, RelationItem, FactItem, ExtractionResult dataclasses.

# Read where the worker calls Ghost B
sed -n '750,830p' /Users/king/polymath_v3.3/backend/services/ingestion/worker.py
# This is _b_branch() — what A.5 will modify.

# Read the cloud extractor's main entry point
grep -nE 'async def extract_entities|def extract_entities' /Users/king/polymath_v3.3/backend/services/ghost_b.py
# Find the signature local Ghost B must match.
```

**A.1 deliverable**: a short field-by-field map (mentally or in a scratch file) noting which `ExtractionResult` / `EntityItem` / `RelationItem` / `FactItem` fields the local extractor must populate. Confidence defaults, `evidence_phrase` format, the Phase-14 counters (`entity_remap_count` etc. — likely 0 for local lane).

## 3. Then proceed to A.4 → A.3 → A.2 → A.5 → A.6 → A.7

See `02_PHASE_A_TASKS.md` for the full deliverable spec of each.

## 4. Test fixture for A.6 (smoke test)

A small, fast file from the merged corpus:

```bash
# Recommended: flame_engine_docs_complete.md (9 KB, 148 lines)
ls -la "/Volumes/Flash Drive/merged/flame_engine_docs_complete.md"

# Alternative if Flash Drive isn't mounted: use the existing chunks JSONL
ls /Users/king/polymath_v3.3/local_ghost_b/flame_chunks.jsonl
# But this file is gitignored and may not exist on a fresh clone
```

## 5. Commits

Commit author MUST be Kingsley (NOT the system default which is `King <king@Kings-Mac-Studio.local>`):

```bash
git -c user.name="Kingsley" -c user.email="ezeokonkwokingsley@gmail.com" \
    commit -m "$(cat <<'EOF'
Short title here (imperative, ~60 chars, repo style: 'Tune embedding batches for MLX')

Optional body (wrapped at 72 chars).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

(Use Opus 4.8 in the co-author line if running on that model.)

**Don't push to origin/main without confirming with the user.** Use `git push origin main` only after user OK.

## 6. Useful existing imports for ghost_b_local.py

```python
# Bring in the canonical GLiREL inference
import sys
sys.path.insert(0, '/Users/king/polymath_v3.3/local_ghost_b')
from glirel_infer import GliRELClassifier, to_record   # extract_chunk API

# Bring in the safety rules
from safety_rules import type_plausible, guard_dangerous

# Bring in enrich helpers
from backend.services.ingestion.enrich import extract as pass1_extract
from backend.services.ingestion.enrich import (
    extract_qualitative_facts,   # NEW in A.4
    qualitative_cue_hits,
)

# Cloud-side dataclasses (DON'T import ghost_b's other internals)
from backend.services.ghost_b import (
    ExtractionResult, EntityItem, RelationItem, FactItem
)

# Pydantic validators (use these before constructing the dataclasses)
from backend.services.ghost_b_schemas import LLMEntity, LLMFact, LLMRelation, FactType
```

## 7. If you get stuck on A.1 — fallback inspection commands

```bash
# How is ExtractionResult constructed in cloud Ghost B?
grep -nB 2 -A 15 'return ExtractionResult' /Users/king/polymath_v3.3/backend/services/ghost_b.py | head -50

# What does graph_backfill expect from ExtractionResult?
grep -nE 'ExtractionResult|extraction\.' /Users/king/polymath_v3.3/backend/services/ingestion/graph_backfill.py | head -20

# Where is the Phase-14 counters logic?
grep -nE 'entity_remap_count|evidence_drop_count|fact_drop_count' /Users/king/polymath_v3.3/backend/services/ghost_b.py | head -10
```

## 8. Failure modes to watch

- **GLiNER pass-2 returns empty for niche entities** → fallback: set `object_kind = entity.entity_type` (use the type as a generic facet)
- **Qualitative-fact rules over-trigger on common words** → tighten regex (e.g., require "is/are/was" before status words)
- **`ExtractionResult` schema mismatch** → A.1's enumeration should catch this; A.6 smoke confirms
- **Author identity mismatch on commit** → see step 5; ALWAYS pass `-c user.name=... -c user.email=...` to git commit
