"""The Testing Lab: train, validate and compare models, in its own process.

WHY ITS OWN SERVICE
    Jared's call, and the right one. Measured on M, 2026-08-21: the backend image is 979 MB and
    this one is 1.53 GB, so folding the ML stack into the backend would push it past 1.5 GB — paid
    on every backend rebuild and restart, for a dependency set that trains models twice a week. And
    a training run is minutes of pinned CPU that would otherwise compete with the requests
    rendering the portfolio. This package is a separate image with a separate requirements.txt, so
    the backend never installs a line of it.

HOW IT IS REACHED
    Only through the backend. The Lab container sits on `rh-internal` with no host port and no
    Caddy route, so every request to it has already passed the app-wide session gate and the CSRF
    guard in backend/app/main.py. The Lab authenticates nobody, because nothing can reach it that
    has not already been authenticated — and that is stated here so it is never quietly untrue.

WHAT IT MUST NEVER DO
    Trade, or write production settings. The Lab measures; applying a tuned result to live weights
    is a separate confirmed, attributed write through PUT /api/settings/{key}, which is bounded and
    audited. Nothing here holds a broker credential.
"""
