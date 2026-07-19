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

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health', timeout=5)" || exit 1

ENTRYPOINT ["./scripts/start.sh"]
