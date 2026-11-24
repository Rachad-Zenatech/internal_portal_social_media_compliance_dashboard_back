FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    TESSERACT_CMD=/usr/bin/tesseract \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HF_HOME=/app/data/hf_cache \
    SENTIMENT_MODEL=vader \
    FINANCIAL_SENTIMENT_MODEL=vader \
    SENTIMENT_WARMUP_ENABLED=false \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=7006

WORKDIR /app

# System packages required by OCR, PDF extraction, PostgreSQL drivers,
# Playwright headless browser dependencies, and C extensions.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    build-essential \
    python3-dev \
    libpq-dev \
    postgresql-client \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-osd \
    poppler-utils \
    gcc \
    curl \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first for better Docker build caching.
COPY requirements.txt .
# torch from PyTorch's CPU-only index first (the container has no GPU): ~200 MB instead of the multi-GB CUDA build that
# a plain install pulls. The requirements then find it already installed.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium browser and its system runtime libraries.
# Store in /ms-playwright with universal permissions for appuser.
RUN python -m playwright install --with-deps chromium \
    && mkdir -p /ms-playwright \
    && chmod -R 777 /ms-playwright \
    && rm -rf /var/lib/apt/lists/*

# Copy application source code.
COPY . .

# Create the persistent-data directory and a non-root application user.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data /app/data/upload_files /app/data/uploads /app/.data/submission_files \
    && chown -R 10001:10001 /app/data \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8009 7006

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8009/health/live')" || exit 1

# Serve FastAPI and the mounted /mcp endpoint in one foreground uvicorn process so memory isn't doubled and crashes restart cleanly.
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8009", "--workers", "1"]
