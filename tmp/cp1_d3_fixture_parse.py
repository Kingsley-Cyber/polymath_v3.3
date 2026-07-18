import asyncio
import json
from pathlib import Path

from services.ingestion.docling_adapter import finalize_source_meta, parse_document


EXPECTED = {
    "fixture_garden_stewardship_2019.pdf": ("Maria Okafor", "2019-03-01"),
    "fixture_lighthouse_logistics_2004.pdf": ("Edwin Halvorsen", "2004-01-01"),
}


async def main() -> None:
    receipts = []
    for filename, (author, document_date) in EXPECTED.items():
        path = Path("/ingest-source/hy3_smoke") / filename
        result = await parse_document(
            path.read_bytes(),
            filename,
            "application/pdf",
            do_ocr=False,
        )
        finalize_source_meta(result, filename)
        provenance = result.routing_trace["bibliographic"][
            "bibliographic_provenance"
        ]
        receipt = {
            "filename": filename,
            "source_format": result.source_format,
            "source_tier": result.source_tier.value,
            "parser_fallback_count": result.parser_fallback_count,
            "author": result.author,
            "document_date": result.document_date,
            "provenance_method": provenance.get("method"),
            "provenance_source": provenance.get("source"),
        }
        receipts.append(receipt)
        assert result.source_format == "pypdf_font_layout"
        assert result.author == author
        assert result.document_date == document_date
        assert provenance.get("method") == "text_head_published"
        assert provenance.get("source") == "text_head:published"
    print(json.dumps(receipts, indent=2, sort_keys=True))


asyncio.run(main())
