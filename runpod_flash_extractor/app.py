"""Blue-green RunPod endpoint for the certified LocalExtractionV1 spine."""

from __future__ import annotations

import os
from typing import Any

from runpod_flash import Endpoint, GpuType, ServerlessScalerType

from runtime import extract_local_batch


LOCKED_DEPENDENCIES = [
    "torch==2.12.0",
    "transformers==4.57.6",
    "tokenizers==0.22.2",
    "numpy==2.2.6",
    "safetensors==0.7.0",
    "sentencepiece==0.2.1",
    "huggingface_hub==0.36.2",
    "pydantic==2.13.4",
    "gliner==0.2.26",
    "spacy==3.8.14",
    (
        "en_core_web_sm @ "
        "https://github.com/explosion/spacy-models/releases/download/"
        "en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"
        "#sha256=1932429db727d4bff3deed6b34cfc05df17794f4a52eeb26cf8928f7c1a0fb85"
    ),
]


@Endpoint(
    name=os.getenv(
        "RUNPOD_FLASH_ENDPOINT_NAME",
        "polymath-local-extraction-green-20260715",
    ),
    gpu=[
        GpuType.NVIDIA_L4,
        GpuType.NVIDIA_RTX_A5000,
        GpuType.NVIDIA_GEFORCE_RTX_4090,
    ],
    workers=(
        int(os.getenv("RUNPOD_FLASH_MIN_WORKERS", "0")),
        int(os.getenv("RUNPOD_FLASH_MAX_WORKERS", "8")),
    ),
    max_concurrency=int(os.getenv("RUNPOD_FLASH_WORKER_CONCURRENCY", "1")),
    idle_timeout=int(os.getenv("RUNPOD_FLASH_IDLE_TIMEOUT", "60")),
    scaler_type=ServerlessScalerType.REQUEST_COUNT,
    scaler_value=int(os.getenv("RUNPOD_FLASH_SCALER_VALUE", "1")),
    execution_timeout_ms=int(os.getenv("RUNPOD_FLASH_EXECUTION_TIMEOUT_MS", "1800000")),
    flashboot=True,
    accelerate_downloads=True,
    dependencies=LOCKED_DEPENDENCIES,
)
def extract_batch(payload: dict[str, Any]) -> dict[str, Any]:
    """Execute the strict credential-free extraction contract."""

    return extract_local_batch(payload)
