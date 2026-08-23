# Memory Vault — Docker image
# sentence-transformers pulls PyTorch, so we use CPU-only to keep image smaller

# ---------------------------------------------------------------------------
# Stage 1 — build the React dashboard
# ---------------------------------------------------------------------------
FROM node:26-slim AS web-builder

WORKDIR /web

COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2 — Python runtime
# ---------------------------------------------------------------------------
FROM python:3.13-slim

WORKDIR /app

# Install CPU-only PyTorch first (avoids pulling the 2GB CUDA version)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# Copy project files (migrations travel inside src/memory_vault/)
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY scripts/start.sh ./scripts/start.sh

# Copy the built dashboard into the package's static dir so it's bundled by the pip install
COPY --from=web-builder /web/dist/ ./src/memory_vault/api/static/

RUN pip install --no-cache-dir . \
    && sed -i 's/\r$//' ./scripts/start.sh \
    && chmod +x ./scripts/start.sh \
    && mkdir -p /var/log/memory-vault

RUN python -m spacy download en_core_web_sm

# The container runs as a non-root user, so put the model caches somewhere
# that user owns. HF_HOME defaults to /root/.cache, which is unreadable to
# anyone else and unwritable under a read-only root filesystem.
ENV HF_HOME=/opt/model-cache/huggingface \
    SENTENCE_TRANSFORMERS_HOME=/opt/model-cache/sentence-transformers

# Non-root. Everything the process writes at runtime is either a mounted
# volume (/var/log/memory-vault), a tmpfs (/tmp, for streamed uploads), or
# read-only (the model cache below), so the image itself never needs to be
# writable — see docker-compose.yml for the read_only + tmpfs setup.
#
# The switch happens BEFORE the model download on purpose: a `chown -R` after
# it rewrites every model file into a new layer, storing the whole ~92MB cache
# twice. Downloading as the owning user avoids that.
RUN useradd --system --uid 10001 --create-home --home-dir /home/memoryvault memoryvault \
    && mkdir -p /opt/model-cache \
    && chown -R memoryvault:memoryvault /app /var/log/memory-vault /opt/model-cache

USER memoryvault

# Download the embedding model at BUILD time. Without this the first request
# after every container start reaches out to huggingface.co and writes into
# the cache — which fails outright when the root filesystem is read-only, and
# makes a cold start depend on the network. ~92MB on a ~2.3GB image.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health', timeout=5)" || exit 1

ENTRYPOINT ["./scripts/start.sh"]
