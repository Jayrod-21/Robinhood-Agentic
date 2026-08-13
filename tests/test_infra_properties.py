"""Regression tests for the three infra fixes (issue #32, review NEW-S4).

The 2026-07-28 infra review closed three blockers with no automated test guarding them:

  B1 — the DB password never appears in ANY process's argv (/proc/PID/cmdline is world-readable);
  B2 — files holding secrets are created at mode 0600 from the first instant, never chmod'd down
       from a looser umask-derived mode after the fact;
  B3 — bin/lib_ports.sh `port_is_free` detects bound ports, including host ports published by a
       STOPPED container and published RANGES (the live 9b hazard: `docker ps` renders an empty
       Ports column for stopped containers, so the pre-fix parser called 9b's ports free).

These are property tests against the real scripts, not assertions about comments:

  * B1/B2 run the actual `bin/db_up.sh` / `bin/db_migrate.sh` (copied verbatim into a scratch
    project tree so they operate on scratch state, never the repo's own db/.env) behind recorder
    shims: every external command the scripts spawn is intercepted on PATH, its full argv is
    logged, and the mode of the credentials file is sampled at each exec boundary. Reverting to
    the pre-fix implementations (password in awk's argv; create-then-chmod) turns both red —
    the shims record the leaked argv and the 644-mode window deterministically.
  * B3 exercises `port_is_free` against a genuinely bound socket, and drives
    `docker_published_ports` through a stub docker CLI that reproduces the stopped-container +
    published-range daemon state byte-for-byte at the CLI boundary, so the real parsing pipeline
    (xargs → inspect template output → range-expanding awk) is what's under test.

Stdlib + pytest only; no docker daemon, no network. Runs identically on the host and in the
CI-pinned python:3.12-slim container (bash, coreutils, grep, sed, awk are all in the base image;
`ss` and a real docker CLI are deliberately not required).
"""

from __future__ import annotations

import os
import shutil
import socket
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BIN = REPO_ROOT / "bin"

# Field separator for recorded argv: unit-separator can't appear in paths or generated secrets.
SEP = "\x1f"


def _bash(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=120
    )


# ── B3: port_is_free ──────────────────────────────────────────────────────────────────────────


def _port_is_free_rc(port: int, env: dict[str, str] | None = None) -> int:
    """Run the real three-check verdict exactly as pick_ports.sh does."""
    res = _bash(
        f'source "{BIN}/lib_ports.sh"; refresh_port_snapshots; port_is_free {port}', env=env
    )
    return res.returncode


def test_port_is_free_detects_a_bound_port_and_frees_on_release() -> None:
    """B3 core property: a port something actually holds is BUSY; the same port is FREE once
    released. The bind check makes this hold even where `ss` and docker are absent."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("0.0.0.0", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        assert _port_is_free_rc(port) != 0, f"port {port} is bound by this test yet reported free"
    finally:
        listener.close()
    assert _port_is_free_rc(port) == 0, f"port {port} was released yet still reported busy"


def test_stopped_container_published_ranges_are_seen_and_expanded(tmp_path: Path) -> None:
    """B3 regression shape from the review: a STOPPED container publishing the range 30222-30223.
    `docker ps` renders an empty Ports column for it, which is exactly why the pre-fix
    `docker ps --format {{.Ports}}` parser called such ports free. The stub docker reproduces the
    daemon's answers at the CLI boundary — `ps -aq` listing a stopped id, `inspect` emitting
    HostConfig.PortBindings HostPort values including a range — so the real pipeline (xargs,
    template output, anchored grep, range-expanding awk, sort) is what parses them."""
    stub = tmp_path / "docker"
    stub.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            # One RUNNING container publishing 20777; one STOPPED container publishing 30222-30223.
            case "$1" in
              ps)      printf 'c0ffee111111\\ndeadbeef2222\\n' ;;  # -aq: running AND stopped ids
              inspect) printf '20777\\n30222-30223\\n' ;;          # HostPort values, range included
              *)       exit 0 ;;
            esac
            """
        )
    )
    stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"}

    res = _bash(f'source "{BIN}/lib_ports.sh"; docker_published_ports', env=env)
    assert res.returncode == 0, res.stderr
    assert res.stdout.split() == ["20777", "30222", "30223"], (
        "published ports must include stopped containers and expand ranges port-by-port; "
        f"got: {res.stdout!r}"
    )

    # And the verdict honours them: every published port is BUSY even though nothing is bound —
    # the stopped container still owns its mapping in compose terms.
    for port in (20777, 30222, 30223):
        assert _port_is_free_rc(port, env=env) != 0, (
            f"port {port} is published by a (stopped) container yet reported free"
        )


# ── B1 + B2: secret handling in db_up.sh ──────────────────────────────────────────────────────


def _write_recorder_shim(shim_dir: Path, name: str, log: Path, watch: Path, body: str = "") -> None:
    """A PATH shim that records its argv and the watched file's current mode, then either runs
    `body` (stubs) or execs the real binary (pass-throughs)."""
    real_stat = shutil.which("stat")
    assert real_stat, "GNU stat is required for the mode probe"
    if not body:
        real = shutil.which(name)
        if real is None:  # not present on this box; nothing to shim
            return
        body = f'exec "{real}" "$@"'
    lines = [
        "#!/usr/bin/env bash",
        "{",
        f"  printf 'argv:{name}'",
        f"  for a in \"$@\"; do printf '{SEP}%s' \"$a\"; done",
        "  printf '\\n'",
        f'  if [ -e "{watch}" ]; then',
        f"    printf 'mode:%s\\n' \"$(\"{real_stat}\" -c %a \"{watch}\")\"",
        "  fi",
        f'}} >> "{log}"',
        body,
        "",
    ]
    shim = shim_dir / name
    shim.write_text("\n".join(lines))
    shim.chmod(0o755)


DOCKER_STUB_DB_UP = """\
case "$1" in
  inspect)
    case "$*" in
      *Health*) echo healthy ;;
      *) echo running ;;
    esac ;;
esac
exit 0
"""


@pytest.fixture(scope="module")
def db_up_run(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Run the REAL bin/db_up.sh once, in a scratch project tree, under a permissive umask, with
    every spawned command recorded. Docker is stubbed (info / volume create / compose up succeed,
    inspect reports healthy), so the script executes its full secret-generation path to exit 0."""
    root = tmp_path_factory.mktemp("db_up")
    proj, shims = root / "proj", root / "shims"
    (proj / "bin").mkdir(parents=True)
    (proj / "db").mkdir()
    shims.mkdir()
    shutil.copy(BIN / "db_up.sh", proj / "bin" / "db_up.sh")
    (proj / "db" / ".env.example").write_text(
        "POSTGRES_USER=rh\nPOSTGRES_DB=rh\nPOSTGRES_PASSWORD=replace-me\n"
    )

    log = root / "recorder.log"
    log.touch()
    env_file = proj / "db" / ".env"
    for name in ("python3", "grep", "date", "stat", "awk", "sed", "chmod", "seq", "sleep"):
        _write_recorder_shim(shims, name, log, env_file)
    _write_recorder_shim(shims, "docker", log, env_file, body=DOCKER_STUB_DB_UP)

    env = {**os.environ, "PATH": f"{shims}:{os.environ.get('PATH', '')}"}
    res = _bash(f'umask 022; exec bash "{proj}/bin/db_up.sh"', env=env)
    assert res.returncode == 0, f"db_up.sh failed under the harness:\n{res.stdout}\n{res.stderr}"

    password = ""
    for line in env_file.read_text().splitlines():
        if line.startswith("POSTGRES_PASSWORD="):
            password = line.split("=", 1)[1]
    argv_lines = [ln for ln in log.read_text().splitlines() if ln.startswith("argv:")]
    modes = [ln.split(":", 1)[1] for ln in log.read_text().splitlines() if ln.startswith("mode:")]
    return {"env_file": env_file, "password": password, "argv": argv_lines, "modes": modes}


def test_db_up_secret_never_appears_in_any_argv(db_up_run: dict) -> None:
    """B1: the generated password must never cross a command line — /proc/PID/cmdline is readable
    by every uid on the box. The recorder logged the argv of every external command db_up.sh
    spawned; the password must appear in none of them. (The pre-fix `awk -v pw=…` implementation
    puts it straight into awk's argv and turns this red.)"""
    pw = db_up_run["password"]
    assert pw and pw != "replace-me" and len(pw) >= 40, "generation did not produce a real secret"

    # Harness sanity: the run actually went through the recorder, including the generating python3
    # and the docker calls — an empty log would vacuously pass.
    joined = "\n".join(db_up_run["argv"])
    assert "argv:python3" in joined and "argv:docker" in joined and len(db_up_run["argv"]) >= 5

    leaked = [line for line in db_up_run["argv"] if pw in line]
    assert not leaked, f"password leaked into argv: {leaked}"


def test_db_up_secret_file_is_0600_from_creation(db_up_run: dict) -> None:
    """B2: db/.env must be born 0600 (O_CREAT|O_EXCL with mode 0600 inside one process), not
    created loose and chmod'd later. Every recorder shim sampled the file's mode at its own exec
    instant under umask 022; a create-then-chmod implementation exposes a 644 sample (the original
    defect), which turns this red."""
    modes = db_up_run["modes"]
    assert modes, "no mode samples recorded — the harness never observed the file"
    assert set(modes) == {"600"}, f"db/.env was observable at non-0600 modes: {sorted(set(modes))}"
    final = stat.S_IMODE(os.stat(db_up_run["env_file"]).st_mode)
    assert final == 0o600, f"final mode {oct(final)}"


# ── B1 (second surface): db_migrate.sh passes the credential by env NAME, never argv ──────────


DOCKER_STUB_MIGRATE = """\
case "$1" in
  inspect) echo running ;;
  image)   echo '' ;;
  run)     printf 'envpw:%s\\n' "$PGPASSWORD" >> "{log}" ;;
esac
exit 0
"""


def test_db_migrate_password_travels_by_env_name_not_argv(tmp_path: Path) -> None:
    """B1 on the consuming side: bin/db_migrate.sh reads db/.env strictly as data and hands the
    password to `docker run` by NAME (`--env PGPASSWORD`), so a password full of shell metatext —
    spaces, `$(…)`, `%`, `@` — must (a) never appear in any argv, (b) arrive in the runner's
    environment byte-for-byte, and (c) never be executed. A planted `$(touch canary)` password
    proves (c): the canary file appearing means some layer evaluated the secret as shell."""
    proj, shims = tmp_path / "proj", tmp_path / "shims"
    (proj / "bin").mkdir(parents=True)
    (proj / "db").mkdir()
    shims.mkdir()
    shutil.copy(BIN / "db_migrate.sh", proj / "bin" / "db_migrate.sh")

    canary = tmp_path / "canary_executed"
    planted = f"S3cret pa%40ss $(touch {canary}) `date` w@rd/end"
    (proj / "db" / ".env").write_text(
        f"POSTGRES_USER=rh\nPOSTGRES_DB=rh\nPOSTGRES_PASSWORD={planted}\n"
    )
    # Build inputs the script hashes before deciding to (stub-)build.
    (proj / "db" / "Dockerfile").write_text("FROM scratch\n")
    (proj / "db" / "requirements.txt").write_text("psycopg[binary]\n")

    log = tmp_path / "recorder.log"
    log.touch()
    _write_recorder_shim(
        shims, "docker", log, proj / "db" / ".env", body=DOCKER_STUB_MIGRATE.format(log=log)
    )

    env = {**os.environ, "PATH": f"{shims}:{os.environ.get('PATH', '')}"}
    res = _bash(f'exec bash "{proj}/bin/db_migrate.sh" status', env=env)
    assert res.returncode == 0, f"db_migrate.sh failed under the harness:\n{res.stdout}\n{res.stderr}"

    lines = log.read_text().splitlines()
    argv_lines = [ln for ln in lines if ln.startswith("argv:")]
    assert any(f"{SEP}run{SEP}" in ln for ln in argv_lines), "docker run was never invoked"
    leaked = [ln for ln in argv_lines if planted in ln]
    assert not leaked, f"password leaked into argv: {leaked}"
    # Passed by NAME on the command line…
    assert any(f"{SEP}--env{SEP}PGPASSWORD{SEP}" in ln for ln in argv_lines)
    # …and received byte-for-byte through the environment.
    assert f"envpw:{planted}" in lines, "PGPASSWORD did not reach the runner intact via env"
    assert not canary.exists(), "the password was EXECUTED as shell somewhere in the pipeline"


# ── B2 (second surface): rendered prompt files are 0600 ───────────────────────────────────────


def test_render_prompt_tempfile_is_0600_and_substituted(tmp_path: Path) -> None:
    """B2: the rendered prompt embeds the brokerage account number, so the temp file must be 0600
    even under a permissive umask. Also proves the render actually substituted the placeholder —
    a 0600 file with the template still inside would be a vacuous pass."""
    template = tmp_path / "prompt.md"
    template.write_text("account: __AGENTIC_ACCOUNT_NUMBER__\n")
    res = _bash(
        "umask 022; "
        f'source "{BIN}/lib_account.sh"; '
        "export AGENTIC_ACCOUNT_NUMBER=987654321; "
        f'render_prompt "{template}"'
    )
    assert res.returncode == 0, res.stderr
    rendered = Path(res.stdout.strip().splitlines()[-1])
    try:
        assert rendered.exists(), f"render_prompt printed no path: {res.stdout!r}"
        mode = stat.S_IMODE(os.stat(rendered).st_mode)
        assert mode == 0o600, f"rendered prompt is mode {oct(mode)}, expected 0600"
        assert rendered.read_text() == "account: 987654321\n"
    finally:
        if rendered.exists():
            rendered.unlink()
