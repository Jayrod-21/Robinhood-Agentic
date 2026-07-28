"""Integration tests: the runner against a real throwaway Postgres (testcontainers), and the
ACTUAL repo migrations 001-003 through a full up → down → up cycle.

Never touches the live rh-db — the container here is ephemeral and dies with the session.

Destructive CLASSIFICATION reads the FILENAME only (ADR-002); nothing inside a file can change
it. The end-to-end forgery corpus from verification rounds 1-4 is pinned here — note which layer
stops it: every body carries a literal (or comment-separated) `DROP TABLE`, so it is the
best-effort keyword sniff that refuses these at discovery (round 4 R4-S3). The corpus proves the
old directive mechanism is dead and the common literal shapes stay caught — NOT that arbitrary
contents are caught: a dynamic-SQL drop the sniff cannot see applies unmarked, and that
documented limitation is pinned below too. The "runner owns the transaction" property is enforced
by the server (transaction_status + xid), and its observed PG16 semantics — including what a
hijacking COMMIT leaves behind — are pinned too.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

try:  # testcontainers >= 4.x moved community modules; keep the fallback for older installs
    from testcontainers.community.postgres import PostgresContainer
except ImportError:  # pragma: no cover
    from testcontainers.postgres import PostgresContainer

from migrate import EXIT_OK, EXIT_SQL, EXIT_VALIDATION, main

REPO_MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"

# Same major as the live stack (docker-compose.db.yml pins postgres:16-alpine by digest).
PG_IMAGE = "postgres:16-alpine"


@pytest.fixture(scope="session")
def pg_container() -> Iterator[PostgresContainer]:
    with PostgresContainer(PG_IMAGE) as pg:
        yield pg


def _admin_url(pg: PostgresContainer) -> str:
    return (
        f"postgresql://{pg.username}:{pg.password}"
        f"@{pg.get_container_host_ip()}:{pg.get_exposed_port(5432)}/{pg.dbname}"
    )


@pytest.fixture
def db_url(pg_container: PostgresContainer, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A fresh database per test (cheap in one shared container), exported as DATABASE_URL."""
    name = f"tdb_{uuid.uuid4().hex[:12]}"
    admin = _admin_url(pg_container)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = admin.rsplit("/", 1)[0] + f"/{name}"
    monkeypatch.setenv("DATABASE_URL", url)
    yield url
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def write_pair(
    d: Path,
    version: str,
    name: str,
    up: str,
    down: str = "SELECT 1;",
    *,
    up_destructive: bool = False,
    down_destructive: bool = False,
) -> None:
    up_mark = ".destructive" if up_destructive else ""
    down_mark = ".destructive" if down_destructive else ""
    (d / f"{version}_{name}{up_mark}.up.sql").write_text(up, encoding="utf-8")
    (d / f"{version}_{name}{down_mark}.down.sql").write_text(down, encoding="utf-8")


def q(url: str, sql: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(url, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


# ── runner mechanics against scratch migrations ───────────────────────────────────────────────


def test_up_applies_and_rerun_is_noop(tmp_path: Path, db_url: str) -> None:
    write_pair(tmp_path, "001", "make_t", "CREATE TABLE t (i int);", "DROP TABLE t;", down_destructive=True)
    assert main(["up", "--migrations-dir", str(tmp_path)]) == EXIT_OK
    assert q(db_url, "SELECT to_regclass('public.t')")[0][0] == "t"
    assert q(db_url, "SELECT version, checksum FROM schema_migrations")[0][0] == "001"
    assert main(["up", "--migrations-dir", str(tmp_path)]) == EXIT_OK  # no pending → still 0


def test_failed_migration_rolls_back_body_and_bookkeeping_together(tmp_path: Path, db_url: str) -> None:
    """The atomicity guarantee (protected PRAISE item): a multi-statement body that fails must
    leave neither its DDL nor its bookkeeping row behind."""
    write_pair(
        tmp_path, "001", "boom",
        "CREATE TABLE t (i int); INSERT INTO t VALUES (1); SELECT 1/0;",
        "DROP TABLE t;",
        down_destructive=True,
    )
    assert main(["up", "--migrations-dir", str(tmp_path)]) == EXIT_SQL
    assert q(db_url, "SELECT to_regclass('public.t')")[0][0] is None
    assert q(db_url, "SELECT count(*) FROM schema_migrations")[0][0] == 0


def test_edited_applied_migration_halts_before_any_sql(tmp_path: Path, db_url: str) -> None:
    write_pair(tmp_path, "001", "a", "CREATE TABLE a (i int);", "DROP TABLE a;", down_destructive=True)
    assert main(["up", "--migrations-dir", str(tmp_path)]) == EXIT_OK
    (tmp_path / "001_a.up.sql").write_text("CREATE TABLE a (i int); -- edited", encoding="utf-8")
    write_pair(tmp_path, "002", "b", "CREATE TABLE b (i int);", "DROP TABLE b;", down_destructive=True)
    assert main(["up", "--migrations-dir", str(tmp_path)]) == EXIT_VALIDATION
    assert q(db_url, "SELECT to_regclass('public.b')")[0][0] is None  # 002 never ran


# ── the destructive gate: filename in, contents irrelevant ────────────────────────────────────


def test_destructive_gate_blocks_and_flag_allows(tmp_path: Path, db_url: str) -> None:
    write_pair(tmp_path, "001", "make", "CREATE TABLE gone (i int);", "DROP TABLE gone;", down_destructive=True)
    assert main(["up", "--migrations-dir", str(tmp_path)]) == EXIT_OK
    write_pair(
        tmp_path, "002", "drop_it", "DROP TABLE gone;", "CREATE TABLE gone (i int);",
        up_destructive=True,
    )
    assert main(["up", "--migrations-dir", str(tmp_path)]) == EXIT_VALIDATION  # blocked, loud
    assert q(db_url, "SELECT to_regclass('public.gone')")[0][0] == "gone"  # nothing applied
    assert main(["up", "--allow-destructive", "--migrations-dir", str(tmp_path)]) == EXIT_OK
    assert q(db_url, "SELECT to_regclass('public.gone')")[0][0] is None


def test_dry_run_evaluates_the_gate_at_plan_time(tmp_path: Path, db_url: str) -> None:
    write_pair(tmp_path, "001", "drop_it", "DROP TABLE IF EXISTS x;", up_destructive=True)
    assert main(["up", "--dry-run", "--migrations-dir", str(tmp_path)]) == EXIT_VALIDATION
    out_ok = main(["up", "--dry-run", "--allow-destructive", "--migrations-dir", str(tmp_path)])
    assert out_ok == EXIT_OK
    assert q(db_url, "SELECT count(*) FROM schema_migrations")[0][0] == 0  # dry run applied nothing


# The end-to-end forgery corpus from verification rounds 1-4. Every body below carries a real
# DROP TABLE plus the round's forged-classification payload. Under the old design each shape
# applied with exit 0 and no --allow-destructive. Under the filename design the file is UNMARKED
# and the SNIFF refuses each of these at discovery before any SQL runs — this corpus exercises
# the secondary net on shapes it is built to see (literal or comment-separated keywords), while
# the classification itself never consults contents. Shapes the sniff cannot see are pinned
# separately (test_dynamic_sql_drop_applies_unmarked_documented_limitation).
FORGERY_BODIES = [
    pytest.param(  # round 1 (B-1): directive inside a dollar-quoted DO body
        "DROP TABLE users;\nDO $$\nBEGIN\n-- migrate: non-destructive\nNULL;\nEND\n$$;\n",
        id="directive-in-dollar-body",
    ),
    pytest.param(  # round 2 (NEW-B1): non-ASCII dollar tag defeated the ASCII-only tag regex
        "COMMENT ON TABLE users IS $café$\n-- migrate: non-destructive\n$café$;\nDROP TABLE users;\n",
        id="nonascii-dollar-tag",
    ),
    pytest.param(  # round 2 (NEW-S1): directive trailing a multi-line body's closing $$
        "DROP TABLE users;\nDO $$\nBEGIN\nNULL;\nEND\n$$ -- migrate: non-destructive\n;\n",
        id="directive-trailing-dollar-close",
    ),
    pytest.param(  # round 3 (NEW2-B1 #1): directive VALUE smuggled from executable code
        "WITH t AS (SELECT 5 AS non, 2 AS destructive)\nSELECT\n-- migrate:\nnon-destructive\nFROM t;\nDROP TABLE users;\n",
        id="split-directive-value-from-code",
    ),
    pytest.param(  # round 3 (NEW2-S1 #5): U+00A0 is a PG identifier char but Python whitespace
        "CREATE TABLE meta (\n\u00a0-- migrate: non-destructive\n int);\nDROP TABLE users;\n",
        id="nbsp-own-line-forgery",
    ),
    pytest.param(  # plain directive on its own line — the directive mechanism itself is dead
        "-- migrate: non-destructive\nDROP TABLE users;\n",
        id="plain-directive-is-inert",
    ),
    pytest.param(  # round 4 (R4-B1 #1): a comment is a token separator to PostgreSQL, so this is
        # a valid DROP TABLE with no whitespace between the keywords — it evaded the
        # whitespace-only sniff and applied unmarked with exit 0 before round 5.
        "drop/**/table users;\n",
        id="comment-separated-drop",
    ),
]


@pytest.mark.parametrize("body", FORGERY_BODIES)
def test_forged_contents_cannot_apply_a_drop_without_the_flag(tmp_path: Path, db_url: str, body: str) -> None:
    write_pair(tmp_path, "001", "make", "CREATE TABLE users (i int);", "DROP TABLE users;", down_destructive=True)
    assert main(["up", "--migrations-dir", str(tmp_path)]) == EXIT_OK
    write_pair(tmp_path, "002", "attack", body, "SELECT 1;")
    assert main(["up", "--migrations-dir", str(tmp_path)]) == EXIT_VALIDATION
    assert q(db_url, "SELECT to_regclass('public.users')")[0][0] == "users"  # table survived
    assert q(db_url, "SELECT count(*) FROM schema_migrations")[0][0] == 1  # 002 never recorded


def test_marked_destructive_migration_ignores_forged_directive_text(tmp_path: Path, db_url: str) -> None:
    """Even a properly marked file gets no say from its contents: a 'non-destructive' directive
    inside it changes nothing — the flag is still required, and with the flag it applies."""
    write_pair(tmp_path, "001", "make", "CREATE TABLE users (i int);", "DROP TABLE users;", down_destructive=True)
    assert main(["up", "--migrations-dir", str(tmp_path)]) == EXIT_OK
    body = "-- migrate: non-destructive\nDROP TABLE users;\n"
    write_pair(tmp_path, "002", "attack", body, "SELECT 1;", up_destructive=True)
    assert main(["up", "--migrations-dir", str(tmp_path)]) == EXIT_VALIDATION  # gate holds
    assert q(db_url, "SELECT to_regclass('public.users')")[0][0] == "users"
    assert main(["up", "--allow-destructive", "--migrations-dir", str(tmp_path)]) == EXIT_OK
    assert q(db_url, "SELECT to_regclass('public.users')")[0][0] is None


def test_dynamic_sql_drop_applies_unmarked_documented_limitation(tmp_path: Path, db_url: str) -> None:
    """HONEST PIN of the documented limitation (round 4 R4-B1 shapes 4-7): a destructive
    statement built at runtime contains no keyword any text rule can see, so this UNMARKED body
    drops `users` with exit 0 and NO --allow-destructive, and the migration is recorded. That is
    the reality migrate.py, ADR-002, and TESTS.md now document: the sniff is a best-effort
    secondary net, and the filename marker set by the author is the only control for this shape.
    If this test ever fails with EXIT_VALIDATION, the sniff has started lexing or evaluating SQL
    — re-read ADR-002's history before keeping that change."""
    write_pair(tmp_path, "001", "make", "CREATE TABLE users (i int);", "DROP TABLE users;", down_destructive=True)
    assert main(["up", "--migrations-dir", str(tmp_path)]) == EXIT_OK
    write_pair(tmp_path, "002", "attack", "DO $$ BEGIN EXECUTE 'DR' || 'OP TABLE users'; END $$;")
    assert main(["up", "--migrations-dir", str(tmp_path)]) == EXIT_OK  # applies — documented, not defended
    assert q(db_url, "SELECT to_regclass('public.users')")[0][0] is None  # the table really is gone
    assert q(db_url, "SELECT count(*) FROM schema_migrations")[0][0] == 2  # and 002 was recorded


# ── the runner owns the transaction: server-enforced, observed PG16 semantics ─────────────────


def test_stray_commit_is_detected_and_not_recorded(tmp_path: Path, db_url: str) -> None:
    """A body that COMMITs decouples schema changes from bookkeeping. The status check fires
    AFTER execution (libpq reports IDLE), the migration is NOT recorded, exit is 1 — and, as
    documented, the statements the stray COMMIT made durable really are durable: this test pins
    the honest failure mode, not a pretty one."""
    write_pair(tmp_path, "001", "hijack", "CREATE TABLE a (i int); COMMIT; CREATE TABLE b (i int);")
    assert main(["up", "--migrations-dir", str(tmp_path)]) == EXIT_VALIDATION
    assert q(db_url, "SELECT count(*) FROM schema_migrations")[0][0] == 0  # never recorded
    # Observed on PG16: the pre-COMMIT statement is durable; the post-COMMIT statement committed
    # with the batch's implicit transaction. Both survive — which is exactly why the error tells
    # the operator to inspect and clean up manually.
    assert q(db_url, "SELECT to_regclass('public.a')")[0][0] == "a"
    assert q(db_url, "SELECT to_regclass('public.b')")[0][0] == "b"


def test_stray_rollback_is_detected_and_not_recorded(tmp_path: Path, db_url: str) -> None:
    write_pair(tmp_path, "001", "hijack", "CREATE TABLE a (i int); ROLLBACK; CREATE TABLE b (i int);")
    assert main(["up", "--migrations-dir", str(tmp_path)]) == EXIT_VALIDATION
    assert q(db_url, "SELECT count(*) FROM schema_migrations")[0][0] == 0
    assert q(db_url, "SELECT to_regclass('public.a')")[0][0] is None  # rolled back by the stray
    assert q(db_url, "SELECT to_regclass('public.b')")[0][0] == "b"  # implicit-tx committed


def test_commit_begin_forging_intrans_is_caught_by_the_xid_check(tmp_path: Path, db_url: str) -> None:
    """`COMMIT; BEGIN;` restores transaction_status to INTRANS — the status check alone would
    pass and the bookkeeping row would commit in the WRONG transaction. pg_current_xact_id()
    changed (observed live: new xid), the check raises, the hijacker's second transaction is
    rolled back by the transaction-block exit, and nothing is recorded."""
    write_pair(tmp_path, "001", "hijack", "CREATE TABLE a (i int); COMMIT; BEGIN; CREATE TABLE b (i int);")
    assert main(["up", "--migrations-dir", str(tmp_path)]) == EXIT_VALIDATION
    assert q(db_url, "SELECT count(*) FROM schema_migrations")[0][0] == 0
    assert q(db_url, "SELECT to_regclass('public.a')")[0][0] == "a"  # committed by the hijack
    assert q(db_url, "SELECT to_regclass('public.b')")[0][0] is None  # 2nd tx rolled back


def test_down_hijack_leaves_the_migration_recorded(tmp_path: Path, db_url: str) -> None:
    write_pair(tmp_path, "001", "t", "CREATE TABLE t (i int);", "COMMIT;")
    assert main(["up", "--migrations-dir", str(tmp_path)]) == EXIT_OK
    assert main(["down", "--allow-destructive", "--migrations-dir", str(tmp_path)]) == EXIT_VALIDATION
    # The bookkeeping DELETE never ran: 001 is still recorded as applied.
    assert q(db_url, "SELECT count(*) FROM schema_migrations")[0][0] == 1


def test_plpgsql_and_tx_keywords_in_text_are_not_false_positives(tmp_path: Path, db_url: str) -> None:
    """With the lexer gone there is nothing to false-positive: DO $$ BEGIN … END $$, COMMIT
    inside a string, BEGIN in a comment, and a "begin" column name all just apply. The server
    itself is the arbiter of what is transaction control."""
    body = (
        "CREATE TABLE t (\"begin\" int);\n"
        "COMMENT ON TABLE t IS 'we commit to quality';\n"
        "-- COMMIT mentioned in a comment\n"
        "DO $$ BEGIN INSERT INTO t VALUES (1); END $$;\n"
    )
    write_pair(tmp_path, "001", "legit", body)
    assert main(["up", "--migrations-dir", str(tmp_path)]) == EXIT_OK
    assert q(db_url, 'SELECT "begin" FROM t')[0][0] == 1
    assert q(db_url, "SELECT count(*) FROM schema_migrations")[0][0] == 1


def test_bare_begin_and_savepoint_are_tolerated_with_atomicity_intact(tmp_path: Path, db_url: str) -> None:
    """Observed live: BEGIN inside an open transaction is a server-side no-op warning and
    SAVEPOINT stays inside the runner's transaction — same xid, status INTRANS, atomicity
    unharmed. The runner therefore tolerates both (the old text-scan rejected them)."""
    write_pair(tmp_path, "001", "b", "CREATE TABLE t (i int); BEGIN; SAVEPOINT sp1; INSERT INTO t VALUES (1);")
    assert main(["up", "--migrations-dir", str(tmp_path)]) == EXIT_OK
    assert q(db_url, "SELECT i FROM t")[0][0] == 1
    assert q(db_url, "SELECT count(*) FROM schema_migrations")[0][0] == 1


# ── rollback and dry-run mechanics ────────────────────────────────────────────────────────────


def test_down_requires_flag_and_target_000_rolls_back_all(tmp_path: Path, db_url: str) -> None:
    write_pair(tmp_path, "001", "a", "CREATE TABLE a (i int);", "DROP TABLE a;", down_destructive=True)
    write_pair(tmp_path, "002", "b", "CREATE TABLE b (i int);", "DROP TABLE b;", down_destructive=True)
    assert main(["up", "--migrations-dir", str(tmp_path)]) == EXIT_OK
    assert main(["down", "--migrations-dir", str(tmp_path)]) == EXIT_VALIDATION  # gate, loud
    assert main(["down", "--allow-destructive", "--migrations-dir", str(tmp_path)]) == EXIT_OK
    assert q(db_url, "SELECT to_regclass('public.b')")[0][0] is None  # bare down: exactly one
    assert q(db_url, "SELECT to_regclass('public.a')")[0][0] == "a"
    assert main(["up", "--migrations-dir", str(tmp_path)]) == EXIT_OK
    assert (
        main(["down", "--allow-destructive", "--target", "000", "--migrations-dir", str(tmp_path)])
        == EXIT_OK
    )
    assert q(db_url, "SELECT count(*) FROM schema_migrations")[0][0] == 0


def test_unmarked_down_rollback_still_requires_flag(tmp_path: Path, db_url: str) -> None:
    """PRAISE-protected: EVERY rollback needs --allow-destructive, marker or no marker — a down
    body discards its up's schema by nature."""
    write_pair(tmp_path, "001", "a", "CREATE TABLE a (i int);", "SELECT 1;")
    assert main(["up", "--migrations-dir", str(tmp_path)]) == EXIT_OK
    assert main(["down", "--migrations-dir", str(tmp_path)]) == EXIT_VALIDATION
    assert main(["down", "--allow-destructive", "--migrations-dir", str(tmp_path)]) == EXIT_OK


def test_dry_run_plans_without_applying(tmp_path: Path, db_url: str, capsys: pytest.CaptureFixture[str]) -> None:
    write_pair(tmp_path, "001", "a", "CREATE TABLE a (i int);", "DROP TABLE a;", down_destructive=True)
    assert main(["up", "--dry-run", "--migrations-dir", str(tmp_path)]) == EXIT_OK
    assert "would apply: 001_a" in capsys.readouterr().out
    assert q(db_url, "SELECT to_regclass('public.a')")[0][0] is None


def test_bad_credentials_exit_connection(tmp_path: Path, db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    write_pair(tmp_path, "001", "a", "SELECT 1;", "SELECT 1;")
    monkeypatch.setenv("DATABASE_URL", db_url.replace("://test:", "://test_wrong_user:"))
    assert main(["status", "--migrations-dir", str(tmp_path)]) == 3


# ── the ACTUAL migrations: full cycle plus schema-behavior verification ───────────────────────


def test_real_migrations_are_classified_from_filenames() -> None:
    from migrate import discover_migrations

    migs = discover_migrations(REPO_MIGRATIONS)
    assert [(m.version, m.up_destructive, m.down_destructive) for m in migs] == [
        ("001", False, True),
        ("002", False, True),
        ("003", False, True),
    ]


def test_real_migrations_up_down_up(db_url: str) -> None:
    md = str(REPO_MIGRATIONS)
    assert main(["up", "--migrations-dir", md]) == EXIT_OK

    # Core objects exist.
    assert q(db_url, "SELECT to_regclass('public.securities')")[0][0] == "securities"
    assert q(db_url, "SELECT to_regclass('public.price_bars_minute')")[0][0] == "price_bars_minute"
    assert q(db_url, "SELECT to_regclass('public.fundamentals_snapshots')")[0][0] == "fundamentals_snapshots"

    # S9: runtime role exists, is not superuser, has no password set.
    rows = q(db_url, "SELECT rolsuper, rolcanlogin FROM pg_roles WHERE rolname = 'rh_app'")
    assert rows == [(False, True)]

    # F2: real full-universe symbol forms accepted; junk rejected.
    src = q(
        db_url,
        "INSERT INTO data_sources (provider, dataset, fetched_at) "
        "VALUES ('polygon', 'minute_bars', now()) RETURNING id",
    )[0][0]
    ids = {}
    for sym in ("AAPL", "BRK.B", "BACpA", "TDW.WS.A", "AANw"):
        ids[sym] = q(
            db_url,
            "INSERT INTO securities (symbol, source_id) VALUES (%s, %s) RETURNING id",
            (sym, src),
        )[0][0]
    with pytest.raises(psycopg.errors.CheckViolation):
        q(db_url, "INSERT INTO securities (symbol) VALUES ('BAD SYM')")

    # F3: one LIVE holder per symbol; a delisted predecessor plus a live successor coexist.
    with pytest.raises(psycopg.errors.UniqueViolation):
        q(db_url, "INSERT INTO securities (symbol) VALUES ('AAPL')")
    q(db_url, "UPDATE securities SET delisted_at = '2021-06-01', first_seen = '2000-01-01' WHERE id = %s", (ids["AAPL"],))
    q(db_url, "INSERT INTO securities (symbol) VALUES ('AAPL')")  # recycled ticker = new row

    # B2: the EST month-end spillover bar (2020-11-30 file, 19:30 ET → 00:30 UTC Dec 1) has a
    # partition to land in, and lands in the RIGHT one.
    q(
        db_url,
        "INSERT INTO price_bars_minute (security_id, ts, open, high, low, close, volume, source_id) "
        "VALUES (%s, '2020-12-01 00:30:00+00', 10, 11, 9, 10.5, 100, %s)",
        (ids["BRK.B"], src),
    )
    assert q(
        db_url,
        "SELECT tableoid::regclass::text FROM price_bars_minute WHERE security_id = %s",
        (ids["BRK.B"],),
    )[0][0] == "price_bars_minute_2020_12"
    # No DEFAULT partition: an out-of-range timestamp fails LOUDLY instead of wedging later.
    with pytest.raises(psycopg.errors.CheckViolation, match="no partition"):
        q(
            db_url,
            "INSERT INTO price_bars_minute (security_id, ts, open, high, low, close, volume) "
            "VALUES (%s, '2019-01-01 15:00:00+00', 1, 1, 1, 1, 0)",
            (ids["BRK.B"],),
        )
    # Helper: range-covering, idempotent, and guarded against garbage ranges.
    made = q(db_url, "SELECT ensure_price_bar_partitions(DATE '2026-01-15', DATE '2026-03-02')")[0][0]
    assert made == ["price_bars_minute_2026_01", "price_bars_minute_2026_02", "price_bars_minute_2026_03"]
    assert q(db_url, "SELECT ensure_price_bar_partitions(DATE '2026-01-15', DATE '2026-03-02')")[0][0] == made
    with pytest.raises(psycopg.errors.RaiseException, match="before p_from"):
        q(db_url, "SELECT ensure_price_bar_partitions(DATE '2026-02-01', DATE '2026-01-01')")
    with pytest.raises(psycopg.errors.RaiseException, match="240"):
        q(db_url, "SELECT ensure_price_bar_partitions(DATE '1970-01-01', DATE '2026-01-01')")

    # B1: restatements coexist; identical observations dedupe; the natural loader is DO NOTHING.
    sec = ids["BACpA"]
    q(
        db_url,
        "INSERT INTO fundamentals_snapshots (security_id, period_end, period_type, known_at, peg_ratio) "
        "VALUES (%s, '2021-03-31', 'quarterly', '2021-05-05 12:00+00', 1.5)",
        (sec,),
    )
    q(  # the restatement: same period, later known_at — must COEXIST, not overwrite
        db_url,
        "INSERT INTO fundamentals_snapshots (security_id, period_end, period_type, known_at, peg_ratio) "
        "VALUES (%s, '2021-03-31', 'quarterly', '2021-08-09 12:00+00', 1.7)",
        (sec,),
    )
    assert q(db_url, "SELECT count(*) FROM fundamentals_snapshots WHERE security_id = %s", (sec,))[0][0] == 2
    with pytest.raises(psycopg.errors.UniqueViolation):  # identical observation is a duplicate
        q(
            db_url,
            "INSERT INTO fundamentals_snapshots (security_id, period_end, period_type, known_at) "
            "VALUES (%s, '2021-03-31', 'quarterly', '2021-08-09 12:00+00')",
            (sec,),
        )
    # NULLS NOT DISTINCT: two unsourced, known_at-unknown loads of the same period also collide.
    q(
        db_url,
        "INSERT INTO fundamentals_snapshots (security_id, period_end, period_type) "
        "VALUES (%s, '2021-06-30', 'quarterly')",
        (sec,),
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        q(
            db_url,
            "INSERT INTO fundamentals_snapshots (security_id, period_end, period_type) "
            "VALUES (%s, '2021-06-30', 'quarterly')",
            (sec,),
        )

    # F1: the accessor is point-in-time correct — before the restatement was known it returns the
    # original; after, the restatement; rows with NULL known_at are invisible to it.
    # Decimal, not float, comes back — and must be compared as Decimal (1.7 has no exact float).
    assert q(db_url, "SELECT peg_ratio FROM fundamentals_asof(%s, '2021-06-01')", (sec,))[0][0] == Decimal("1.5")
    assert q(db_url, "SELECT peg_ratio FROM fundamentals_asof(%s, '2021-12-01')", (sec,))[0][0] == Decimal("1.7")
    assert q(db_url, "SELECT count(*) FROM fundamentals_asof(%s, '2021-04-01')", (sec,))[0][0] == 0

    # N1: known_at earlier than period_end (UTC-anchored) is a data error.
    with pytest.raises(psycopg.errors.CheckViolation):
        q(
            db_url,
            "INSERT INTO fundamentals_snapshots (security_id, period_end, period_type, known_at) "
            "VALUES (%s, '2021-03-31', 'annual', '2021-03-30 23:00+00')",
            (sec,),
        )

    # F4: data_sources is append-only — deleting a referenced source is refused, not cascaded.
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        q(db_url, "DELETE FROM data_sources WHERE id = %s", (src,))

    # Full teardown and re-apply: every down truly reverses its up, leaving no residue.
    assert main(["down", "--allow-destructive", "--target", "000", "--migrations-dir", md]) == EXIT_OK
    assert q(db_url, "SELECT to_regclass('public.securities')")[0][0] is None
    assert q(db_url, "SELECT count(*) FROM pg_roles WHERE rolname = 'rh_app'")[0][0] == 0
    assert q(db_url, "SELECT count(*) FROM pg_proc WHERE proname = 'ensure_price_bar_partitions'")[0][0] == 0
    assert main(["up", "--migrations-dir", md]) == EXIT_OK
    assert q(db_url, "SELECT count(*) FROM schema_migrations")[0][0] == 3
