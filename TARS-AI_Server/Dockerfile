# TARS-AI Companion Server — CPU-only container (host has no GPU)
FROM python:3.11-slim

# espeak-ng: audio synthesis fallback deps; libgomp1: OpenMP runtime for torch/whisper
RUN apt-get update && apt-get install -y --no-install-recommends \
        espeak-ng libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# CPU torch FIRST, then the remaining requirements with the CUDA index lines
# stripped. The CPU wheel (e.g. 2.6.0+cpu) satisfies "torch>=2.6.0", so the
# later `pip install -r` does NOT re-download a CUDA build from PyPI.
COPY requirements-server.txt .
RUN sed -i '/download.pytorch.org/d' requirements-server.txt \
    && pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements-server.txt

# Runtime code + dashboard assets (app-server.py reads www/ — see :1665-1671)
COPY app-server.py .
COPY www/ ./www/

# Models are downloaded to /app/models (MODELS_DIR, app-server.py:382); every
# loader passes an explicit cache_dir under it (whisper :532, vision :1302,
# embeddings :1496) — HF_HOME is never consulted. Persistence comes from the
# named volume mounted at /app/models in docker-compose.yml (S-M4-2).

EXPOSE 5678

CMD ["python", "app-server.py"]
