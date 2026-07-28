"""Shared fixtures for the migration-runner suite.

`db/migrate.py` runs as a top-level script (both in the runner container and under
`python db/migrate.py`), so tests import it the same way: `db/` goes on sys.path and the module is
`migrate`, not `db.migrate`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
