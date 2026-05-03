"""Upload filename/MIME normalization before parser admission."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


SUPPORTED_EXTENSIONS = {
    ".csv",
    ".doc",
    ".docx",
    ".eml",
    ".epub",
    ".htm",
    ".html",
    ".log",
    ".markdown",
    ".md",
    ".mdown",
    ".mkd",
    ".msg",
    ".odp",
    ".ods",
    ".odt",
    ".pdf",
    ".ppt",
    ".pptx",
    ".rtf",
    ".text",
    ".txt",
    ".xls",
    ".xlsx",
    ".xhtml",
}

MIME_EXTENSION_MAP = {
    "application/csv": ".csv",
    "application/epub+zip": ".epub",
    "application/msword": ".doc",
    "application/pdf": ".pdf",
    "application/rtf": ".rtf",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.oasis.opendocument.presentation": ".odp",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/xhtml+xml": ".xhtml",
    "text/csv": ".csv",
    "text/html": ".html",
    "text/markdown": ".md",
    "text/plain": ".txt",
    "text/x-markdown": ".md",
}


@dataclass(frozen=True)
class IntakeResult:
    filename: str
    mime: str
    normalized: bool = False
    warning: str | None = None


class IntakeValidationError(ValueError):
    pass


def _clean_mime(mime: str | None) -> str:
    return str(mime or "").split(";", 1)[0].strip().lower()


def normalize_upload_filename(filename: str | None, mime: str | None) -> IntakeResult:
    """Return a parser-safe filename or raise before work is queued.

    Docling relies on filename format hints for several inputs. Browser uploads
    can carry useful MIME information while losing the extension, so we repair
    that specific case here instead of letting parse fail later.
    """

    original = Path(filename or "upload").name.strip() or "upload"
    mime_l = _clean_mime(mime)
    ext = Path(original).suffix.lower()
    if ext in SUPPORTED_EXTENSIONS:
        return IntakeResult(filename=original, mime=mime_l)

    inferred_ext = MIME_EXTENSION_MAP.get(mime_l)
    if not ext and inferred_ext:
        return IntakeResult(
            filename=f"{original}{inferred_ext}",
            mime=mime_l,
            normalized=True,
            warning=(
                f"Normalized upload filename from {original!r} to "
                f"{original + inferred_ext!r} using MIME {mime_l!r}."
            ),
        )

    if ext:
        raise IntakeValidationError(
            f"Unsupported file extension {ext!r} for {original!r}."
        )
    raise IntakeValidationError(
        f"File {original!r} has no extension and MIME {mime_l or '<missing>'!r} "
        "does not identify a supported parser format."
    )
