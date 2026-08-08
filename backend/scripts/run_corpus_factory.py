"""Corpus factory gate — serial corpus digest == overlapped corpus digest.

Runs a list of source files as ONE corpus through the Graphify pipeline via
the CorpusCoordinator, once with max_active=1 and once with max_active=N,
each into a FRESH namespace (no resume contamination), and compares the
corpus digests plus wall-clock.

Usage:
    run_corpus_factory.py --inputs f1.md f2.md ... [--max-active 3] [--run-id X]
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402
from dotenv import dotenv_values  # noqa: E402

from services.extraction.corpus_coordinator import CorpusDocument, run_corpus_factory  # noqa: E402
from services.extraction.gliner2_cpu_provider import get_gliner2_cpu_provider  # noqa: E402


def _mongo_uri() -> str:
    env = dotenv_values(os.path.join(_BACKEND, "..", ".env"))
    return re.sub(r"@mongodb:", "@localhost:", env.get("MONGODB_URI") or env.get("MONGO_URI") or "")


def _documents(paths: list[str]) -> list[CorpusDocument]:
    documents = []
    for path in paths:
        text = Path(path).read_text(encoding="utf-8")
        doc_id = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        documents.append(CorpusDocument(
            doc_id=doc_id,
            text=text,
            children=[SimpleNamespace(chunk_id=f"{doc_id}:1", text=text)],
        ))
    return documents


async def _run(paths: list[str], max_active: int, run_id: str) -> int:
    client = AsyncIOMotorClient(_mongo_uri(), serverSelectionTimeoutMS=8000)
    provider = get_gliner2_cpu_provider()
    documents = _documents(paths)
    print(f"{len(documents)} documents, run id {run_id}")

    results = {}
    for label, budget in (("serial", 1), (f"factory({max_active})", max_active)):
        db = client[f"graphify_factory_{run_id}_{'serial' if budget == 1 else 'parallel'}"]
        started = time.perf_counter()
        result = await run_corpus_factory(
            db=db, corpus_id=f"factory-{run_id}", documents=documents,
            provider=provider, max_active=budget,
        )
        elapsed = time.perf_counter() - started
        results[label] = result
        print(f"{label}: wall {elapsed:.1f}s | overlap {result.report['overlap_factor']:.2f}x "
              f"| passed {result.report['passed']}/{result.report['documents']} "
              f"| corpus digest {result.corpus_digest[:24]}")
        for row in result.per_document:
            if row["status"] != "passed":
                print("   FAILED:", row["doc_id"], row.get("error_type"), row.get("error"))

    serial_key, factory_key = list(results)
    equal = results[serial_key].corpus_digest == results[factory_key].corpus_digest
    speedup = (
        results[serial_key].report["wall_seconds"]
        / results[factory_key].report["wall_seconds"]
    )
    print(f"speedup: {speedup:.2f}x")
    print("CORPUS EQUALITY GATE:", "PASS — identical semantic output" if equal else "FAIL")
    client.close()
    return 0 if equal else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", nargs="+", required=True)
    parser.add_argument("--max-active", type=int, default=3)
    parser.add_argument("--run-id", default=time.strftime("%Y%m%dT%H%M%S"))
    args = parser.parse_args()
    return asyncio.run(_run(args.inputs, args.max_active, args.run_id))


if __name__ == "__main__":
    raise SystemExit(main())
