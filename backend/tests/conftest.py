"""Test path setup + a deterministic environment (issue: the suite depended on the CWD).

Path setup: make ``app`` (backend) and ``src`` (project root) importable.

Determinism: the settings classes read ``backend/.env`` through pydantic-settings' ``env_file``.
That file is a developer's live configuration — on a configured host it sets AUTH_DATABASE_URL,
which flips auth enforcement ON for every test that never asked for it (17 tests failed when
pytest ran from ``backend/`` and passed from the repo root, because the relative ``.env`` used to
resolve against the CWD). Worse, ``monkeypatch.delenv`` cannot suppress an ``env_file`` value, so
tests asserting the unconfigured posture were unpassable on exactly the hosts where the configured
posture mattered. A suite whose result depends on the working directory is not a gate, so:

* ``env_file`` is disabled for BOTH settings classes for the whole session — tests see only real
  environment variables, which monkeypatch can add and remove symmetrically;
* every test starts with the two DSNs stripped (the "no database" posture, a first-class state per
  ``app/db/config.py``) and opts in explicitly via ``monkeypatch.setenv`` when it wants one.
"""

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND.parent

for p in (str(BACKEND), str(PROJECT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Imports must follow the sys.path setup above — this is the module that creates that path.
from app.config import Settings  # noqa: E402
from app.db.config import DbSettings, reset_db_settings  # noqa: E402

# Session-wide and unconditional: no test may load a developer's backend/.env. pydantic-settings
# reads model_config["env_file"] at instantiation time, so mutating it here (before any test
# builds a Settings object) covers every construction for the rest of the process.
Settings.model_config["env_file"] = None
DbSettings.model_config["env_file"] = None
reset_db_settings()


@pytest.fixture(autouse=True)
def _no_inherited_dsns(monkeypatch):
    """Strip both DSNs so every test starts DB-less regardless of the invoking shell.

    Tests that want a database say so with ``monkeypatch.setenv`` (their fixtures run after this
    one, so the opt-in wins); tests that assert the pre-auth stand-down posture rely on this
    baseline and mark it explicitly at their fixture. The teardown reset drops any cached
    settings a test built, so its DSNs cannot leak into the next test.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AUTH_DATABASE_URL", raising=False)
    reset_db_settings()
    yield
    reset_db_settings()
