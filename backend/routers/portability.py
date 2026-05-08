"""Runtime portability API for Settings download/upload buttons."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from routers.auth import get_current_user
from services.portability import (
    export_portability_archive,
    import_portability_archive,
)

router = APIRouter(prefix="/api/portability", tags=["portability"])


def _cleanup(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


@router.get("/export")
async def export_runtime_archive(
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
):
    """Build and download a logical Polymath runtime archive."""
    try:
        archive_path, _stats = await export_portability_archive(current_user["user_id"])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Export failed: {exc}") from exc

    background_tasks.add_task(_cleanup, str(archive_path))
    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename="polymath-runtime-export.zip",
        background=background_tasks,
    )


@router.post("/import")
async def import_runtime_archive(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    """Upload and merge a logical Polymath runtime archive."""
    filename = file.filename or ""
    if not filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Upload a .zip Polymath archive.")

    tmp = tempfile.NamedTemporaryFile(prefix="polymath-portability-upload-", suffix=".zip", delete=False)
    tmp_path = Path(tmp.name)
    try:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            tmp.write(chunk)
        tmp.close()
        stats = await import_portability_archive(tmp_path, current_user["user_id"])
        return {"status": "ok", "stats": stats}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Import failed: {exc}") from exc
    finally:
        await file.close()
        _cleanup(str(tmp_path))
