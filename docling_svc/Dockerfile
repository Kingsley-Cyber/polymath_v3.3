# Polymath docling sidecar — wraps IBM Docling behind a thin FastAPI POST /parse.
# GPU-enabled layout parser. OCR is disabled in the service; compose pins CUDA
# visibility so this uses the intended desktop GPU without scanning images.

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

# CUDA-enabled torch (cu121). This lets Docling use the assigned GPU for layout
# parsing while OCR remains disabled at runtime.
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cu121 \
        "torch>=2.2.2,<3" "torchvision>=0,<1"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Do not pre-fetch Docling/HF model weights into the image layer. Compose
# bind-mounts DOCLING_ARTIFACTS_PATH and HF_HOME to host storage under
# C:/PolymathRuntime by default, so runtime downloads survive rebuilds without
# bloating image layers or BuildKit cache.

EXPOSE 8500

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8500/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8500", "--workers", "1"]
