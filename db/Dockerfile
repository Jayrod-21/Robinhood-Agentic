# Migration runner image.
#
# The database is on an internal-only network with no host port (ADR-001), so migrations run from a
# container attached to that network rather than from the host. Two benefits beyond reachability:
# the runner's Python is pinned to 3.12 regardless of the host's 3.14, and its dependencies are
# baked in — the internal network has no egress, so a runtime `pip install` there is impossible by
# design.
#
# The repo is mounted read-only at run time rather than copied in: migrations are data, and the set
# that runs must be the set in the working tree, not a stale layer.

# Digest from `docker pull python:3.12-slim` on 2026-07-28. To bump: pull, then read
# `docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim`.
FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

# psycopg[binary] ships the libpq wheel, so no build toolchain and no system libpq is needed.
# Exact- and hash-pinned via requirements.txt (--require-hashes): an unpinned driver is one
# `docker build` away from changing behaviour under a migration that has already been applied
# elsewhere, and a version pin alone does not authenticate the artifact.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt && rm /tmp/requirements.txt

# Numeric non-root user, created before the entrypoint is set. Nothing here needs to write to the
# image; the only writable path is the database itself.
RUN useradd --uid 1001 --create-home --shell /usr/sbin/nologin runner
USER 1001

WORKDIR /repo

# Exec form so PID 1 receives SIGTERM and a long CREATE INDEX can be interrupted cleanly.
ENTRYPOINT ["python", "/repo/db/migrate.py"]
CMD ["status"]
