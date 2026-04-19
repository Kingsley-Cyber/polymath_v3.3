# Polymath docling sidecar — wraps IBM Docling behind a thin FastAPI POST /parse.
# CUDA-enabled PyTorch (cu121). Backend talks to this service over HTTP via
# services/ingestion/docling_adapter.py. Pinned to GPU 0 (RTX 3090) via
# device_ids in docker-compose.yml — see GOTCHAS #65.

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/app/hf_cache \
    DOCLING_ARTIFACTS_PATH=/app/docling_models

# Docling needs libgl/glib for image ops; libgomp for torch threading.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# CUDA-enabled torch (cu121 — driver 591 supports CUDA 12.x). The wheel
# bundles the CUDA runtime libs so we don't need a full nvidia base image.
RUN pip install --upgrade pip && \
    pip install --extra-index-url https://download.pytorch.org/whl/cu121 \
        "torch>=2.2.2,<3" "torchvision>=0,<1"

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY main.py .

# Pre-fetch the docling layout/OCR model weights at build time so the first
# /parse call doesn't pay a ~1.5 GB download. Failure is tolerated — runtime
# fetch will retry. Comment this RUN out if your build network is too slow
# and you'd rather pay the cost on first use.
RUN python -c "from docling.utils.model_downloader import download_models; download_models()" || \
    echo "Model pre-fetch skipped/failed — will download on first /parse"

EXPOSE 8500

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8500/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8500", "--workers", "1"]
