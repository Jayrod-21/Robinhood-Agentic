"""Test path setup: make ``app`` (backend) and ``src`` (project root) importable."""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND.parent

for p in (str(BACKEND), str(PROJECT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)
