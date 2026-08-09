# Handoff — Graph view renders every node as "concept" (GLiNER/GLiREL classification not visible)

**Author:** Claude (Opus 4.8) — investigation 2026-06-14, 9-agent workflow + adversarial audit, all citations re-verified against live code/data.
**For:** Codex (UI/UX lane). **Mandate:** validate every finding below FIRST, then implement the full fix.
**Repo root:** `/Users/king/polymath_v3.3`

---

## 0. TL;DR

User reports: in the graph view, **every node shows as "concept"**, nothing shows as Person/Organization/Product, and edges show no GLiREL semantic relation.

This is **NOT** a GLiNER/GLiREL / extraction / storage problem. Neo4j holds rich, diverse classification and the backend read queries project it correctly. The bug is **the default landing graph (Brain View) only** — a book-centric overview whose orbiting entities are returned **type-less** by the backend and then **hardcoded to `entity_type:"Concept"` by the frontend**. The fully-typed entity graph (GLiNER types + GLiREL predicates) **already renders correctly** on the drill-down and Graph-Query paths.

Fix = (A) backend: project per-entity type on the brain-view `top_entities`; (B) frontend: stop hardcoding `"Concept"`, read the real type. Plus optional secondary cleanups (§6).

---

## 1. Ground truth — the data is fine (verify first, §5.1)

Neo4j (`NEO4J_ENABLED=true`) holds:
- **Entity nodes (796k):** props include `primary_entity_type`, `entity_type`, `observed_entity_types`, `canonical_family`. Distribution of `primary_entity_type`: **Concept ~35% (280k)**, Person 140k, Method 69k, Document 67k, Organization 44k, Software 40k, Event 34k, Location 31k, Artifact 27k, TimeReference 16k, Product 15k, Standard, Rule, Law, other. → diverse, NOT all concept.
- **Edges:** the Neo4j relationship *type* is the generic `RELATES_TO`, but the **GLiREL predicate is the `r.predicate` PROPERTY**: part_of 205k, uses 161k, **related_to only ~14% (134k)**, references, created_by, instance_of, located_in, implements, produces, depends_on, works_for, supports, causes, … Edges also carry `r.relation_family`.

So the semantic meaning the models produced is present and correct in the graph DB.

---

## 2. Architecture — the graph surfaces and which one is the default

The app mounts **only** `<GraphViewer mode="brain">` (`frontend/src/App.tsx:721-722`). `GraphViewer` has several internal data paths:

| Surface | Backend source | Renders GLiNER type? | Renders GLiREL predicate? | Status |
|---|---|---|---|---|
| **Brain View** (default tab) | `POST /api/graph/brain-view` → `services/graph/queries.py:get_brain_view` | ❌ satellites hardcoded "Concept" | ❌ edges are structural `contains`/`bridges_to` | **BROKEN (what the user sees)** |
| Book drill-down | drill path → `local_entities` | ✅ real `entity_type` | ✅ real `predicate` | OK |
| Graph Query tab | `POST /api/graph/query` → `graph_query.py:expand_subgraph` | ✅ `coalesce(primary_entity_type, entity_type,'other')` | ✅ `coalesce(r.predicate,'related_to')` + relation_family | OK |
| `/graph/full`, `/api/graph/cluster` | `neo4j_reader.py` | ✅ | ✅ | OK (raw dicts, no response_model) |
| Overview supernodes | `services/graph/overview.py` | n/a (aggregate `domain`/`concept_community` supernodes) | n/a (structural) | By design |
| Mission Control "context map" | `/api/graph/discover` → `orchestrator._context_graph_from_result` | ❌ hardcoded | ❌ hardcoded | Dead + flattened (see §6.2) |

**The user is on the default Brain View tab.** That is the surface to fix.

---

## 3. Root cause (verified) — Brain View, two contributing layers

### 3.1 Backend returns type-less entities
`backend/services/graph/queries.py` `get_brain_view`:
- Per-**book** dominant type IS computed (`:144 coalesce(d.dominant_entity_type, computed_type) AS dominant_entity_type`).
- But per-**entity** `top_entities` is **names only**: `:165 collect(coalesce(e_seed.display_name, e_seed.entity_id)) AS ranked_names` → `:169 ranked_names[..8] AS top_entities` → serialized `:337 "top_entities": list(record.get("top_entities") or [])` (a `list[str]`). No `primary_entity_type` is ever collected per entity.
- Confirmed in the API contract: `frontend/src/lib/api.ts` `BrainViewDocument.top_entities?: string[]` (~`:1857`).

### 3.2 Frontend hardcodes the type it never received
`frontend/src/components/graph/GraphViewer.tsx`:
- Satellite entity nodes (built from `d.top_entities`, ~`:798-823`): **`:812 entity_type: "Concept", :813 kind: "Concept"`** — every orbiting entity, regardless of real type.
- Cross-book bridge nodes (~`:650-655`): **`:653 entity_type: "Concept"`**.
- Brain-View edges are synthetic: `:827 predicate: "contains"`, `:837 predicate: "bridges_to"`, `:661/:683 predicate: "in_book"` — never a GLiREL `r.predicate`.

### 3.3 Smoking gun (proves it's the hardcode, not a missing field)
`frontend/src/lib/sigma-constants.ts` `inferNodeKind` (`:294-317`) reads `String(node?.entity_type || node?.primary_entity_type)`, switch-maps Person/Organization/Product/Software/Location/Method/Document/Standard/Rule/Law/Artifact/TimeReference/Concept, and **defaults to `"Other"` (`:317`)** — NOT "Concept". So if the type were merely *absent*, nodes would render "Other"/slate. The user sees "Concept" specifically ⇒ it is the explicit `entity_type:"Concept"` hardcode at `GraphViewer.tsx:812-813`/`:653`. The renderer + palette (`NODE_COLORS`, 17 kinds) are correct and would show diverse types the moment they receive real `entity_type`.

---

## 4. The fix (primary — Brain View)

### 4.A Backend — `backend/services/graph/queries.py` (`get_brain_view`)
Additively project per-entity type **without changing the existing `top_entities` shape** (changing it to objects would break the current frontend). Add a parallel records field:

In the entity-collection `WITH` (around `:164-169`), alongside `ranked_names`, also collect typed records, e.g.:
```cypher
collect({
  entity_id: e_seed.entity_id,
  name: coalesce(e_seed.display_name, e_seed.entity_id),
  entity_type: coalesce(e_seed.primary_entity_type, e_seed.entity_type, 'Other')
}) AS ranked_records
...
ranked_records[..8] AS top_entity_records,
ranked_names[..8]   AS top_entities,   -- keep for back-compat
```
Carry `top_entity_records` through the subsequent `WITH`/`RETURN` (mirror every place `top_entities` is threaded, e.g. `:215-217`, `:241`) and serialize it at `:337`:
```python
"top_entity_records": list(record.get("top_entity_records") or []),
```

### 4.B Frontend — `frontend/src/components/graph/GraphViewer.tsx` + types
- `api.ts` `BrainViewDocument`: add `top_entity_records?: { entity_id: string; name: string; entity_type: string }[]`.
- Satellite loop (~`:798-823`): if `d.top_entity_records` is present, build satellites from it using `entity_type: rec.entity_type` and **drop the hardcoded `kind:"Concept"`** (let the adapter/`inferNodeKind` derive the kind from `entity_type`). Fallback path (only `top_entities` names available): set `entity_type` to `""`/omit so it renders **"Other"**, never `"Concept"`.
- Bridge nodes (~`:650-655`): same — carry the real type when available, else omit (→ "Other").
- (Optional polish) Bridge edges already have `dominant_relation_family` from the backend (`api.ts BrainViewFlatBridge.dominant_relation_family`); you can color/label them by family via the existing edge-family styling instead of leaving them generic.

**Edges:** leave Brain View's `contains`/`bridges_to`/`in_book` as-is — those are *correct* for a book overview (a book *contains* an entity; books *bridge* via shared entities). True entity→entity GLiREL predicates (part_of/uses/works_for) belong to the **entity graph** (drill/query), which already shows them. If product wants GLiREL predicates on the default canvas, that's a design change (e.g. a "concept graph" toggle that loads `/api/graph/query` or `/graph/full`), not a bug fix.

### Do NOT touch (already correct — verify before trusting, then leave alone)
`services/graph/neo4j_reader.py` (projects `coalesce(primary_entity_type,entity_type) AS entity_type` + `r.predicate`/`relation_family`); `services/graph/graph_query.py:expand_subgraph` (`:343`, `:372-373`); the drill path `GraphViewer.tsx:638-671`; `inferNodeKind`/`NODE_COLORS`/`EDGE_STYLES` in `sigma-constants.ts`; `overview.py` supernodes (aggregate by design).

---

## 5. Validation Codex must do FIRST (re-derive these; don't trust this doc blindly)

> These line numbers were captured 2026-06-14; the repo may have shifted. Confirm each citation, then implement.

### 5.1 Confirm data is rich (Neo4j)
```bash
cd /Users/king/polymath_v3.3
PW="$(grep -E '^NEO4J_PASSWORD=' .env | cut -d= -f2)"
docker exec polymath_v33-neo4j-1 cypher-shell -u neo4j -p "$PW" \
  "MATCH (n:Entity) RETURN n.primary_entity_type AS t, count(*) AS c ORDER BY c DESC LIMIT 20"
docker exec polymath_v33-neo4j-1 cypher-shell -u neo4j -p "$PW" \
  "MATCH ()-[r:RELATES_TO]->() RETURN r.predicate AS p, count(*) AS c ORDER BY c DESC LIMIT 20"
```
Expect diverse entity types (Concept ~35%, Person, Organization, Product, …) and diverse predicates (part_of, uses, …).

### 5.2 Confirm the default surface + the hardcode + the type-less query
- `frontend/src/App.tsx` — only `<GraphViewer mode="brain">` is mounted.
- `frontend/src/components/graph/GraphViewer.tsx` — `entity_type:"Concept"`/`kind:"Concept"` at the satellite (~`:812-813`) and bridge (~`:653`) builders; synthetic edge predicates `:827`/`:837`.
- `backend/services/graph/queries.py` — `get_brain_view` returns `top_entities` as `ranked_names[..8]` (names only, ~`:165`/`:169`/`:337`).
- `frontend/src/lib/sigma-constants.ts` — `inferNodeKind` default is `"Other"` (~`:317`), proving the hardcode is the cause.

### 5.3 Confirm the typed paths already work (so the fix shape is right)
- `GraphViewer.tsx:638-671` drill maps `entity_type: e.entity_type` + `predicate: r.predicate`.
- Live raw-graph API returns diverse types/predicates (mint an owner token; corpus `authentic_library` Neo4j uuid = `f8a0aa85-6cb4-4f64-a973-f9183f1546bb`, owner Mongo `_id` = `6a132beafef900c17f87848e`):
```bash
TOKEN=$(docker exec polymath_v33-backend-1 python -c \
 "from services.auth import auth_service; print(auth_service.create_access_token('6a132beafef900c17f87848e','king'))")
docker exec polymath_v33-backend-1 sh -c \
 "curl -s -H 'Authorization: Bearer $TOKEN' 'http://localhost:8000/api/corpora/f8a0aa85-6cb4-4f64-a973-f9183f1546bb/graph/full?limit=300'" \
 | python -m json.tool | head -60
# Expect nodes with entity_type=Person/Organization/Location/... and edges with predicate=uses/works_for/... 
# Also hit POST /api/graph/brain-view and confirm top_entities is a list[str] (no type) — that is the gap.
```

---

## 6. Secondary findings (do these too for "the full thing", but lower priority)

### 6.1 `/api/graph/query` edge family — `models/_schemas_legacy.py`
`GraphQueryLink` (~`:1272`) and `RelationEdge` (~`:1210`) keep `predicate` but have **no `relation_family` field**, so family-based edge coloring is unavailable on the rendered Graph-Query path (adapter falls back to `WeakAssociation` gray). Add an optional `relation_family: str = "WeakAssociation"` to both. Pure additive, low risk.

### 6.2 Mission Control / context map — currently DEAD + flattened
- `POST /api/graph/discover` **500s**: `services.graph._orchestrator_legacy` is **missing** (`orchestrator.py:6321` raises). The context map serves nothing live. Restore that module first if Mission Control is wanted.
- Even when revived, `orchestrator._context_graph_from_result` hardcodes node `:662 "kind":"concept"` and edge `:754 "kind": raw.get("classification") or "context"` (the GLiREL predicate is buried in the free-text `evidence` field), and the schema `models/schemas.py` `ContextGraphNode` (`:569`, only `kind`, no `entity_type`) / `ContextGraphLink` (`:588`, only `kind`, no `predicate`/`relation_family`) **structurally strip** type/predicate.
- **IMPORTANT (audit finding):** the `context_graph` payload is **not rendered by any graph component** — it appears only in `frontend/src/types/discover.ts` and both callers use it for **synthesis prose** (`AtomicView.tsx` nucleus, `GraphViewer.tsx` `auto_synthesis.markdown`). So fixing §6.2 will NOT change the visible node/edge colors. Treat it as: restore `_orchestrator_legacy` (to un-break discover) + optionally enrich the schema — but do not expect it to affect the user's "all concept" symptom. **This is the trap the first analysis fell into; verify before investing.**

---

## 7. Findings directory (file → role)

| File | Lines | Role |
|---|---|---|
| `frontend/src/App.tsx` | ~721 | mounts ONLY Brain View → default surface |
| `frontend/src/components/graph/GraphViewer.tsx` | ~812-813, ~653 | **BUG**: hardcodes satellite/bridge `entity_type:"Concept"` |
| `frontend/src/components/graph/GraphViewer.tsx` | ~798-823, ~827/837/661 | satellite/edge construction (synthetic predicates) |
| `frontend/src/components/graph/GraphViewer.tsx` | ~638-671 | drill path — CORRECT (real type + predicate) |
| `backend/services/graph/queries.py` | ~144 | per-book dominant_entity_type (exists) |
| `backend/services/graph/queries.py` | ~165, 169, 337 | **BUG ROOT**: `top_entities` = names only, no per-entity type |
| `frontend/src/lib/api.ts` | ~1857 | `BrainViewDocument.top_entities: string[]` (contract gap) |
| `frontend/src/lib/sigma-constants.ts` | ~294-317 | `inferNodeKind` (default "Other") — CORRECT; smoking gun |
| `frontend/src/lib/sigma-constants.ts` | NODE_COLORS / EDGE_STYLES | type→color palette — CORRECT |
| `backend/services/graph/neo4j_reader.py` | ~153, 180-181 | raw reader — CORRECT (don't touch) |
| `backend/services/graph/graph_query.py` | ~343, 372-373 | query path — CORRECT (don't touch) |
| `backend/models/_schemas_legacy.py` | ~1210, 1272 | secondary: edge `relation_family` missing |
| `backend/services/graph/orchestrator.py` | ~662, 754, 6321 | context-map flatten + discover 500 (dead path) |
| `backend/models/schemas.py` | ~566-597 | ContextGraphNode/Link strip type/predicate (dead path) |
| `frontend/src/types/discover.ts` | — | context_graph type def; NOT rendered |

---

## 8. Acceptance criteria

1. On the **default graph view**, orbiting entities render with their real GLiNER types (Person/Organization/Product/Software/Location/…), each its palette color, with a type chip — not all "Concept".
2. Drill-down and Graph-Query tabs continue to render real types + GLiREL predicate labels (no regression).
3. No surface renders an entity as "Concept" unless its real `primary_entity_type`/`entity_type` actually is "Concept".
4. (If §6.1 done) Graph-Query edges color by `relation_family`.
5. (If §6.2 attempted) `/api/graph/discover` returns 200; note explicitly whether it affects the visible canvas (it should not).
