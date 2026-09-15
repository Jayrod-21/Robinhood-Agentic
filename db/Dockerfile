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

# Digest from `docker pull python:3.12-slim` on 2026-09-14. To bump: pull, then read
# `docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim`.
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

# Debian security updates on top of the pinned base. The digest above fixes *what* is installed so
# a rebuild is reproducible; it cannot fix *when* upstream rebuilds, and python:3.12-slim lags the
# Debian security archive by weeks. Without this the image ships known-fixed CVEs in perl-base,
# gzip, libsqlite3-0 and libpcre2-8-0 — all flagged by the Trivy gate, none of them ours.
#
# Deliberately not version-pinned, unlike requirements.txt: a pinned -security version is removed
# from the archive as soon as it is superseded, so pinning here converts a silent vulnerability
# into a broken build on someone else's schedule. Authenticity still holds — apt verifies the
# signed Release file and each package checksum against it.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

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
