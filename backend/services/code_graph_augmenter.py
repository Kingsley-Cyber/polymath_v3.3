"""Phase 4.5 — graphify augmenter for the code lane.

Runs the graphify CLI (https://github.com/safishamsi/graphify, MIT) on a
batch of CODE-kind chunks to extract cross-symbol relationships and
community labels, then translates the output into a payload ready for
Neo4j MERGE writes.

Phase 4 already promotes every code chunk's `symbols_defined` and
`imports` into `:Entity` nodes via `_synthesize_code_extraction_results`.
This module adds two pieces on top:

1. `:Entity-[:CALLS]->:Entity` cross-file edges (graphify resolves call
   targets across files using tree-sitter alone — no LLM).
2. `graphify_community` integer property on `:Entity` nodes (Leiden
   clustering output) so retrieval can boost or filter by cluster.

The augmenter is **opt-in** via `Settings.GRAPHIFY_AUGMENT_CODE_LANE`
(per-corpus, default False). It is a SOFT dependency — when graphify
isn't installed or the subprocess fails for any reason, the augmenter
returns an empty enrichment dict and the rest of the ingestion pipeline
proceeds unchanged. Phase 4's deterministic entity write is the floor;
graphify enrichment is pure upside.

Design choices:
- Uses the `python -m graphify update` CLI rather than the lower-level
  Python API. The CLI handles cache setup, ROOT detection, manifest
  generation, etc. — fighting those internals from Python was producing
  zero-node graphs. The CLI is the supported surface.
- Each code chunk is written to a per-chunk temp file named by its
  `metadata.file_path` (when present) or a generated identifier with
  the correct language extension. Multi-file repos get cross-file edges
  naturally; single-document multi-fence .md ingests get cross-fence
  edges within the temp dir.
- Privacy boundary: graphify's code-extraction path (Pass 1) is
  deterministic tree-sitter, no LLM. The augmenter calls `update`
  specifically because that subcommand skips the LLM passes that would
  otherwise run on docs/PDFs. So even when `GRAPHIFY_AUGMENT_CODE_LANE`
  is on, no chunk content leaves the box.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Map Polymath language tags → file extensions for the temp-file write.
# graphify dispatches its tree-sitter parser by extension, so picking the
# right one matters. Keep in sync with docling_adapter._CODE_EXT_TO_LANGUAGE
# (inverted). When a language isn't in the map, fall back to ".txt" which
# graphify treats as non-code and ignores — quiet failure mode.
_LANG_TO_EXT: dict[str, str] = {
    "python": ".py",
    "javascript": ".js", "tsx": ".tsx", "typescript": ".ts",
    "go": ".go", "rust": ".rs",
    "lua": ".lua", "luau": ".luau",
    "cpp": ".cpp", "c": ".c",
    "cuda": ".cu",
    "java": ".java", "kotlin": ".kt",
    "ruby": ".rb", "php": ".php",
    "swift": ".swift", "dart": ".dart",
    "scala": ".scala", "csharp": ".cs",
    "bash": ".sh", "sql": ".sql",
    "objc": ".m",
    "html": ".html", "css": ".css",
    "vue": ".vue", "svelte": ".svelte",
    "json": ".json", "yaml": ".yaml",
    "toml": ".toml",
    "elixir": ".ex", "haskell": ".hs",
    "r": ".r", "nix": ".nix",
    "glsl": ".glsl", "hlsl": ".hlsl",
}


@dataclass(frozen=True)
class GraphifyEnrichment:
    """Per-document enrichment ready for Neo4j MERGE. All keys default
    to empty so partial failures don't crash downstream consumers."""

    # canonical-name → leiden community id
    entity_communities: dict[str, int]
    # list of (src_canonical, dst_canonical, source_file, source_location)
    call_edges: list[tuple[str, str, str, str]]
    # raw graphify community labels for diagnostics (community_id → label, if any)
    community_labels: dict[int, str]
    # chunk_id → list of symbol names this chunk's code calls. Derived from
    # call_edges by parsing source_file (written as `<chunk_id>.<ext>` by
    # `_write_temp_inputs`) back to the originating chunk. Used by the
    # worker to backfill `metadata.symbols_called` before sparse encoding
    # so BM25 indexes graphify-derived call sites.
    chunk_calls: dict[str, list[str]]
    # node counts for ops visibility
    node_count: int
    edge_count: int

    @classmethod
    def empty(cls) -> "GraphifyEnrichment":
        return cls(
            entity_communities={},
            call_edges=[],
            community_labels={},
            chunk_calls={},
            node_count=0,
            edge_count=0,
        )

    @property
    def is_empty(self) -> bool:
        return self.node_count == 0 and self.edge_count == 0


def _strip_label(label: str) -> str:
    """Normalize a graphify node label to match Phase 4's canonical_name.

    graphify emits labels like `"Combat.PunchAttack()"`, `".__init__()"`,
    `"VectorStore"`. Phase 4's `symbols_defined` carries `"Combat.PunchAttack"`,
    `"__init__"`, `"VectorStore"`. We strip trailing `()` and leading `.`
    so the two sets line up for the MERGE side-by-comparison.
    """
    s = label.strip()
    if s.endswith("()"):
        s = s[:-2]
    if s.startswith("."):
        s = s.lstrip(".")
    return s


def _translate(graph_json: dict[str, Any]) -> GraphifyEnrichment:
    """Translate graphify's NetworkX node-link JSON into a GraphifyEnrichment.
    Pure function — no I/O, fully testable with mocked graphify output."""
    nodes = graph_json.get("nodes", []) or []
    links = graph_json.get("links", []) or []

    id_to_clean: dict[str, str] = {}
    communities: dict[str, int] = {}
    community_labels: dict[int, str] = {}

    for n in nodes:
        nid = n.get("id")
        label = n.get("label")
        if not nid or not label:
            continue
        clean = _strip_label(str(label))
        if not clean:
            continue
        id_to_clean[str(nid)] = clean
        community = n.get("community")
        if isinstance(community, int):
            communities[clean] = community
        # graphify also emits cluster-level label nodes — capture them when
        # they have a `community_label` attr or look like one (file_type==summary)
        if n.get("file_type") == "summary":
            comm_id = n.get("community")
            if isinstance(comm_id, int):
                community_labels[comm_id] = clean

    call_edges: list[tuple[str, str, str, str]] = []
    # chunk_id → list of called symbol names. graphify writes `source_file`
    # as the BASENAME of the temp file we created. `_write_temp_inputs`
    # uses `<chunk_id>.<ext>` so Path(source_file).stem reverses cleanly
    # back to the originating chunk_id.
    chunk_calls: dict[str, list[str]] = {}
    for link in links:
        rel = (link.get("relation") or "").lower()
        if rel != "calls":
            continue
        src_id = str(link.get("source", ""))
        dst_id = str(link.get("target", ""))
        src = id_to_clean.get(src_id)
        dst = id_to_clean.get(dst_id)
        if not src or not dst or src == dst:
            continue
        source_file = str(link.get("source_file") or "")
        source_location = str(link.get("source_location") or "")
        call_edges.append((src, dst, source_file, source_location))
        # Bucket per-chunk for the worker's symbols_called backfill.
        # An empty / malformed source_file is silently skipped — defensive
        # against future graphify schema changes.
        if source_file:
            try:
                chunk_id = Path(source_file).stem
            except Exception:
                chunk_id = ""
            if chunk_id:
                bucket = chunk_calls.setdefault(chunk_id, [])
                # Dedupe within a chunk (one function may call dst many times)
                if dst not in bucket:
                    bucket.append(dst)

    return GraphifyEnrichment(
        entity_communities=communities,
        call_edges=call_edges,
        community_labels=community_labels,
        chunk_calls=chunk_calls,
        node_count=len(nodes),
        edge_count=len(links),
    )


def _safe_chunk_id_filename(chunk_id: str) -> str:
    """Sanitize a chunk_id for filesystem use. chunk_id is constructed in
    tier_chunker as `f"{doc_id}_{idx:04d}"` where doc_id is sha256-hex —
    already filesystem-safe across Win/macOS/Linux. This pass is defense
    in depth against future ID shape changes."""
    return "".join(c if c.isalnum() or c in "_-" else "_" for c in chunk_id)


def _write_temp_inputs(
    code_chunks: list[Any],
    tmpdir: Path,
) -> int:
    """Write each code chunk to a file named `<chunk_id>.<ext>` so graphify's
    `source_file` output reverses cleanly back to the originating chunk_id
    via `Path(source_file).stem`. Chunks without a chunk_id, language, or
    non-empty text are silently skipped. Returns the count actually written.

    Using chunk_id (not file_path) as the filename trades the file_path
    information loss (graphify's path-tokenization in `analyze` is mostly
    cosmetic — it doesn't affect call-graph resolution) for a reversible
    mapping that the worker's `symbols_called` backfill depends on.
    """
    written = 0
    for chunk in code_chunks:
        text = getattr(chunk, "text", "") or ""
        lang = (getattr(chunk, "language", None) or "").lower()
        chunk_id = getattr(chunk, "chunk_id", None) or ""
        if not text.strip() or not lang or not chunk_id:
            continue
        ext = _LANG_TO_EXT.get(lang, ".txt")
        safe_id = _safe_chunk_id_filename(chunk_id)
        if not safe_id:
            continue
        target = tmpdir / f"{safe_id}{ext}"
        target.write_text(text, encoding="utf-8")
        written += 1
    return written


def augment_code_chunks(
    code_chunks: list[Any],
    *,
    timeout_seconds: int = 120,
    python_executable: str | None = None,
) -> GraphifyEnrichment:
    """Run graphify on a list of CODE-kind chunks (ChildChunk or duck-typed
    equivalents — anything with `.text`, `.language`, `.metadata` attrs).

    Returns a GraphifyEnrichment. Always returns — never raises. Errors
    are logged and converted to `GraphifyEnrichment.empty()`. The empty
    result is the safe fallback for the caller; downstream just skips
    the augmenter write.
    """
    if not code_chunks:
        return GraphifyEnrichment.empty()

    executable = python_executable or sys.executable
    if shutil.which(executable) is None and executable != sys.executable:
        logger.warning(
            "code_graph_augmenter: python_executable=%r not found on PATH; "
            "falling back to sys.executable",
            executable,
        )
        executable = sys.executable

    try:
        with tempfile.TemporaryDirectory(prefix="polymath_graphify_") as tmp:
            tmpdir = Path(tmp)
            written = _write_temp_inputs(code_chunks, tmpdir)
            if written == 0:
                logger.info("code_graph_augmenter: no code chunks to send to graphify")
                return GraphifyEnrichment.empty()

            try:
                proc = subprocess.run(
                    [executable, "-m", "graphify", "update", str(tmpdir)],
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                logger.warning(
                    "code_graph_augmenter: graphify timed out after %ds on %d files",
                    timeout_seconds, written,
                )
                return GraphifyEnrichment.empty()
            except FileNotFoundError:
                logger.warning(
                    "code_graph_augmenter: python -m graphify failed to launch — "
                    "is graphifyy installed in this env?"
                )
                return GraphifyEnrichment.empty()

            if proc.returncode != 0:
                logger.warning(
                    "code_graph_augmenter: graphify rc=%d stderr=%s",
                    proc.returncode, (proc.stderr or "")[:500],
                )
                return GraphifyEnrichment.empty()

            graph_path = tmpdir / "graphify-out" / "graph.json"
            if not graph_path.exists():
                logger.warning(
                    "code_graph_augmenter: graphify ran but produced no graph.json at %s",
                    graph_path,
                )
                return GraphifyEnrichment.empty()

            try:
                payload = json.loads(graph_path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("code_graph_augmenter: graph.json parse failed: %s", exc)
                return GraphifyEnrichment.empty()

            enrichment = _translate(payload)
            logger.info(
                "code_graph_augmenter: ingested %d nodes / %d edges → "
                "%d call edges, %d entities with community labels",
                enrichment.node_count, enrichment.edge_count,
                len(enrichment.call_edges), len(enrichment.entity_communities),
            )
            return enrichment

    except Exception as exc:
        logger.warning("code_graph_augmenter: unexpected failure: %s", exc, exc_info=True)
        return GraphifyEnrichment.empty()
