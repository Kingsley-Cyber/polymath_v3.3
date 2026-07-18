"""No-write parse/chunk census for the frozen E2E selection."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import re
from pathlib import Path

from models.schemas import IngestionConfig
from services.ingestion import docling_adapter, tier_chunker
from services.ingestion.section_classifier import PARENT_SUMMARY_KINDS


ROOT = Path("/ingest-source/runpod_e2e_15doc_20260715")
MANIFEST = Path("/tmp/runpod_e2e_15doc_selection_v1.json")


async def main() -> None:
    selection = json.loads(MANIFEST.read_text(encoding="utf-8"))
    config = IngestionConfig(
        preset="deep",
        extraction_engine="runpod_flash",
        runpod_wire_contract="local_extraction_v1",
        runpod_endpoint_id_override="hk81nfl5cnwufx",
        runpod_account_name_override="primary",
        chunk_summarization=True,
        use_neo4j=True,
    )
    rows = []
    total_parents = 0
    total_children = 0
    total_summary_required = 0
    for selected in selection["selected"]:
        path = ROOT / selected["filename"]
        data = path.read_bytes()
        mime, _ = mimetypes.guess_type(path.name)
        parsed = await docling_adapter.parse_document(
            data,
            filename=path.name,
            mime=mime or "application/octet-stream",
            do_ocr=False,
        )
        docling_adapter.finalize_source_meta(parsed, path.name)
        normalized = re.sub(
            r"\s+", " ", (parsed.markdown or parsed.text or "").strip()
        )
        doc_id = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        parents, children, _ = tier_chunker.chunk(
            parse_result=parsed,
            doc_id=doc_id,
            corpus_id="runpod-e2e-preflight-no-write",
            config=config,
        )
        summary_required = sum(
            1
            for parent in parents
            if not parent.chunk_kind or parent.chunk_kind in PARENT_SUMMARY_KINDS
        )
        rows.append(
            {
                "children": len(children),
                "filename": path.name,
                "parents": len(parents),
                "source_tier": parsed.source_tier.value,
                "summary_required": summary_required,
            }
        )
        total_parents += len(parents)
        total_children += len(children)
        total_summary_required += summary_required
        print(json.dumps(rows[-1], sort_keys=True), flush=True)
    print(
        json.dumps(
            {
                "document_count": len(rows),
                "rows": rows,
                "total_children": total_children,
                "total_parents": total_parents,
                "total_summary_required": total_summary_required,
            },
            indent=2,
            sort_keys=True,
        )
    )


asyncio.run(main())
