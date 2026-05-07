# Polymath embedder sidecar — sentence-transformers behind a thin FastAPI
# /embeddings endpoint. CUDA-enabled torch installed from the cu128 wheel
# index so kernels include sm_120 (RTX PRO 6000 Blackwell). The previous
# pytorch:2.5.1-cuda12.1 base only built sm_50…sm_90 and crashed on .to('cuda')
# with "CUDA driver error: unknown error" on Blackwell. Mirrors the docling
# sidecar's installation pattern, which already runs on this host.

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# curl for the healthcheck; libgomp1 for torch threading.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Install torch first from the cu128 channel so transitive resolutions in
# sentence-transformers don't pull a non-CUDA wheel from PyPI.
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cu128 \
        "torch>=2.7,<3"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 80

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80"]
