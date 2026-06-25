"""Batch jobs run on a schedule (cron / systemd timer), not via HTTP.

These run inside the backend container so they reuse its code, env, API key, and mounted volumes:
    docker compose exec backend python -m app.jobs.cycle open|close
"""
