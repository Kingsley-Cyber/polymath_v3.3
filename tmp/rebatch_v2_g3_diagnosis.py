import asyncio
import json
from io import BytesIO
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorClient
from pypdf import PdfReader

from config import get_settings


CORPUS_ID = "62193743-4175-40da-b861-ba1e1e567b9a"
FILES = {
    "fixture_garden_stewardship_2019.pdf",
    "fixture_lighthouse_logistics_2004.pdf",
}


async def main() -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    db = client[settings.MONGODB_DATABASE]
    documents = await db.documents.find(
        {"corpus_id": CORPUS_ID, "filename": {"$regex": "fixture.*pdf$"}},
        {
            "_id": 0,
            "doc_id": 1,
            "filename": 1,
            "author": 1,
            "document_date": 1,
            "source_published_at": 1,
            "bibliographic_provenance": 1,
            "routing_trace": 1,
            "source_format": 1,
        },
    ).sort("filename", 1).to_list(length=10)
    source = []
    for filename in sorted(FILES):
        path = Path("/ingest-source/hy3_smoke") / filename
        reader = PdfReader(BytesIO(path.read_bytes()))
        metadata = reader.metadata or {}
        lines = [
            line.strip()
            for line in (reader.pages[0].extract_text() or "").splitlines()
            if line.strip()
        ]
        source.append(
            {
                "filename": filename,
                "embedded_metadata": {
                    "title": str(metadata.get("/Title") or ""),
                    "author": str(metadata.get("/Author") or ""),
                    "creation_date": str(metadata.get("/CreationDate") or ""),
                    "mod_date": str(metadata.get("/ModDate") or ""),
                },
                "first_page_lines": lines[:12],
            }
        )
    print(
        json.dumps(
            {"stored_documents": documents, "source_pdf_evidence": source},
            default=str,
            indent=2,
            sort_keys=True,
        )
    )
    client.close()


asyncio.run(main())
