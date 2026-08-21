# Testing Lab image — the ML service, separate from the backend on purpose.
#
# Build context is the PROJECT ROOT (see deploy/docker-compose.prod.yml): the image bundles the lab
# package and the src/ml library it drives, and nothing from backend/.
#
# MEASURED on M, 2026-08-21: this image is 1.53 GB; the backend image is 979 MB. Adding this
# dependency set to the backend would push it past 1.5 GB and slow every backend rebuild and
# restart — which happen often — to pay for a stack that trains models twice a week. That is the
# whole argument for the split, and the numbers above are measurements, not estimates.
FROM python:3.12-slim

WORKDIR /app

# curl for the container healthcheck. No build-essential: every pin in lab/requirements.txt ships a
# manylinux wheel, and the compiler toolchain added ~180 MB for nothing. libgomp1 is xgboost's OpenMP
# runtime, which its wheel links against and slim does not ship — it fails at IMPORT time, not at
# install time, so a build that omits it looks clean and then dies on the first experiment.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY lab/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY lab/ ./lab/
COPY src/ ./src/

RUN useradd -m -u 1001 labuser && chown -R labuser:labuser /app
USER labuser

EXPOSE 8100

# Hits the Lab's own health route, which reports database reachability — so an unhealthy container
# means something a restart might fix, not just "the process is alive".
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8100/api/testing-lab/health || exit 1

# One worker. Training is CPU-bound and already runs in a thread pool; a second worker would double
# the memory for the same throughput and let two experiments contend for the same cores.
CMD ["uvicorn", "lab.app:app", "--host", "0.0.0.0", "--port", "8100", "--workers", "1"]
