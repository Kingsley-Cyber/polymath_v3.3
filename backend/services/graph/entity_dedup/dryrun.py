"""Entity dedup/resolution — DRY RUN (preview only, ZERO graph mutation).

Reads one corpus's entities from Neo4j (read-only MATCH...RETURN — there is no
MERGE/CREATE/SET/DELETE anywhere in this module), proposes merges, and writes a
preview/audit doc to Mongo (`graph_entity_dedup_preview`) plus a human-readable
report. Nothing in Neo4j is touched.

Two proposal lanes:
  Tier 1 — squash-key exact match. squash(nn) = normalized_name with spaces,
           underscores and hyphens removed. Deterministic, no embedding. Catches
           the dominant real fragmentation (flame_audio≡flameaudio,
           flame engine≡flameengine, abstract base class vs abstractbaseclass).
  Tier 2 — embedding cosine within a squash-prefix bucket, reported with a
           shared-neighbor Jaccard so a human can calibrate a threshold. Catches
           morphological variants (plurals) and near-synonyms the squash key
           misses. Bounded by MAX_EMBED; coverage is reported, never silently
           truncated.

Grounding (measured 2026-06-13 on the live 796k-entity graph):
  - canonical_family is a COARSE 19-value topic label (programming_languages has
    83k members); only 24% of entities have one. Used as a WEAK prior only,
    never a precision gate — this corrects the original plan.
  - normalize_entity_name keeps underscores and strips hyphens, so genuine dup
    fragments differ in normalized_name but collapse under the squash key.
  - 4,615 numeric/short-name entities exist; some ('c','r','ai','un') are real
    languages/concepts, so junk is only FLAGGED for review, never auto-merged.

Survivor selection (deterministic): max by (mentions, degree, -len(canonical),
reversed entity_id) — most-evidenced node wins; the base lemma tends to survive;
ties broken lexicographically so re-runs are identical.

Type policy: same primary_entity_type → proposed. Cross-type only when the pair
is in TYPE_ALLOWLIST, and then flagged require_review=true (the flame engine /
flameengine Software↔Organization case is the motivating example).
"""
from __future__ import annotations

import argparse
import asyncio
import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from config import get_settings

# ── Tunables (surfaced in the preview so they can be calibrated) ────────────
SIM_THRESHOLD = 0.90          # Tier-2 cosine floor to PROPOSE (report-only cut)
SIM_STRONG = 0.93             # Tier-2 cosine at/above which we'd auto-apply
JACCARD_MIN = 0.10            # neighbor overlap shown for calibration (soft)
MAX_EMBED = 8000              # Tier-2 embedding budget for the dry run
EMBED_DIM = 1024              # Qwen3-Embedding-0.6B (matches _DEFAULT_DIM)
PREFIX_LEN = 4                # Tier-2 bucket key = squash-key char prefix
BUCKET_MAX = 400              # cap members scored per Tier-2 bucket
JUNK_MENTION_MAX = 2          # junk candidate must be this sparse...
JUNK_DEGREE_MAX = 2           # ...on both mentions and degree to be flagged

# Symmetric cross-type transitions allowed (entity-identity-compatible).
_ALLOW = {
    ("Organization", "Software"),
    ("Concept", "Software"),
    ("Product", "Software"),
    ("Concept", "Method"),
    ("Product", "Organization"),
}
TYPE_ALLOWLIST = _ALLOW | {(b, a) for (a, b) in _ALLOW}

_SQUASH_RE = re.compile(r"[\s_\-]+")
_LETTER_RE = re.compile(r"[a-z]")


def squash_key(normalized_name: str) -> str:
    """Collapse whitespace/underscore/hyphen so punctuation/spacing variants of
    the same surface form share a key. 'flame engine'->'flameengine'."""
    return _SQUASH_RE.sub("", (normalized_name or "").lower())


def _settings_attr(s: Any, *names: str, default: Any = None) -> Any:
    for n in names:
        v = getattr(s, n, None)
        if v:
            return v
    return default


def _is_junk_name(cn: str) -> bool:
    """True only for names with no alphabetic content (pure numbers/symbols) or
    <=2 chars. The caller additionally requires low mentions+degree, and we
    never merge these — only flag them."""
    cn = (cn or "").strip()
    if len(cn) <= 2:
        return True
    if not _LETTER_RE.search(cn.lower()):
        return True
    return False


def _survivor_sort_key(e: dict) -> tuple:
    # Higher is better → sort desc on this tuple, pick first.
    return (e["mentions"], e["degree"], -len(e["cn"] or ""), _rev(e["id"]))


def _rev(s: str) -> str:
    # smaller entity_id wins on final tie → invert ordering by mapping to a
    # value that sorts the way we want when taken as "max".
    return "".join(chr(255 - min(ord(c), 255)) for c in (s or ""))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    u = len(a | b)
    return (len(a & b) / u) if u else 0.0


def _type_ok(pt_a: str, pt_b: str) -> tuple[bool, bool]:
    """(allowed, is_cross_type). Same type always allowed; cross-type only if in
    the allow-list."""
    pt_a = pt_a or ""
    pt_b = pt_b or ""
    if pt_a == pt_b:
        return True, False
    return (pt_a, pt_b) in TYPE_ALLOWLIST, True


# ── Neo4j (read-only) ───────────────────────────────────────────────────────
async def _load_corpus_entities(sess, corpus_id: str) -> list[dict]:
    """All non-tombstoned entities mentioned by this corpus, with mention count
    and degree. Read-only."""
    q = """
    MATCH (c:Chunk {corpus_id: $cid})-[:MENTIONS]->(e:Entity)
    WHERE e.merged_into IS NULL AND coalesce(e.tombstone, false) = false
      AND coalesce(e.quarantined, false) = false
      AND e.normalized_name IS NOT NULL AND e.normalized_name <> ''
    WITH DISTINCT e
    RETURN e.entity_id AS id, e.canonical_name AS cn, e.normalized_name AS nn,
           e.canonical_family AS fam, e.primary_entity_type AS pt,
           e.definitional_phrase AS defp,
           size([(e)<-[:MENTIONS]-() | 1]) AS mentions,
           size([(e)-[:RELATES_TO]-() | 1]) AS degree
    """
    out: list[dict] = []
    res = await sess.run(q, cid=corpus_id)
    async for r in res:
        out.append({
            "id": r["id"], "cn": r["cn"] or "", "nn": r["nn"] or "",
            "fam": r["fam"], "pt": r["pt"] or "", "defp": r["defp"] or "",
            "mentions": int(r["mentions"] or 0), "degree": int(r["degree"] or 0),
        })
    return out


async def _load_neighbor_sets(sess, ids: list[str]) -> dict[str, set]:
    """Top RELATES_TO neighbor ids (undirected) for the given entities, for
    Jaccard. Read-only, capped per node."""
    if not ids:
        return {}
    q = """
    MATCH (e:Entity)-[:RELATES_TO]-(o:Entity)
    WHERE e.entity_id IN $ids
    WITH e.entity_id AS id, collect(DISTINCT o.entity_id)[..60] AS nbrs
    RETURN id, nbrs
    """
    out: dict[str, set] = {}
    res = await sess.run(q, ids=ids)
    async for r in res:
        out[r["id"]] = set(r["nbrs"] or [])
    return out


# ── Embedding (Tier 2) ───────────────────────────────────────────────────────
async def _embed(texts: list[str]) -> list[list[float]] | None:
    """Embed entity strings via the corpus-frozen local embedder (MLX Qwen3).
    Returns None on any failure so Tier 2 degrades gracefully and the dry run
    still emits Tier-1 results."""
    try:
        from services.embedder import embed_batch
        return await embed_batch(texts, mode="local", expected_dim=EMBED_DIM)
    except Exception as exc:  # noqa: BLE001
        print(f"[tier2] embedding unavailable ({exc!r}); skipping Tier 2")
        return None


# ── Core dry run ─────────────────────────────────────────────────────────────
async def run_dryrun(corpus_id: str, *, do_tier2: bool = True) -> dict:
    s = get_settings()
    from neo4j import AsyncGraphDatabase

    drv = AsyncGraphDatabase.driver(
        _settings_attr(s, "NEO4J_URI", "NEO4J_URL"),
        auth=(
            _settings_attr(s, "NEO4J_USER", "NEO4J_USERNAME", default="neo4j"),
            _settings_attr(s, "NEO4J_PASSWORD", "NEO4J_PASS"),
        ),
    )
    try:
        async with drv.session() as sess:
            ents = await _load_corpus_entities(sess, corpus_id)
            by_id = {e["id"]: e for e in ents}

            # ── junk flagging (no merge) ──────────────────────────────────
            junk = [
                {"id": e["id"], "cn": e["cn"], "pt": e["pt"],
                 "mentions": e["mentions"], "degree": e["degree"]}
                for e in ents
                if _is_junk_name(e["cn"])
                and e["mentions"] <= JUNK_MENTION_MAX
                and e["degree"] <= JUNK_DEGREE_MAX
            ]
            junk_ids = {j["id"] for j in junk}

            # ── Tier 1: squash-key exact buckets ──────────────────────────
            sq_buckets: dict[str, list[dict]] = defaultdict(list)
            for e in ents:
                if e["id"] in junk_ids:
                    continue
                k = squash_key(e["nn"])
                if len(k) >= 3:  # skip ultra-short keys (noise)
                    sq_buckets[k].append(e)

            tier1_groups = [grp for grp in sq_buckets.values() if len(grp) > 1]

            # collect ids that need neighbor sets (Tier-1 group members)
            t1_ids = [e["id"] for grp in tier1_groups for e in grp]
            nbrs = await _load_neighbor_sets(sess, t1_ids)

            proposals: list[dict] = []
            for grp in tier1_groups:
                survivor = max(grp, key=_survivor_sort_key)
                for dup in grp:
                    if dup["id"] == survivor["id"]:
                        continue
                    allowed, cross = _type_ok(survivor["pt"], dup["pt"])
                    j = _jaccard(nbrs.get(survivor["id"], set()),
                                 nbrs.get(dup["id"], set()))
                    proposals.append({
                        "lane": "tier1_squash",
                        "survivor_id": survivor["id"], "survivor_cn": survivor["cn"],
                        "survivor_pt": survivor["pt"], "survivor_mentions": survivor["mentions"],
                        "dup_id": dup["id"], "dup_cn": dup["cn"], "dup_pt": dup["pt"],
                        "dup_mentions": dup["mentions"],
                        "squash_key": squash_key(dup["nn"]),
                        "neighbor_jaccard": round(j, 3),
                        "same_family": bool(survivor["fam"] and survivor["fam"] == dup["fam"]),
                        "cross_type": cross,
                        "type_allowed": allowed,
                        "require_review": cross or (not allowed),
                        "decision": "auto" if (allowed and not cross) else "review",
                    })

            already = {p["dup_id"] for p in proposals} | {p["survivor_id"] for p in proposals}

            # ── Tier 2: embedding within squash-prefix buckets ────────────
            tier2_stats = {"attempted": 0, "embedded": 0, "buckets": 0,
                           "candidates": 0, "skipped_budget": 0, "enabled": do_tier2}
            if do_tier2:
                pre_buckets: dict[str, list[dict]] = defaultdict(list)
                for e in ents:
                    if e["id"] in junk_ids or e["id"] in already:
                        continue
                    k = squash_key(e["nn"])
                    if len(k) >= PREFIX_LEN:
                        pre_buckets[k[:PREFIX_LEN]].append(e)
                cand_buckets = [
                    grp[:BUCKET_MAX] for grp in pre_buckets.values() if len(grp) > 1
                ]
                tier2_stats["buckets"] = len(cand_buckets)

                # budget embeddings across buckets
                to_embed: list[dict] = []
                for grp in cand_buckets:
                    if len(to_embed) + len(grp) > MAX_EMBED:
                        tier2_stats["skipped_budget"] += len(grp)
                        continue
                    to_embed.extend(grp)
                tier2_stats["attempted"] = len(to_embed)

                if to_embed:
                    texts = [
                        (e["cn"] + (". " + e["defp"] if e["defp"] else "")).strip()
                        for e in to_embed
                    ]
                    vecs = await _embed(texts)
                    if vecs and len(vecs) == len(to_embed):
                        tier2_stats["embedded"] = len(vecs)
                        vec_by_id = {to_embed[i]["id"]: vecs[i] for i in range(len(to_embed))}
                        # re-bucket the embedded set by prefix and score pairs
                        emb_buckets: dict[str, list[dict]] = defaultdict(list)
                        for e in to_embed:
                            emb_buckets[squash_key(e["nn"])[:PREFIX_LEN]].append(e)
                        pair_ids: set[str] = set()
                        raw_pairs: list[tuple[dict, dict, float]] = []
                        for grp in emb_buckets.values():
                            for i in range(len(grp)):
                                for k2 in range(i + 1, len(grp)):
                                    a, b = grp[i], grp[k2]
                                    sim = _cosine(vec_by_id[a["id"]], vec_by_id[b["id"]])
                                    if sim >= SIM_THRESHOLD:
                                        raw_pairs.append((a, b, sim))
                                        pair_ids.add(a["id"]); pair_ids.add(b["id"])
                        nbrs2 = await _load_neighbor_sets(sess, list(pair_ids))
                        for a, b, sim in raw_pairs:
                            survivor, dup = (a, b) if _survivor_sort_key(a) >= _survivor_sort_key(b) else (b, a)
                            allowed, cross = _type_ok(survivor["pt"], dup["pt"])
                            j = _jaccard(nbrs2.get(a["id"], set()), nbrs2.get(b["id"], set()))
                            proposals.append({
                                "lane": "tier2_embed",
                                "survivor_id": survivor["id"], "survivor_cn": survivor["cn"],
                                "survivor_pt": survivor["pt"], "survivor_mentions": survivor["mentions"],
                                "dup_id": dup["id"], "dup_cn": dup["cn"], "dup_pt": dup["pt"],
                                "dup_mentions": dup["mentions"],
                                "cosine": round(sim, 4),
                                "neighbor_jaccard": round(j, 3),
                                "same_family": bool(survivor["fam"] and survivor["fam"] == dup["fam"]),
                                "cross_type": cross, "type_allowed": allowed,
                                "require_review": (not allowed) or cross or sim < SIM_STRONG or j < JACCARD_MIN,
                                "decision": "auto" if (allowed and not cross and sim >= SIM_STRONG and j >= JACCARD_MIN) else "review",
                            })
                        tier2_stats["candidates"] = len(raw_pairs)

            # ── stats ─────────────────────────────────────────────────────
            auto = [p for p in proposals if p["decision"] == "auto"]
            review = [p for p in proposals if p["decision"] == "review"]
            survivors = {p["survivor_id"] for p in proposals}
            dups = {p["dup_id"] for p in proposals}
            stats = {
                "corpus_id": corpus_id,
                "corpus_entities": len(ents),
                "junk_flagged": len(junk),
                "tier1_groups": len(tier1_groups),
                "proposals_total": len(proposals),
                "proposals_auto": len(auto),
                "proposals_review": len(review),
                "distinct_survivors": len(survivors),
                "entities_eliminated_if_applied": len(dups),
                "tier2": tier2_stats,
            }

            preview_doc = {
                "kind": "entity_dedup_preview",
                "corpus_id": corpus_id,
                "created_at": datetime.now(timezone.utc),
                "mutated_graph": False,
                "tunables": {
                    "SIM_THRESHOLD": SIM_THRESHOLD, "SIM_STRONG": SIM_STRONG,
                    "JACCARD_MIN": JACCARD_MIN, "MAX_EMBED": MAX_EMBED,
                    "PREFIX_LEN": PREFIX_LEN, "type_allowlist": sorted(["|".join(t) for t in _ALLOW]),
                },
                "stats": stats,
                "proposals": proposals,
                "junk": junk,
            }

        # ── persist preview to Mongo (NOT Neo4j) ─────────────────────────
        try:
            from motor.motor_asyncio import AsyncIOMotorClient
            mc = AsyncIOMotorClient(_settings_attr(s, "MONGODB_URI", "MONGODB_URL"))
            db = mc[_settings_attr(s, "MONGODB_DB", default="polymath")]
            ins = await db["graph_entity_dedup_preview"].insert_one(preview_doc)
            stats["preview_doc_id"] = str(ins.inserted_id)
            mc.close()
        except Exception as exc:  # noqa: BLE001
            print(f"[preview] Mongo write skipped ({exc!r})")

        return {"stats": stats, "proposals": preview_doc["proposals"], "junk": junk}
    finally:
        await drv.close()


def _print_report(result: dict) -> None:
    st = result["stats"]
    print("\n" + "=" * 72)
    print(f"ENTITY DEDUP — DRY RUN PREVIEW  (corpus {st['corpus_id']})")
    print("=" * 72)
    print(f"corpus entities scanned         : {st['corpus_entities']:,}")
    print(f"junk-name candidates (flagged)  : {st['junk_flagged']:,}  (NOT merged)")
    print(f"Tier-1 squash-key dup groups    : {st['tier1_groups']:,}")
    t2 = st["tier2"]
    print(f"Tier-2 embed: buckets={t2['buckets']:,} embedded={t2['embedded']:,} "
          f"candidates={t2['candidates']:,} budget_skipped={t2['skipped_budget']:,}")
    print("-" * 72)
    print(f"proposed merges TOTAL           : {st['proposals_total']:,}")
    print(f"  auto (same-type, high conf)   : {st['proposals_auto']:,}")
    print(f"  review (cross-type / soft)    : {st['proposals_review']:,}")
    print(f"entities eliminated if applied  : {st['entities_eliminated_if_applied']:,} "
          f"({100*st['entities_eliminated_if_applied']/max(1,st['corpus_entities']):.1f}% of corpus)")
    print(f"preview doc                     : {st.get('preview_doc_id','(mongo skipped)')}")
    print("-" * 72)
    props = result["proposals"]
    def _show(title, rows):
        print(f"\n{title} ({len(rows)} shown up to 18):")
        for p in rows[:18]:
            extra = (f"cos={p['cosine']}" if "cosine" in p else "squash") + \
                    f" jac={p['neighbor_jaccard']}"
            flag = " [CROSS-TYPE]" if p.get("cross_type") else ""
            print(f"  {p['dup_cn']!r}({p['dup_pt']},{p['dup_mentions']}m) -> "
                  f"{p['survivor_cn']!r}({p['survivor_pt']},{p['survivor_mentions']}m)"
                  f"  [{p['lane']} {extra}]{flag}")
    _show("AUTO merges", [p for p in props if p["decision"] == "auto"])
    _show("REVIEW merges", [p for p in props if p["decision"] == "review"])
    if result["junk"]:
        print(f"\nJUNK flagged ({len(result['junk'])} shown up to 20):")
        print("  " + ", ".join(repr(j["cn"]) for j in result["junk"][:20]))
    print("\n(NO graph mutation occurred — preview only.)\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Entity dedup dry run (preview only)")
    ap.add_argument("--corpus", required=True, help="corpus_id")
    ap.add_argument("--no-tier2", action="store_true", help="skip embedding lane")
    args = ap.parse_args()
    result = asyncio.run(run_dryrun(args.corpus, do_tier2=not args.no_tier2))
    _print_report(result)


if __name__ == "__main__":
    main()
