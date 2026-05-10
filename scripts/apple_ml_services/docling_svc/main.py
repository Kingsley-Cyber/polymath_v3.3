"""Apple Silicon docling sidecar — host-native, CPU.

Docling already runs CPU-only on macOS (no MPS path inside docling
itself). The host-native version exists so Apple users don't need to
ship the docling Docker container; it's lighter to run alongside the
MLX sidecars under the same LaunchAgent.

Wire spec — matches the docling sidecar shape backend already calls:
  GET  /health → {"status": "ok"}
  POST /parse  → multipart upload of (file, mime), returns docling JSON

NOTE — IMPLEMENTATION SCAFFOLD
This is a thin wrapper around docling.document_converter.DocumentConverter.
The verified production version on Mac Studio adds: 150 MB upload cap,
600s read timeout, OCR disabled, custom inject_synthetic_headers pre-pass
matching the in-cluster sidecar contract. Replace the body when porting.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile

logger = logging.getLogger("docling_svc")
logging.basicConfig(level=logging.INFO)

MAX_UPLOAD_MB = int(os.environ.get("DOCLING_MAX_UPLOAD_MB", "150"))

app = FastAPI(title="Polymath Apple Docling Sidecar", version="0.1.0")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/parse")
async def parse(file: UploadFile = File(...)) -> dict:
    """Parse an uploaded document via docling and return its JSON form.

    REPLACE with the verified Mac Studio implementation that adds:
      - upload-size guard (HTTP 413 above DOCLING_MAX_UPLOAD_MB)
      - inject_synthetic_headers pre-pass
      - corpus-aware mime hinting
    """
    body = await file.read()
    if len(body) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"upload exceeds {MAX_UPLOAD_MB} MB")

    try:
        from docling.document_converter import DocumentConverter
    except ImportError as exc:
        raise HTTPException(status_code=503, detail=f"docling not installed: {exc}")

    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(file.filename or "").suffix) as tmp:
        tmp.write(body)
        tmp_path = Path(tmp.name)

    try:
        converter = DocumentConverter()
        result = converter.convert(str(tmp_path))
        return {
            "filename": file.filename,
            "document": result.document.export_to_dict(),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"parse failed: {exc}")
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
