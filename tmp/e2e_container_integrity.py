"""Safe post-deploy pin and persistent-journal writability check."""

from __future__ import annotations

import importlib.metadata as metadata
import os
from pathlib import Path

import spacy


assert metadata.version("spacy") == "3.8.14"
assert metadata.version("en-core-web-sm") == "3.8.0"
nlp = spacy.load("en_core_web_sm")
assert str(nlp.meta.get("version") or "") == "3.8.0"
assert nlp.pipe_names
print(
    f"uid={os.getuid()} spacy={metadata.version('spacy')} "
    f"model={metadata.version('en-core-web-sm')} pipes={len(nlp.pipe_names)}"
)

journal_dir = os.environ.get("RUNPOD_JOB_JOURNAL_DIR")
if journal_dir:
    root = Path(journal_dir)
    assert root.is_absolute()
    root.mkdir(parents=True, exist_ok=True)
    probe = root / ".e2e-write-preflight"
    with probe.open("w", encoding="utf-8") as handle:
        handle.write("ok\n")
        handle.flush()
        os.fsync(handle.fileno())
    probe.unlink()
    print(f"journal_dir={root} fsync_preflight=ok")
else:
    print("journal_dir=unset")
