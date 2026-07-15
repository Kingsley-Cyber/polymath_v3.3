"""Pinned GLiNER + spaCy + Python LocalExtractionV1 runtime.

The module is imported both by the RunPod endpoint and by the pinned-local
reference harness. It owns no credentials and exposes no durable-write API.
"""

from __future__ import annotations

import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import random
import re
import sys
import time
from typing import Any

from models.extraction_registry import (
    extraction_registry_hashes,
    load_extraction_registries,
)
from models.local_extraction import LocalExtractionV1
from services.ingestion.gliner_mentions import select_gliner_mentions
from services.ingestion.semantic_observations import (
    build_spacy_observation_bundle,
    compile_local_extraction_v1,
)


CONTRACT_VERSION = "polymath.runpod_local_extraction.v1"
DETERMINISM_PROFILE = "polymath.torch_cuda_deterministic.v1"
PYTHON_VERSION = "3.11.15"
SPACY_VERSION = "3.8.14"
SPACY_MODEL = "en_core_web_sm"
SPACY_MODEL_VERSION = "3.8.0"
PARSER_VERSION = "spacy:3.8.14;model:3.8.0"
GLINER_VERSION = "0.2.26"
GLINER_MODEL_ID = "urchade/gliner_medium-v2.1"
GLINER_MODEL_REVISION = "40ec419335d09393f298636f471328b722c6da9e"
GLINER_CONFIG_SHA256 = (
    "a8f3c2ecc57deb70077be6940962aa60e82d861a153a5cd2839b91795968ae7d"
)
GLINER_WEIGHTS_SHA256 = (
    "922214c0c60f7835bb5c00f52ad1769d38518d5183f85de7bc03893a8403c023"
)
ENTITY_THRESHOLD = 0.4
MAX_WINDOW_WORDS = 260
MAX_TASKS = 64
MAX_TASK_CHARS = 200_000
TIME_EXPRESSIONS_MAX_PER_CHUNK = 64
TIME_CUE_WINDOW_CHARS = 40
EXTRACTION_VOCABULARY_SHA256 = (
    "47ea44fee2341c3cc65ef2bb4f99795947aa0c1cc9e1d55314efc7647af89612"
)
PREDICATE_NORMALIZATION_SHA256 = (
    "0ba7cdc3d8dd6f643e7ccce74b46f4711940947fa73020adaf130f5efd727ce8"
)

EXPECTED_DISTRIBUTIONS = {
    "gliner": GLINER_VERSION,
    "huggingface-hub": "0.36.2",
    "numpy": "2.2.6",
    "pydantic": "2.13.4",
    "safetensors": "0.7.0",
    "sentencepiece": "0.2.1",
    "spacy": SPACY_VERSION,
    "tokenizers": "0.22.2",
    "torch": "2.12.0",
    "transformers": "4.57.6",
    "en-core-web-sm": SPACY_MODEL_VERSION,
}
EXPECTED_ASSET_CONTRACT = {
    "extraction_vocabulary_sha256": EXTRACTION_VOCABULARY_SHA256,
    "predicate_normalization_sha256": PREDICATE_NORMALIZATION_SHA256,
    "gliner_config_sha256": GLINER_CONFIG_SHA256,
    "gliner_weights_sha256": GLINER_WEIGHTS_SHA256,
}
EXPECTED_DETERMINISM_ENV = {
    "CUBLAS_WORKSPACE_CONFIG": ":4096:8",
    "NVIDIA_TF32_OVERRIDE": "0",
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "PYTHONHASHSEED": "0",
}

_ROOT = Path(__file__).resolve().parent
_SOURCE_CLOSURE = (
    "app.py",
    "runtime.py",
    "models/__init__.py",
    "models/extraction_registry.py",
    "models/hash_taxonomy.py",
    "models/local_extraction.py",
    "models/semantic_artifacts.py",
    "services/__init__.py",
    "services/ingestion/__init__.py",
    "services/ingestion/gliner_mentions.py",
    "services/ingestion/semantic_observations.py",
    "registries/extraction_vocabularies.v1.json",
    "registries/predicate_normalization.v1.json",
)
_NLP: Any = None
_MODEL: Any = None
_MODEL_SNAPSHOT: Path | None = None
_DETERMINISM_IDENTITY: dict[str, Any] | None = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_closure_manifest() -> dict[str, Any]:
    files: dict[str, str] = {}
    for relative in _SOURCE_CLOSURE:
        path = _ROOT / relative
        if not path.is_file():
            raise RuntimeError(f"locked source closure file missing: {relative}")
        files[relative] = _sha256(path)
    encoded = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return {
        "files": files,
        "file_count": len(files),
        "closure_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _raw_registry_hashes() -> dict[str, str]:
    return {
        "extraction_vocabulary_sha256": _sha256(
            _ROOT / "registries/extraction_vocabularies.v1.json"
        ),
        "predicate_normalization_sha256": _sha256(
            _ROOT / "registries/predicate_normalization.v1.json"
        ),
    }


def _distribution_versions() -> dict[str, str]:
    observed: dict[str, str] = {}
    for name, expected in EXPECTED_DISTRIBUTIONS.items():
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError as exc:
            raise RuntimeError(f"locked dependency missing: {name}") from exc
        if actual != expected:
            raise RuntimeError(
                f"locked dependency drift: {name} expected {expected}, observed {actual}"
            )
        observed[name] = actual
    return observed


def _configure_determinism() -> dict[str, Any]:
    """Apply and attest the locked production inference determinism profile."""

    global _DETERMINISM_IDENTITY
    if _DETERMINISM_IDENTITY is not None:
        return dict(_DETERMINISM_IDENTITY)

    observed_env = {key: os.getenv(key) for key in EXPECTED_DETERMINISM_ENV}
    if observed_env != EXPECTED_DETERMINISM_ENV:
        raise RuntimeError(
            "determinism environment differs from locked profile "
            f"{DETERMINISM_PROFILE}"
        )

    import numpy as np
    import torch

    random.seed(0)
    np.random.seed(0)
    torch.manual_seed(0)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(0)

    torch.set_num_threads(1)
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        if torch.get_num_interop_threads() != 1:
            raise
    torch.set_float32_matmul_precision("highest")
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    torch.backends.cuda.matmul.allow_fp16_reduced_precision_reduction = False
    torch.backends.cuda.matmul.allow_bf16_reduced_precision_reduction = False
    torch.use_deterministic_algorithms(True, warn_only=False)

    identity = {
        "profile": DETERMINISM_PROFILE,
        "seed": 0,
        "environment": observed_env,
        "torch_deterministic_algorithms": (
            torch.are_deterministic_algorithms_enabled()
        ),
        "torch_deterministic_warn_only": (
            torch.is_deterministic_algorithms_warn_only_enabled()
        ),
        "torch_float32_matmul_precision": torch.get_float32_matmul_precision(),
        "cuda_matmul_allow_tf32": torch.backends.cuda.matmul.allow_tf32,
        "cudnn_allow_tf32": torch.backends.cudnn.allow_tf32,
        "cudnn_benchmark": torch.backends.cudnn.benchmark,
        "cudnn_deterministic": torch.backends.cudnn.deterministic,
        "cuda_matmul_allow_fp16_reduced_precision_reduction": (
            torch.backends.cuda.matmul.allow_fp16_reduced_precision_reduction
        ),
        "cuda_matmul_allow_bf16_reduced_precision_reduction": (
            torch.backends.cuda.matmul.allow_bf16_reduced_precision_reduction
        ),
        "torch_num_threads": torch.get_num_threads(),
        "torch_num_interop_threads": torch.get_num_interop_threads(),
        "cuda_available": torch.cuda.is_available(),
    }
    expected = {
        "torch_deterministic_algorithms": True,
        "torch_deterministic_warn_only": False,
        "torch_float32_matmul_precision": "highest",
        "cuda_matmul_allow_tf32": False,
        "cudnn_allow_tf32": False,
        "cudnn_benchmark": False,
        "cudnn_deterministic": True,
        "cuda_matmul_allow_fp16_reduced_precision_reduction": False,
        "cuda_matmul_allow_bf16_reduced_precision_reduction": False,
        "torch_num_threads": 1,
        "torch_num_interop_threads": 1,
    }
    if any(identity[key] != value for key, value in expected.items()):
        raise RuntimeError("PyTorch determinism settings failed closed")
    _DETERMINISM_IDENTITY = identity
    return dict(identity)


def runtime_identity(*, model_snapshot: Path | None = None) -> dict[str, Any]:
    if platform.python_version() != PYTHON_VERSION:
        raise RuntimeError(
            f"locked Python drift: expected {PYTHON_VERSION}, "
            f"observed {platform.python_version()}"
        )
    registry_raw = _raw_registry_hashes()
    for key, expected in (
        ("extraction_vocabulary_sha256", EXTRACTION_VOCABULARY_SHA256),
        ("predicate_normalization_sha256", PREDICATE_NORMALIZATION_SHA256),
    ):
        if registry_raw[key] != expected:
            raise RuntimeError(f"locked registry drift: {key}")
    distributions = _distribution_versions()
    identity: dict[str, Any] = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "distributions": distributions,
        "spacy_model": SPACY_MODEL,
        "spacy_model_version": SPACY_MODEL_VERSION,
        "parser_version": PARSER_VERSION,
        "gliner_model_id": GLINER_MODEL_ID,
        "gliner_model_revision": GLINER_MODEL_REVISION,
        "asset_contract": dict(EXPECTED_ASSET_CONTRACT),
        "registry_namespace_hashes": extraction_registry_hashes(),
        "source_closure": source_closure_manifest(),
        "determinism": _configure_determinism(),
    }
    if model_snapshot is not None:
        identity["model_snapshot"] = {
            "config_sha256": _sha256(model_snapshot / "gliner_config.json"),
            "weights_sha256": _sha256(model_snapshot / "model.safetensors"),
        }
    return identity


def _load_nlp() -> Any:
    global _NLP
    if _NLP is not None:
        return _NLP
    import spacy

    if str(spacy.__version__) != SPACY_VERSION:
        raise RuntimeError("spaCy package differs from locked contract")
    nlp = spacy.load(SPACY_MODEL)
    if str(nlp.meta.get("version") or "") != SPACY_MODEL_VERSION:
        raise RuntimeError("spaCy model differs from locked contract")
    required_pipes = {
        "tok2vec",
        "tagger",
        "parser",
        "attribute_ruler",
        "lemmatizer",
        "ner",
    }
    if not required_pipes <= set(nlp.pipe_names):
        raise RuntimeError("spaCy model pipeline is incomplete")
    _NLP = nlp
    return nlp


def _model_cache_root() -> Path:
    configured = os.getenv("POLYMATH_HF_CACHE_ROOT")
    if configured:
        return Path(configured).expanduser()
    runpod_root = Path("/runpod-volume")
    if runpod_root.is_dir():
        return runpod_root / "huggingface-cache" / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _load_model() -> tuple[Any, Path]:
    global _MODEL, _MODEL_SNAPSHOT
    if _MODEL is not None and _MODEL_SNAPSHOT is not None:
        return _MODEL, _MODEL_SNAPSHOT
    import torch

    _configure_determinism()
    from gliner import GLiNER
    from huggingface_hub import snapshot_download

    snapshot = Path(
        snapshot_download(
            repo_id=GLINER_MODEL_ID,
            revision=GLINER_MODEL_REVISION,
            cache_dir=_model_cache_root(),
            local_files_only=os.getenv("POLYMATH_LOCAL_FILES_ONLY", "0") == "1",
        )
    )
    observed = {
        "config": _sha256(snapshot / "gliner_config.json"),
        "weights": _sha256(snapshot / "model.safetensors"),
    }
    if observed != {
        "config": GLINER_CONFIG_SHA256,
        "weights": GLINER_WEIGHTS_SHA256,
    }:
        raise RuntimeError("GLiNER snapshot hash differs from locked contract")
    model = GLiNER.from_pretrained(str(snapshot), local_files_only=True)
    if torch.cuda.is_available():
        model = model.to("cuda")
    model.eval()
    _MODEL = model
    _MODEL_SNAPSHOT = snapshot
    return model, snapshot


def _sentence_spans(text: str, doc: Any) -> list[tuple[int, int]]:
    spans = [
        (int(sentence.start_char), int(sentence.end_char))
        for sentence in doc.sents
        if sentence.text.strip()
    ]
    return spans or [(0, len(text))]


def _windows(text: str, *, nlp: Any, doc: Any) -> list[tuple[str, int]]:
    if len(text.split()) <= MAX_WINDOW_WORDS:
        return [(text, 0)]
    windows: list[tuple[str, int]] = []
    current: list[tuple[int, int]] = []
    current_words = 0
    for start, end in _sentence_spans(text, doc):
        words = len(text[start:end].split())
        if words > MAX_WINDOW_WORDS:
            if current:
                left, right = current[0][0], current[-1][1]
                windows.append((text[left:right], left))
                current = []
                current_words = 0
            tokens = list(nlp.make_doc(text[start:end]))
            for offset in range(0, len(tokens), MAX_WINDOW_WORDS):
                block = tokens[offset : offset + MAX_WINDOW_WORDS]
                if block:
                    left = start + int(block[0].idx)
                    right = start + int(block[-1].idx) + len(str(block[-1]))
                    windows.append((text[left:right], left))
            continue
        if current and current_words + words > MAX_WINDOW_WORDS:
            left, right = current[0][0], current[-1][1]
            windows.append((text[left:right], left))
            current = current[-1:]
            current_words = len(text[current[0][0] : current[0][1]].split())
        current.append((start, end))
        current_words += words
    if current:
        left, right = current[0][0], current[-1][1]
        windows.append((text[left:right], left))
    return windows or [(text, 0)]


_MONTH_TOKEN = (
    "January|February|March|April|May|June|July|August|September|October|"
    "November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec"
)
_YEAR_TOKEN = r"(?:19|20)\d{2}"
_SEASON_TOKEN = r"(?:spring|summer|autumn|fall|winter)"
_PERIOD_TOKEN = rf"(?:{_SEASON_TOKEN}|seasons?|quarters?|periods?)"
_MODIFIER_TOKEN = r"(?:[A-Za-z][A-Za-z'’\-]{0,31})"
_TIME_REGEX_FAMILY: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("iso_date", re.compile(r"\b\d{4}-\d{2}-\d{2}\b")),
    (
        "year_range",
        re.compile(
            rf"\b{_YEAR_TOKEN}\s*(?:[-–—]|to|through|until)\s*{_YEAR_TOKEN}\b", re.I
        ),
    ),
    (
        "year_event_period",
        re.compile(
            rf"\b{_YEAR_TOKEN}(?:\s+{_MODIFIER_TOKEN}){{0,3}}\s+{_PERIOD_TOKEN}\b", re.I
        ),
    ),
    ("season_year", re.compile(rf"\b{_SEASON_TOKEN}\s+{_YEAR_TOKEN}\b", re.I)),
    (
        "qualified_year",
        re.compile(rf"\b(?:early|mid|late)(?:\s+|[-–—]){_YEAR_TOKEN}\b", re.I),
    ),
    ("quarter", re.compile(rf"\bQ[1-4]\s+{_YEAR_TOKEN}\b")),
    ("month_year", re.compile(rf"\b(?:{_MONTH_TOKEN})\.?\s+{_YEAR_TOKEN}\b")),
    ("version", re.compile(r"\bv?\d+\.\d+(?:\.\d+)*\b")),
    ("year", re.compile(rf"\b{_YEAR_TOKEN}\b")),
)
_VERSION_CUE_RE = re.compile(
    r"\b(?:release|releases|released|version|versions)\b", re.I
)
_TIME_ROLE_CUES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "publication",
        re.compile(
            r"\b(?:published|publishes|publish|publication|issued|released)\b", re.I
        ),
    ),
    (
        "revision",
        re.compile(r"\b(?:updated|revised|revision|amended|modified)\b", re.I),
    ),
    ("reference", re.compile(r"\b(?:as of|according to|data from)\b", re.I)),
    (
        "event",
        re.compile(
            r"\b(?:occurred|happened|took place|launched|founded|began)\b", re.I
        ),
    ),
    (
        "effective",
        re.compile(r"\b(?:effective|takes effect|in effect|comes into force)\b", re.I),
    ),
    (
        "forecast",
        re.compile(
            r"\b(?:will launch|will release|will ship|expected|forecasts?|projected|predicted|anticipated)\b",
            re.I,
        ),
    ),
    (
        "deadline",
        re.compile(r"\b(?:deadline|due by|due on|due date|no later than)\b", re.I),
    ),
)


def _time_context(text: str, start: int, end: int) -> str:
    return text[max(0, start - TIME_CUE_WINDOW_CHARS) : end + TIME_CUE_WINDOW_CHARS]


def capture_time_expressions(text: str, doc: Any) -> tuple[list[dict[str, Any]], bool]:
    captured: list[dict[str, Any]] = []
    taken: list[tuple[int, int]] = []

    def add(start: int, end: int, detector: str) -> None:
        if start >= end or any(left < end and start < right for left, right in taken):
            return
        taken.append((start, end))
        context = _time_context(text, start, end)
        captured.append(
            {
                "text": text[start:end],
                "char_start": start,
                "char_end": end,
                "detector": detector,
                "role_candidates": [
                    role for role, pattern in _TIME_ROLE_CUES if pattern.search(context)
                ],
            }
        )

    # Specific deterministic patterns own overlapping spans. The pinned full
    # spaCy pipeline can split "2018 drought summer" into separate DATE
    # entities; accepting those first would suppress the stronger exact phrase.
    for family, pattern in _TIME_REGEX_FAMILY:
        for match in pattern.finditer(text):
            if family == "version" and not _VERSION_CUE_RE.search(
                _time_context(text, match.start(), match.end())
            ):
                continue
            add(match.start(), match.end(), "regex")
    for entity in getattr(doc, "ents", ()):
        if str(getattr(entity, "label_", "")) in {"DATE", "TIME", "EVENT"}:
            add(int(entity.start_char), int(entity.end_char), "spacy")
    captured.sort(key=lambda item: (item["char_start"], item["char_end"]))
    truncated = len(captured) > TIME_EXPRESSIONS_MAX_PER_CHUNK
    return captured[:TIME_EXPRESSIONS_MAX_PER_CHUNK], truncated


def _validate_payload(payload: dict[str, Any]) -> list[dict[str, str]]:
    if set(payload) != {
        "contract_version",
        "batch_id",
        "model_id",
        "model_revision",
        "spacy_pipeline",
        "asset_contract",
        "determinism_profile",
        "tasks",
    }:
        raise ValueError("request fields do not match the locked wire contract")
    if payload["contract_version"] != CONTRACT_VERSION:
        raise ValueError("unsupported extraction contract")
    if payload["model_id"] != GLINER_MODEL_ID:
        raise ValueError("GLiNER model id differs from locked contract")
    if payload["model_revision"] != GLINER_MODEL_REVISION:
        raise ValueError("GLiNER model revision differs from locked contract")
    if payload["spacy_pipeline"] != SPACY_MODEL:
        raise ValueError("spaCy pipeline differs from locked contract")
    if payload["asset_contract"] != EXPECTED_ASSET_CONTRACT:
        raise ValueError("request asset contract differs from locked assets")
    if payload["determinism_profile"] != DETERMINISM_PROFILE:
        raise ValueError("request determinism profile differs from locked runtime")
    batch_id = payload["batch_id"]
    if not isinstance(batch_id, str) or not batch_id.strip() or len(batch_id) > 200:
        raise ValueError("batch_id must be a bounded nonempty string")
    tasks = payload["tasks"]
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_TASKS:
        raise ValueError("tasks must contain 1..64 rows")
    required = {"document_id", "child_id", "source_version_id", "text"}
    validated: list[dict[str, str]] = []
    seen_children: set[str] = set()
    for raw in tasks:
        if not isinstance(raw, dict) or set(raw) != required:
            raise ValueError("task fields do not match the locked contract")
        row = {key: str(raw[key]) for key in required}
        if any(not row[key].strip() for key in required):
            raise ValueError("task fields must be nonempty strings")
        if len(row["text"]) > MAX_TASK_CHARS:
            raise ValueError("task text exceeds the locked character bound")
        if row["child_id"] in seen_children:
            raise ValueError("child_id values must be unique within one batch")
        seen_children.add(row["child_id"])
        validated.append(row)
    return validated


def extract_local_batch(
    payload: dict[str, Any],
    *,
    nlp: Any = None,
    model: Any = None,
    enforce_runtime: bool = True,
) -> dict[str, Any]:
    """Run one strict bounded batch and return only staged extraction JSON."""

    started = time.perf_counter()
    tasks = _validate_payload(payload)
    registries = load_extraction_registries()
    entity_types = list(registries["vocab"]["entity_types"])

    snapshot: Path | None = None
    if enforce_runtime:
        _configure_determinism()
        nlp = nlp or _load_nlp()
        if model is None:
            model, snapshot = _load_model()
        else:
            snapshot = _MODEL_SNAPSHOT
        identity = runtime_identity(model_snapshot=snapshot)
    else:
        if nlp is None or model is None:
            raise ValueError(
                "injected nlp and model are required when runtime checks are off"
            )
        identity = {
            "test_injected": True,
            "asset_contract": dict(EXPECTED_ASSET_CONTRACT),
            "source_closure": source_closure_manifest(),
        }

    task_docs: list[Any] = []
    task_windows: list[list[tuple[str, int]]] = []
    flattened: list[str] = []
    for task in tasks:
        doc = nlp(task["text"])
        windows = _windows(task["text"], nlp=nlp, doc=doc)
        task_docs.append(doc)
        task_windows.append(windows)
        flattened.extend(window_text for window_text, _ in windows)

    raw_batches = model.batch_predict_entities(
        flattened,
        entity_types,
        threshold=ENTITY_THRESHOLD,
        batch_size=min(32, len(flattened)),
    )
    if len(raw_batches) != len(flattened):
        raise RuntimeError("GLiNER returned the wrong window cardinality")

    results: list[dict[str, Any]] = []
    cursor = 0
    for index, task in enumerate(tasks):
        window_rows: list[dict[str, Any]] = []
        for window_text, window_offset in task_windows[index]:
            rows = raw_batches[cursor]
            cursor += 1
            for raw in rows or []:
                converted = dict(raw)
                converted["start"] = window_offset + int(raw.get("start") or 0)
                converted["end"] = window_offset + int(raw.get("end") or 0)
                if window_text[
                    int(raw.get("start") or 0) : int(raw.get("end") or 0)
                ] != str(raw.get("text") or ""):
                    converted["text"] = ""
                window_rows.append(converted)

        mentions, mention_counts = select_gliner_mentions(
            document_id=task["document_id"],
            child_id=task["child_id"],
            text=task["text"],
            raw_entities=window_rows,
            controlled_types=entity_types,
        )
        bundle = build_spacy_observation_bundle(
            text=task["text"],
            nlp=lambda _text, doc=task_docs[index]: doc,
            source_version_id=task["source_version_id"],
            hierarchy_node_id=task["child_id"],
            parser_id=SPACY_MODEL,
            parser_version=PARSER_VERSION,
        )
        compiled = compile_local_extraction_v1(
            bundle,
            document_id=task["document_id"],
            child_id=task["child_id"],
        )
        extraction = LocalExtractionV1.model_validate(
            {
                **compiled.extraction.model_dump(mode="python"),
                "entities": [item.model_dump(mode="python") for item in mentions],
                "relations": [],
            }
        )
        for mention in extraction.entities:
            if task["text"][mention.start_char : mention.end_char] != mention.text:
                raise RuntimeError("entity mention failed exact source round trip")
        for predicate in extraction.predicates:
            if (
                task["text"][predicate.start_char : predicate.end_char]
                != predicate.surface_text
            ):
                raise RuntimeError("predicate mention failed exact source round trip")
        temporal, temporal_truncated = capture_time_expressions(
            task["text"], task_docs[index]
        )
        results.append(
            {
                "document_id": task["document_id"],
                "child_id": task["child_id"],
                "source_version_id": task["source_version_id"],
                "extraction": extraction.model_dump(mode="json"),
                "temporal_captures": temporal,
                "temporal_captures_truncated": temporal_truncated,
                "mention_selection_counts": dict(sorted(mention_counts.items())),
                "compilation_receipt": compiled.receipt(),
            }
        )

    duration = time.perf_counter() - started
    return {
        "contract_version": CONTRACT_VERSION,
        "batch_id": payload["batch_id"],
        "results": results,
        "runtime_identity": identity,
        "metrics": {
            "chunks": len(tasks),
            "windows": len(flattened),
            "entities": sum(len(row["extraction"]["entities"]) for row in results),
            "predicates": sum(len(row["extraction"]["predicates"]) for row in results),
            "relations": 0,
            "duration_seconds": round(duration, 6),
        },
    }


if sys.version_info[:2] != (3, 11):
    # Import remains possible for static tooling, but no other interpreter line
    # may execute a real batch under the locked contract.
    pass
