# Memory Vault — Docker image
# sentence-transformers pulls PyTorch, so we use CPU-only to keep image smaller

FROM python:3.11-slim

WORKDIR /app

# Install CPU-only PyTorch first (avoids pulling the 2GB CUDA version)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# Copy project files and install
COPY pyproject.toml ./
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY scripts/start.sh ./scripts/start.sh

ENV PYTHONPATH=/app

RUN pip install --no-cache-dir . \
    && sed -i 's/\r$//' ./scripts/start.sh \
    && chmod +x ./scripts/start.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health', timeout=5)" || exit 1

ENTRYPOINT ["./scripts/start.sh"]
