# Gatekeeper backend: runs with no network access at all.
#
# Build from the repository root:
#   docker build -f deploy/docker/backend.Dockerfile -t gatekeeper-backend:local .
#
# Everything the app needs at runtime is inside the image: Python packages, the
# SAML (xmlsec) libraries, and the embedding model. Nothing is downloaded on start.

# ---------- build stage: compile/install dependencies and fetch the model ----------
FROM python:3.14-slim-bookworm AS build

# Build-only system packages. xmlsec (for python3-saml) may compile against libxmlsec1.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential pkg-config libxml2-dev libxmlsec1-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY backend/requirements.txt .
# CPU-only PyTorch: the default CUDA build adds ~2 GB, and there are no GPUs to use.
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.txt

# Bake the embedding model into the image (the only time it touches the internet).
RUN python -c "from sentence_transformers import SentenceTransformer; \
SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2').save('/models/all-MiniLM-L6-v2')"
# save() writes the weights as mode 600 (root-only); the app runs as non-root, so open them up.
RUN chmod -R a+rX /models

# ---------- runtime stage: just what's needed to run ----------
FROM python:3.14-slim-bookworm

# Runtime libraries for SAML signature checks (no compilers in this stage).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libxmlsec1 libxmlsec1-openssl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /usr/local /usr/local
COPY --from=build /models /models

WORKDIR /app
COPY backend/ /app

# Unprivileged user. The app writes only to /app/logs (per-request trace) and /tmp.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin gatekeeper \
 && mkdir -p /app/logs && chown 10001:10001 /app/logs

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HOME=/tmp/hf \
    EMBEDDING_MODEL_PATH=/models/all-MiniLM-L6-v2

USER 10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
