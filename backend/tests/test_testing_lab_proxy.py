"""The backend's door to the Testing Lab: what it will not forward, and who it says asked.

The Lab authenticates nobody. That is safe only because it is unreachable except from inside this
process — no host port, no Caddy route, `rh-internal` only. So the security argument lives half in
deploy/docker-compose.prod.yml and half in this router, and these tests pin the half that is code:

    * the enumerated read allow-list, so a Lab route added later is not automatically exposed;
    * the operator stamp, so an experiment records an identity rather than a claim;
    * a clear 503 when the Lab is not deployed, rather than an obscure connection error;
    * a rate limit on runs, because each POST pins a core for minutes.
"""

from __future__ import annotations

import json

import httpx
import pytest
from app.routers import testing_lab as mod
from fastapi import HTTPException
from starlette.datastructures import State


class _Request:
    """The two things the router reads off a request."""

    def __init__(self, body: dict | None = None, operator=None, params: dict | None = None):
        self.state = State()
        if operator is not None:
            self.state.operator = operator
        self._body = body or {}
        self.query_params = params or {}

    async def json(self) -> dict:
        return self._body


class _Operator:
    def __init__(self, email: str):
        self.email = email


@pytest.fixture(autouse=True)
def _reset_limiters():
    mod._READ_LIMITER.reset()
    mod._RUN_LIMITER.reset()


@pytest.fixture
def deployed(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LAB_BASE_URL", "http://lab:8100")


# ── the allow-list ────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["health", "parameters", "datasets", "experiments", "compare"])
def test_the_enumerated_read_routes_are_reachable(path: str) -> None:
    assert any(path == r or path.startswith(f"{r}/") for r in mod._READ_ROUTES)


@pytest.mark.parametrize(
    "path", ["admin", "shutdown", "settings", "../health", "experiments-secret"]
)
@pytest.mark.anyio
async def test_anything_not_enumerated_is_404_before_a_socket_is_opened(
    path: str, deployed
) -> None:
    """Break: replace the allow-list with a {path:path} passthrough. A Lab endpoint added next
    month is then internet-reachable the moment it exists, without anyone reviewing it here."""
    with pytest.raises(HTTPException) as exc:
        await mod.lab_read(path, _Request())
    assert exc.value.status_code == 404


@pytest.mark.anyio
async def test_a_subpath_of_an_allowed_route_is_allowed(deployed, monkeypatch) -> None:
    """`experiments/41` must reach the Lab; `experiments-secret` must not. The check is on a path
    SEGMENT boundary, not a string prefix."""
    seen = {}

    async def _get(self, url, params=None):
        seen["url"] = url
        return httpx.Response(200, json={"id": 41}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", _get)
    response = await mod.lab_read("experiments/41", _Request())

    assert response.status_code == 200
    assert seen["url"].endswith("/api/testing-lab/experiments/41")


# ── attribution ───────────────────────────────────────────────────────────────────────────────


def test_the_operator_is_taken_from_the_session_not_the_body() -> None:
    """Break: forward the client's operator. The experiments table then records a claim rather
    than an identity — the rule routers/settings.py already follows for every audited write."""
    body = {"models": ["xgboost"], "operator": "joe@example.com"}
    stamped = mod._attributed(body, _Request(operator=_Operator("jared@example.com")))

    assert stamped["operator"] == "jared@example.com"
    assert stamped["models"] == ["xgboost"], "the rest of the body passes through untouched"


def test_an_unauthenticated_request_records_no_operator_rather_than_inventing_one() -> None:
    """A run attributed to nobody and a run attributed to "unknown" are different facts."""
    assert mod._attributed({}, _Request())["operator"] is None


def test_an_operator_without_an_email_still_gets_attributed() -> None:
    class _Bare:
        def __str__(self) -> str:
            return "operator-7"

    assert mod._attributed({}, _Request(operator=_Bare()))["operator"] == "operator-7"


# ── the Lab being absent, or unreachable ──────────────────────────────────────────────────────


def test_an_undeployed_lab_says_so_instead_of_failing_obscurely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unset LAB_BASE_URL turned into `http:///api/...` produces a connection error that reads
    like the Lab is broken rather than absent — two different problems for whoever is on call."""
    monkeypatch.delenv("LAB_BASE_URL", raising=False)

    with pytest.raises(HTTPException) as exc:
        mod._base_url()

    assert exc.value.status_code == 503
    assert "not deployed" in exc.value.detail


def test_a_blank_lab_url_is_treated_as_undeployed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAB_BASE_URL", "   ")
    with pytest.raises(HTTPException) as exc:
        mod._base_url()
    assert exc.value.status_code == 503


def test_a_trailing_slash_does_not_produce_a_double_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAB_BASE_URL", "http://lab:8100/")
    assert mod._base_url() == "http://lab:8100"


@pytest.mark.anyio
async def test_an_unreachable_lab_is_502_not_a_traceback(deployed, monkeypatch) -> None:
    async def _get(self, url, params=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx.AsyncClient, "get", _get)

    with pytest.raises(HTTPException) as exc:
        await mod.lab_read("health", _Request())
    assert exc.value.status_code == 502


@pytest.mark.anyio
async def test_a_stream_failure_arrives_as_an_sse_error_event_not_a_truncated_body(
    deployed, monkeypatch
) -> None:
    """The response has already begun by the time the upstream fails, so raising here would
    truncate the stream and the client would see a silent stall instead of a reason."""

    def _stream(self, method, url, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx.AsyncClient, "stream", _stream)
    response = await mod._stream("experiments/run", {})
    chunks = [chunk async for chunk in response.body_iterator]

    assert b"unreachable" in b"".join(chunks)
    assert b"data: " in b"".join(chunks)


# ── rate limits ───────────────────────────────────────────────────────────────────────────────


def test_runs_have_a_tighter_budget_than_reads() -> None:
    """Each POST pins a core for minutes; a GET reads a row."""
    assert mod._RUN_BUDGET[0] < mod._READ_BUDGET[0]


def test_the_run_budget_refuses_with_a_retry_after() -> None:
    allowed, _window = mod._RUN_BUDGET
    for _ in range(allowed):
        mod._gate(mod._RUN_LIMITER, mod._RUN_BUDGET, "Testing Lab run")

    with pytest.raises(HTTPException) as exc:
        mod._gate(mod._RUN_LIMITER, mod._RUN_BUDGET, "Testing Lab run")

    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"]


# ── the shape the router relays ───────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_a_lab_error_response_is_relayed_as_an_sse_error_event(deployed, monkeypatch) -> None:
    class _Upstream:
        status_code = 422

        async def aread(self):
            return b"a sweep is capped at 24 values"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(httpx.AsyncClient, "stream", lambda self, *a, **k: _Upstream())
    response = await mod._stream("sweeps", {})
    body = b"".join([chunk async for chunk in response.body_iterator]).decode()

    assert body.startswith("data: ")
    assert "capped at 24" in body
    assert json.loads(body[len("data: ") :])["type"] == "error"


def test_reads_are_never_cached_by_an_intermediary() -> None:
    """Same posture as every other /api/ response: this operator's work, no reason for a copy to
    live anywhere else."""
    source = mod.__file__
    with open(source, encoding="utf-8") as handle:
        assert 'private, no-store' in handle.read()


# ── the other half of the security argument: the compose file ─────────────────────────────────
#
# The Lab authenticates nobody, so the guarantee is topological — no host port, no Caddy route,
# `rh-internal` only. That guarantee lives in a YAML file, which is exactly the kind of thing that
# gets edited in a hurry during a deploy. These read it.
#
# Parsed textually rather than with PyYAML: yaml is not pinned in backend/requirements.txt or
# requirements.txt, so importing it here would make these tests silently skip-or-error in CI —
# and a security test that quietly stops running is worse than no test.

from pathlib import Path  # noqa: E402 — grouped with the compose tests it serves

_COMPOSE = Path(__file__).resolve().parents[2] / "deploy" / "docker-compose.prod.yml"


def _service_block(name: str) -> str:
    """The lines of one service, by indentation. Comments included — they are load-bearing here."""
    lines = _COMPOSE.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line == f"  {name}:")
    out = []
    for line in lines[start + 1 :]:
        if line and not line.startswith("    ") and not line.startswith("  #"):
            break
        out.append(line)
    return "\n".join(out)


def test_the_lab_is_not_published_to_the_host() -> None:
    """A `ports:` line here turns the Lab into an unauthenticated endpoint that trains models on
    demand. It has `expose:` only, which is container-network-visible and nothing more."""
    block = _service_block("lab")

    assert "\n    ports:" not in block, "the Testing Lab must never publish a host port"
    assert "\n    expose:" in block


def _field(block: str, name: str) -> list[str]:
    """The list items under one key of a service block, by indentation."""
    lines = block.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"{name}:")
    out = []
    for line in lines[start + 1 :]:
        if not line.strip() or line.strip().startswith("#"):
            continue
        if not line.startswith("      "):
            break
        out.append(line.strip().lstrip("- "))
    return out


def test_the_lab_is_only_on_the_internal_network() -> None:
    """Not dual-homed like the backend. It needs the database and nothing else, so it has no route
    to the internet — and therefore no way to exfiltrate what it reads even if it were
    compromised. Break: add `- default` to its networks."""
    networks = _field(_service_block("lab"), "networks")

    assert networks == ["rh-internal"], f"the Lab must be internal-only, got {networks}"


def test_caddy_proxies_only_to_the_backend_and_the_frontend() -> None:
    """Caddy sends /api/* to the backend, which proxies to the Lab WITH authentication. A
    `reverse_proxy lab:8100` in that file would bypass the session gate entirely."""
    caddyfile = (_COMPOSE.parent / "Caddyfile").read_text(encoding="utf-8")
    targets = {
        line.strip().split("reverse_proxy", 1)[1].strip()
        for line in caddyfile.splitlines()
        if line.strip().startswith("reverse_proxy")
    }

    assert targets == {"backend:8000", "frontend:3000"}, f"unexpected Caddy upstream: {targets}"


def test_every_secret_in_the_backend_env_is_explicitly_denied_to_the_lab() -> None:
    """The Lab reads backend/.env for DATABASE_URL, so it would inherit every other key in it.
    Each secret-shaped one is blanked in `environment:`, which overrides `env_file`.

    This test is the reason those blanks cannot silently rot: add a credential to backend/.env
    without denying it here, and this goes red. Break: delete one of the blank lines.
    """
    env_example = _COMPOSE.parents[1] / "backend" / ".env.example"
    keys = [
        line.split("=", 1)[0].strip()
        for line in env_example.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.strip().startswith("#")
    ]
    secretish = [
        k
        for k in keys
        if any(t in k for t in ("KEY", "SECRET", "PASS", "TOKEN", "AUTH_DATABASE_URL"))
    ]
    assert secretish, "the .env example should name at least one credential"

    block = _service_block("lab")
    missing = [k for k in secretish if f"\n      - {k}=\n" not in block + "\n"]

    assert not missing, (
        f"backend/.env carries {missing} and the lab service does not blank them. The Lab inherits "
        "env_file, so an undenied credential is a credential it holds."
    )


def test_the_lab_holds_the_database_url_and_nothing_else_that_matters() -> None:
    """The one thing it does need. Role rh_app, which migration 012 REVOKEd the auth tables from,
    so even this DSN cannot reach a password hash or a TOTP secret."""
    block = _service_block("lab")
    assert "DATABASE_URL=" not in block.split("env_file:")[0], "DATABASE_URL comes from env_file"
    assert "../backend/.env" in block


def test_the_backend_does_not_wait_on_the_lab_to_start() -> None:
    """The Lab imports xgboost, scikit-learn and statsmodels — real seconds. Gating the dashboard
    on it would let a Lab that fails to build take the portfolio page down with it."""
    backend = _service_block("backend")
    depends = backend.split("    depends_on:")[1] if "    depends_on:" in backend else ""
    assert "lab" not in depends
