FROM pytorch/pytorch:2.11.0-cuda13.0-cudnn9-runtime

WORKDIR /app

# curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN set -eux; \
    grep -vE '^flash-attn([<>=!~].*)?$' requirements.txt > /tmp/requirements-core.txt; \
    pip install --break-system-packages --no-cache-dir --upgrade pip setuptools wheel packaging ninja; \
    pip install --break-system-packages --no-cache-dir -r /tmp/requirements-core.txt; \
    pip install --break-system-packages --no-cache-dir --no-build-isolation "flash-attn>=2.7.4.post1" || \
        echo "flash-attn install failed; embedder will fall back to standard attention at runtime"

COPY main.py .

EXPOSE 80

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80"]
