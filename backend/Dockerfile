# Backend image — FastAPI dashboard service.
#
# Build context is the PROJECT ROOT (see docker-compose.yml) because the image bundles both the
# backend app and the reused screening package at src/. Paths below are relative to that root.
FROM python:3.12-slim

WORKDIR /app

# curl is used by the container healthcheck; build-essential covers any wheels without manylinux.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code + the reused, already-tested screening package from 3b's src/.
COPY backend/app/ ./app/
COPY src/ ./src/

# Run as non-root.
RUN useradd -m -u 1001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
