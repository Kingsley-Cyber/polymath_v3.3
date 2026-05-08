# Polymath reranker sidecar. Install torch from the cu128 wheel index so CUDA
# kernels include sm_120 support for RTX PRO 6000 Blackwell hosts. The older
# pytorch:2.5.1-cuda12.1 image loads but crashes during CrossEncoder.predict.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# curl for healthcheck; libgomp1 for torch CPU threading.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cu128 \
        "torch>=2.7,<3"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
