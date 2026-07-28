"""Integration tests: the runner against a real throwaway Postgres (testcontainers), and the
ACTUAL repo migrations 001-004 through a full up → down → up cycle.

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
        ("004", False, True),
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
    # 004's trigger functions leave no residue either.
    assert q(db_url, "SELECT count(*) FROM pg_proc WHERE proname LIKE 'enforce\\_%%'")[0][0] == 0
    assert main(["up", "--migrations-dir", md]) == EXIT_OK
    assert q(db_url, "SELECT count(*) FROM schema_migrations")[0][0] == 4


# ── 004: the evaluation tables ────────────────────────────────────────────────────────────────
# EVALUATION_FRAMEWORK.md demands enforced invariants, not recorded intentions (the 004 fix-pass
# theme). Each test below pins one defence against a specific way the system could fool itself;
# each was proven to go red when its defence was reverted on a scratch copy of the migration.


def _seed_agents(db_url: str) -> dict[str, int]:
    """The standard cast: a persona, a second persona, the blind control, a judge, the real
    account, and one security."""
    ids: dict[str, int] = {}
    for key, kind in (
        ("bull", "persona"),
        ("bear", "persona"),
        ("blind", "blind"),
        ("judge_risk", "judge"),
        ("real", "real"),
    ):
        ids[key] = q(
            db_url,
            "INSERT INTO agents (agent_key, version, kind) VALUES (%s, 1, %s) RETURNING id",
            (key, kind),
        )[0][0]
    ids["sec"] = q(db_url, "INSERT INTO securities (symbol) VALUES ('NVDA') RETURNING id")[0][0]
    return ids


def _seed_debate(db_url: str, ids: dict[str, int]) -> tuple[int, int]:
    """A ticker debate (context cutoff 45 days back) with the bull's buy proposal."""
    deb = q(
        db_url,
        "INSERT INTO debates (scope, security_id, question, context_as_of, started_at) "
        "VALUES ('ticker', %s, 'Buy NVDA?', now() - interval '45 days', now() - interval '45 days') "
        "RETURNING id",
        (ids["sec"],),
    )[0][0]
    prop = q(
        db_url,
        "INSERT INTO agent_proposals (debate_id, agent_id, stance, rationale) "
        "VALUES (%s, %s, 'buy', 'entry thesis') RETURNING id",
        (deb, ids["bull"]),
    )[0][0]
    return deb, prop


def _seed_scored_portfolio(db_url: str, ids: dict[str, int], deb: int, prop: int) -> int:
    """The bull's counterfactual, marked daily for 30 days and evaluated once."""
    pid = q(
        db_url,
        "INSERT INTO paper_portfolios (kind, agent_id, debate_id, proposal_id, inception_date) "
        "VALUES ('counterfactual', %s, %s, %s, CURRENT_DATE - 40) RETURNING id",
        (ids["bull"], deb, prop),
    )[0][0]
    _mark_daily(db_url, pid, days=30)
    q(
        db_url,
        "INSERT INTO evaluation_runs (portfolio_id, window_start, window_end, n_observations, "
        "min_n_for_ranking, risk_free_annual, sharpe, sortino, max_drawdown, hit_rate, inputs_as_of) "
        "VALUES (%s, CURRENT_DATE - 30, CURRENT_DATE, 30, 21, 0.0525, 1.42, 2.10, -0.18, 0.55, now())",
        (pid,),
    )
    return pid


def _mark_daily(db_url: str, pid: int, days: int) -> None:
    """days consecutive daily marks ending today, each priced the same UTC evening — inside the
    ck_prd_mark_window bound and after the seed portfolios' inception (CURRENT_DATE - 40)."""
    q(
        db_url,
        "INSERT INTO portfolio_returns_daily (portfolio_id, trade_date, market_value, daily_return, priced_as_of) "
        "SELECT %s, CURRENT_DATE - %s + g, 100000 + g, 0.001, "
        "((CURRENT_DATE - %s + g)::timestamp AT TIME ZONE 'UTC') + interval '21 hours' "
        "FROM generate_series(0, %s - 1) AS g",
        (pid, days - 1, days - 1, days),
    )


def test_evaluation_schema_enforces_sample_size(db_url: str) -> None:
    """A Sharpe or Sortino without its sample size must be unstorable.

    Standard deviation is undefined for n < 2, so a ratio reported with fewer observations is
    arithmetically impossible — not merely unreliable. The framework's concern is a persona that
    looks brilliant over six days being ranked as if that meant something.
    """
    md = str(REPO_MIGRATIONS)
    assert main(["up", "--migrations-dir", md]) == EXIT_OK

    ids = _seed_agents(db_url)
    pid = q(
        db_url,
        "INSERT INTO paper_portfolios (kind, agent_id, inception_date) "
        "VALUES ('blind', %s, CURRENT_DATE - 40) RETURNING id",
        (ids["blind"],),
    )[0][0]

    # n_observations is NOT NULL: omitting it fails rather than defaulting to something.
    with pytest.raises(psycopg.errors.NotNullViolation):
        q(
            db_url,
            "INSERT INTO evaluation_runs (portfolio_id, window_start, window_end, "
            "min_n_for_ranking, risk_free_annual, inputs_as_of) "
            "VALUES (%s, CURRENT_DATE - 10, CURRENT_DATE, 21, 0.05, now())",
            (pid,),
        )

    # A Sharpe (or Sortino) claimed on a single observation is rejected at the CHECK, before the
    # count-verification trigger even runs.
    for ratio_col in ("sharpe", "sortino"):
        with pytest.raises(psycopg.errors.CheckViolation):
            q(
                db_url,
                f"INSERT INTO evaluation_runs (portfolio_id, window_start, window_end, n_observations, "
                f"min_n_for_ranking, risk_free_annual, {ratio_col}, inputs_as_of) "
                f"VALUES (%s, CURRENT_DATE - 10, CURRENT_DATE, 1, 21, 0.05, 1.5, now())",
                (pid,),
            )

    # With enough observations (and the marks to prove them) it stores, and is_rankable reflects
    # the recorded ranking floor.
    _mark_daily(db_url, pid, days=30)
    q(
        db_url,
        "INSERT INTO evaluation_runs (portfolio_id, window_start, window_end, n_observations, "
        "min_n_for_ranking, risk_free_annual, sharpe, sortino, max_drawdown, hit_rate, inputs_as_of) "
        "VALUES (%s, CURRENT_DATE - 30, CURRENT_DATE, 30, 21, 0.0525, 1.42, 2.10, -0.18, 0.55, now())",
        (pid,),
    )
    assert q(db_url, "SELECT is_rankable FROM evaluation_runs")[0][0] is True
    q(
        db_url,
        "INSERT INTO evaluation_runs (portfolio_id, window_start, window_end, n_observations, "
        "min_n_for_ranking, risk_free_annual, sharpe, inputs_as_of) "
        "VALUES (%s, CURRENT_DATE - 30, CURRENT_DATE, 30, 63, 0.0525, 1.42, now())",
        (pid,),
    )
    assert q(db_url, "SELECT is_rankable FROM evaluation_runs WHERE min_n_for_ranking = 63")[0][0] is False


def test_evaluation_runs_are_append_only(db_url: str) -> None:
    """Append-only is a REVOKE, not a comment (fix-pass B7/F-8).

    Two halves: recomputing with different reward weights coexists as a NEW row (no over-eager
    uniqueness), and the runtime role really cannot rewrite history — UPDATE/DELETE are absent on
    every observation/history table, with exactly the lifecycle columns granted back.
    """
    md = str(REPO_MIGRATIONS)
    assert main(["up", "--migrations-dir", md]) == EXIT_OK

    ids = _seed_agents(db_url)
    pid = q(
        db_url,
        "INSERT INTO paper_portfolios (kind, agent_id, inception_date) "
        "VALUES ('blind', %s, CURRENT_DATE - 40) RETURNING id",
        (ids["blind"],),
    )[0][0]
    _mark_daily(db_url, pid, days=30)

    for weights in ('{"w_sortino": 0.5}', '{"w_sortino": 0.7}'):
        q(
            db_url,
            "INSERT INTO evaluation_runs (portfolio_id, window_start, window_end, n_observations, "
            "min_n_for_ranking, risk_free_annual, sharpe, inputs_as_of, reward_weights) "
            "VALUES (%s, CURRENT_DATE - 30, CURRENT_DATE, 30, 21, 0.05, 1.42, now(), %s::jsonb)",
            (pid, weights),
        )
    # Same portfolio, same window, two weightings — both retained.
    assert q(db_url, "SELECT count(*) FROM evaluation_runs WHERE portfolio_id = %s", (pid,))[0][0] == 2

    # The enforcement half: no UPDATE/DELETE for rh_app on any history table.
    for table in (
        "evaluation_runs",
        "portfolio_returns_daily",
        "agent_proposals",
        "agent_proposal_positions",
        "judgments",
        "knowledge_base_entries",
        "guardrail_events",
        "risk_free_rates",
    ):
        for priv in ("UPDATE", "DELETE"):
            assert q(
                db_url, "SELECT has_table_privilege('rh_app', %s, %s)", (table, priv)
            )[0][0] is False, f"rh_app must not hold {priv} on {table}"
        assert q(db_url, "SELECT has_table_privilege('rh_app', %s, 'INSERT')", (table,))[0][0] is True
    # Agents are retired, never deleted.
    assert q(db_url, "SELECT has_table_privilege('rh_app', 'agents', 'DELETE')")[0][0] is False
    # Column-level lifecycle grants: the judgment's outcome link is settable, the ruling is not.
    assert q(
        db_url,
        "SELECT has_column_privilege('rh_app', 'judgments', 'resulting_portfolio_id', 'UPDATE')",
    )[0][0] is True
    assert q(db_url, "SELECT has_column_privilege('rh_app', 'judgments', 'decision', 'UPDATE')")[0][0] is False


def test_evaluation_schema_shape_constraints(db_url: str) -> None:
    """The structural invariants that keep the counterfactual machinery honest."""
    md = str(REPO_MIGRATIONS)
    assert main(["up", "--migrations-dir", md]) == EXIT_OK

    ids = _seed_agents(db_url)

    # The blind control is a singleton — two would make "the control" ambiguous.
    with pytest.raises(psycopg.errors.UniqueViolation):
        q(db_url, "INSERT INTO agents (agent_key, version, kind) VALUES ('blind2', 1, 'blind')")
    # …but retire-then-replace is the supported path (partial unique on live rows only).
    q(db_url, "UPDATE agents SET retired_at = now() WHERE agent_key = 'blind'")
    q(db_url, "INSERT INTO agents (agent_key, version, kind) VALUES ('blind2', 1, 'blind')")
    # One live version per key, several retired ones.
    q(db_url, "UPDATE agents SET retired_at = now() WHERE agent_key = 'bull'")
    q(db_url, "INSERT INTO agents (agent_key, version, kind) VALUES ('bull', 2, 'persona')")
    with pytest.raises(psycopg.errors.UniqueViolation):
        q(db_url, "INSERT INTO agents (agent_key, version, kind) VALUES ('bull', 3, 'persona')")

    # A counterfactual portfolio without the proposal it came from is meaningless.
    with pytest.raises(psycopg.errors.CheckViolation):
        q(
            db_url,
            "INSERT INTO paper_portfolios (kind, agent_id, inception_date) "
            "VALUES ('counterfactual', %s, CURRENT_DATE)",
            (ids["bear"],),
        )

    # A debate must record its point-in-time cutoff: "we forgot" and "there wasn't one" must differ.
    with pytest.raises(psycopg.errors.NotNullViolation):
        q(db_url, "INSERT INTO debates (scope, question) VALUES ('slate', 'allocate the book')")

    # A ticker debate needs a security; a slate debate must not have one.
    with pytest.raises(psycopg.errors.CheckViolation):
        q(db_url, "INSERT INTO debates (scope, question, context_as_of) VALUES ('ticker', 'q?', now())")
    with pytest.raises(psycopg.errors.CheckViolation):
        q(
            db_url,
            "INSERT INTO debates (scope, security_id, question, context_as_of) "
            "VALUES ('slate', %s, 'q?', now())",
            (ids["sec"],),
        )

    # S1/F-4: a portfolio credited to bear cannot be seeded by bull's proposal — the composite FK
    # ties (proposal, debate, agent) together.
    deb, prop = _seed_debate(db_url, ids)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        q(
            db_url,
            "INSERT INTO paper_portfolios (kind, agent_id, debate_id, proposal_id, inception_date) "
            "VALUES ('counterfactual', %s, %s, %s, CURRENT_DATE - 40)",
            (ids["bear"], deb, prop),
        )

    # S2: agents.kind is enforced at point of use — a persona cannot file a judgment, a judge
    # cannot file a proposal, and a blind book cannot belong to a persona.
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        q(
            db_url,
            "INSERT INTO judgments (debate_id, judge_agent_id, decision) VALUES (%s, %s, 'hold')",
            (deb, ids["bear"]),
        )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        q(
            db_url,
            "INSERT INTO agent_proposals (debate_id, agent_id, stance, rationale) "
            "VALUES (%s, %s, 'sell', 'a judge should not be here')",
            (deb, ids["judge_risk"]),
        )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        q(
            db_url,
            "INSERT INTO paper_portfolios (kind, agent_id, inception_date) "
            "VALUES ('blind', %s, CURRENT_DATE - 40)",
            (ids["bear"],),
        )


def test_debate_cascade_cannot_destroy_scored_history(db_url: str) -> None:
    """Fix-pass B-1: one DELETE FROM debates used to erase proposals, judgments, the portfolio,
    30 marks, and the metrics — the exact data the down migration declares unrecoverable. Scored
    debates now refuse deletion (RESTRICT); a failed debate with nothing scored still cleans up.
    """
    md = str(REPO_MIGRATIONS)
    assert main(["up", "--migrations-dir", md]) == EXIT_OK
    ids = _seed_agents(db_url)

    # A scored debate: portfolio + marks + metrics hang off it. Deletion is refused, loudly.
    deb, prop = _seed_debate(db_url, ids)
    _seed_scored_portfolio(db_url, ids, deb, prop)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        q(db_url, "DELETE FROM debates WHERE id = %s", (deb,))
    assert q(db_url, "SELECT count(*) FROM evaluation_runs")[0][0] == 1  # nothing was lost

    # A failed debate that produced only proposals and a judgment (no portfolio) cleans up in one
    # statement — that is the CASCADE that remains, and it is the intended cheap-cleanup path.
    deb2 = q(
        db_url,
        "INSERT INTO debates (scope, security_id, question, context_as_of, status, completed_at) "
        "VALUES ('ticker', %s, 'Buy more?', now() - interval '1 day', 'failed', now()) RETURNING id",
        (ids["sec"],),
    )[0][0]
    q(
        db_url,
        "INSERT INTO agent_proposals (debate_id, agent_id, stance, rationale) "
        "VALUES (%s, %s, 'buy', 'mid-run casualty')",
        (deb2, ids["bull"]),
    )
    q(
        db_url,
        "INSERT INTO judgments (debate_id, judge_agent_id, decision) VALUES (%s, %s, 'escalate')",
        (deb2, ids["judge_risk"]),
    )
    q(db_url, "DELETE FROM debates WHERE id = %s", (deb2,))
    assert q(db_url, "SELECT count(*) FROM agent_proposals WHERE debate_id = %s", (deb2,))[0][0] == 0
    assert q(db_url, "SELECT count(*) FROM judgments WHERE debate_id = %s", (deb2,))[0][0] == 0
    # The scored debate and its history are still fully intact.
    assert q(db_url, "SELECT count(*) FROM portfolio_returns_daily")[0][0] == 30


def test_portfolio_close_check_is_timezone_independent(db_url: str) -> None:
    """Fix-pass B-2: closed_at::date read the session TimeZone GUC, so the same row was accepted
    under Pacific/Kiritimati and rejected under UTC. The UTC-anchored constraint must reject a
    close 1h before inception's UTC midnight under ANY session TimeZone."""
    md = str(REPO_MIGRATIONS)
    assert main(["up", "--migrations-dir", md]) == EXIT_OK
    ids = _seed_agents(db_url)

    for tz in ("UTC", "Pacific/Kiritimati"):
        with psycopg.connect(db_url, autocommit=True) as conn:
            conn.execute(f"SET TimeZone = '{tz}'")
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO paper_portfolios (kind, agent_id, inception_date, closed_at) "
                    "VALUES ('blind', %s, DATE '2026-07-28', TIMESTAMPTZ '2026-07-27 23:00:00+00')",
                    (ids["blind"],),
                )
    # And a legitimate same-UTC-day close is accepted regardless of session TimeZone.
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("SET TimeZone = 'Pacific/Kiritimati'")
        conn.execute(
            "INSERT INTO paper_portfolios (kind, agent_id, inception_date, closed_at) "
            "VALUES ('blind', %s, DATE '2026-07-27', TIMESTAMPTZ '2026-07-27 23:00:00+00')",
            (ids["blind"],),
        )


def test_evaluation_ratio_parameters_are_recorded(db_url: str) -> None:
    """Fix-pass B1: the same nine returns store as Sharpe 7.20, 7.07, 1.57, or 0.45 depending on
    rf/annualisation conventions — so the row must carry them, and the rf must have a point-in-time
    source table rather than a config constant."""
    md = str(REPO_MIGRATIONS)
    assert main(["up", "--migrations-dir", md]) == EXIT_OK
    ids = _seed_agents(db_url)
    pid = q(
        db_url,
        "INSERT INTO paper_portfolios (kind, agent_id, inception_date) "
        "VALUES ('blind', %s, CURRENT_DATE - 40) RETURNING id",
        (ids["blind"],),
    )[0][0]
    _mark_daily(db_url, pid, days=30)

    # risk_free_annual is NOT NULL: a run that does not state its rf convention is unstorable.
    with pytest.raises(psycopg.errors.NotNullViolation):
        q(
            db_url,
            "INSERT INTO evaluation_runs (portfolio_id, window_start, window_end, n_observations, "
            "min_n_for_ranking, sharpe, inputs_as_of) "
            "VALUES (%s, CURRENT_DATE - 30, CURRENT_DATE, 30, 21, 1.42, now())",
            (pid,),
        )

    # risk_free_rates is point-in-time: a revision (new known_at) COEXISTS with the original.
    q(
        db_url,
        "INSERT INTO risk_free_rates (series, effective_date, annual_rate, known_at) "
        "VALUES ('DGS3MO', '2026-07-01', 0.0525, '2026-07-01T12:00:00Z'), "
        "       ('DGS3MO', '2026-07-01', 0.0530, '2026-07-08T12:00:00Z')",
    )
    assert q(db_url, "SELECT count(*) FROM risk_free_rates")[0][0] == 2
    # A rate known before its effective date is a data error (UTC-anchored, 003's idiom).
    with pytest.raises(psycopg.errors.CheckViolation):
        q(
            db_url,
            "INSERT INTO risk_free_rates (series, effective_date, annual_rate, known_at) "
            "VALUES ('DGS3MO', '2026-07-02', 0.05, '2026-07-01T12:00:00Z')",
        )


def test_judgment_outcome_join_path(db_url: str) -> None:
    """Fix-pass B2: §3.2 (a judge reviews its own prior outcomes) needs an unambiguous join from a
    judgment to what it chose and what that produced. Before, judgments recorded neither."""
    md = str(REPO_MIGRATIONS)
    assert main(["up", "--migrations-dir", md]) == EXIT_OK
    ids = _seed_agents(db_url)
    deb, prop = _seed_debate(db_url, ids)
    _seed_scored_portfolio(db_url, ids, deb, prop)

    # A directional ruling must name the proposal it backed.
    with pytest.raises(psycopg.errors.CheckViolation):
        q(
            db_url,
            "INSERT INTO judgments (debate_id, judge_agent_id, decision) VALUES (%s, %s, 'buy')",
            (deb, ids["judge_risk"]),
        )
    # …and that proposal must belong to THIS debate (composite FK).
    deb2 = q(
        db_url,
        "INSERT INTO debates (scope, security_id, question, context_as_of) "
        "VALUES ('ticker', %s, 'Again?', now() - interval '1 hour') RETURNING id",
        (ids["sec"],),
    )[0][0]
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        q(
            db_url,
            "INSERT INTO judgments (debate_id, judge_agent_id, decision, chosen_proposal_id) "
            "VALUES (%s, %s, 'buy', %s)",
            (deb2, ids["judge_risk"], prop),
        )

    # The honest path: ruling backs the bull's proposal; the real account's book records the result.
    jid = q(
        db_url,
        "INSERT INTO judgments (debate_id, judge_agent_id, decision, chosen_proposal_id) "
        "VALUES (%s, %s, 'buy', %s) RETURNING id",
        (deb, ids["judge_risk"], prop),
    )[0][0]
    real_pid = q(
        db_url,
        "INSERT INTO paper_portfolios (kind, agent_id, inception_date) "
        "VALUES ('real', %s, CURRENT_DATE - 40) RETURNING id",
        (ids["real"],),
    )[0][0]
    q(db_url, "UPDATE judgments SET resulting_portfolio_id = %s WHERE id = %s", (real_pid, jid))

    # judgment → chosen proposal → counterfactual → metrics: exactly one row, no ambiguity.
    rows = q(
        db_url,
        "SELECT er.sharpe FROM judgments j "
        "JOIN paper_portfolios pp ON pp.proposal_id = j.chosen_proposal_id "
        "JOIN evaluation_runs er ON er.portfolio_id = pp.id WHERE j.id = %s",
        (jid,),
    )
    assert rows == [(Decimal("1.420000"),)]
    # judgment → the book the ruling actually produced.
    assert q(
        db_url,
        "SELECT pp.kind FROM judgments j JOIN paper_portfolios pp ON pp.id = j.resulting_portfolio_id "
        "WHERE j.id = %s",
        (jid,),
    ) == [("real",)]


def test_proposal_weight_sum_and_holdings(db_url: str) -> None:
    """Fix-pass B3: a persona could propose a 300% book in a cash account and its counterfactual
    would be marked as if that were real. The deferred trigger caps the SUM at 100; the holdings
    table makes market_value recomputable instead of trusted."""
    md = str(REPO_MIGRATIONS)
    assert main(["up", "--migrations-dir", md]) == EXIT_OK
    ids = _seed_agents(db_url)
    deb, prop = _seed_debate(db_url, ids)
    for sym in ("MU", "AMD"):
        q(db_url, "INSERT INTO securities (symbol) VALUES (%s)", (sym,))

    q(
        db_url,
        "INSERT INTO agent_proposal_positions (proposal_id, security_id, target_weight_pct) "
        "SELECT %s, id, 50.0 FROM securities WHERE symbol IN ('NVDA', 'MU')",
        (prop,),
    )
    # The third 50% breaches the account: the trigger names the proposal and the offending sum.
    with pytest.raises(psycopg.errors.CheckViolation, match="sum to 150"):
        q(
            db_url,
            "INSERT INTO agent_proposal_positions (proposal_id, security_id, target_weight_pct) "
            "SELECT %s, id, 50.0 FROM securities WHERE symbol = 'AMD'",
            (prop,),
        )
    assert q(db_url, "SELECT count(*) FROM agent_proposal_positions")[0][0] == 2

    # Holdings: a lot is auditable (entry recorded), and a half-recorded exit is rejected.
    pid = q(
        db_url,
        "INSERT INTO paper_portfolios (kind, agent_id, debate_id, proposal_id, inception_date) "
        "VALUES ('counterfactual', %s, %s, %s, CURRENT_DATE - 40) RETURNING id",
        (ids["bull"], deb, prop),
    )[0][0]
    q(
        db_url,
        "INSERT INTO paper_portfolio_positions (portfolio_id, security_id, entry_date, shares, entry_price) "
        "VALUES (%s, %s, CURRENT_DATE - 40, 100.5, 123.45)",
        (pid, ids["sec"]),
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        q(
            db_url,
            "UPDATE paper_portfolio_positions SET exit_price = 150.0 WHERE portfolio_id = %s",
            (pid,),
        )


def test_lookahead_bounds_enforced(db_url: str) -> None:
    """Fix-pass B4: all four lookahead shapes review inserted live are now unrepresentable —
    future-priced marks, pre-inception returns, a context cutoff after the debate started, and a
    counterfactual backdated past its debate's cutoff."""
    md = str(REPO_MIGRATIONS)
    assert main(["up", "--migrations-dir", md]) == EXIT_OK
    ids = _seed_agents(db_url)
    deb, prop = _seed_debate(db_url, ids)
    pid = q(
        db_url,
        "INSERT INTO paper_portfolios (kind, agent_id, debate_id, proposal_id, inception_date) "
        "VALUES ('counterfactual', %s, %s, %s, CURRENT_DATE - 40) RETURNING id",
        (ids["bull"], deb, prop),
    )[0][0]

    # 4a: a mark priced from the future is a forecast wearing a Sharpe.
    with pytest.raises(psycopg.errors.CheckViolation):
        q(
            db_url,
            "INSERT INTO portfolio_returns_daily (portfolio_id, trade_date, market_value, daily_return, priced_as_of) "
            "VALUES (%s, CURRENT_DATE - 10, 150000, 0.35, now() + interval '1 year')",
            (pid,),
        )
    # 4b: a return row before the portfolio existed is manufactured history.
    with pytest.raises(psycopg.errors.CheckViolation, match="predates its inception"):
        q(
            db_url,
            "INSERT INTO portfolio_returns_daily (portfolio_id, trade_date, market_value, daily_return, priced_as_of) "
            "VALUES (%s, CURRENT_DATE - 60, 100000, 0.02, "
            "((CURRENT_DATE - 60)::timestamp AT TIME ZONE 'UTC') + interval '21 hours')",
            (pid,),
        )
    # 4c: a debate whose context cutoff postdates its start read its own future.
    with pytest.raises(psycopg.errors.CheckViolation):
        q(
            db_url,
            "INSERT INTO debates (scope, security_id, question, context_as_of, started_at) "
            "VALUES ('ticker', %s, 'q?', now() + interval '1252 days', now())",
            (ids["sec"],),
        )
    # 4d: a counterfactual incepted before its debate's cutoff is a backdated track record.
    with pytest.raises(psycopg.errors.CheckViolation, match="context cutoff"):
        q(
            db_url,
            "UPDATE paper_portfolios SET inception_date = DATE '2020-01-01' WHERE id = %s",
            (pid,),
        )


def test_reward_needs_weights_and_guardrails_have_a_home(db_url: str) -> None:
    """Fix-pass B5: a reward stored with '{}' (the old DEFAULT) or garbage weights is
    unobservable, and §5's guardrail_breach_penalty finally has a data source with enforced
    override provenance."""
    md = str(REPO_MIGRATIONS)
    assert main(["up", "--migrations-dir", md]) == EXIT_OK
    ids = _seed_agents(db_url)
    pid = q(
        db_url,
        "INSERT INTO paper_portfolios (kind, agent_id, inception_date) "
        "VALUES ('blind', %s, CURRENT_DATE - 40) RETURNING id",
        (ids["blind"],),
    )[0][0]
    _mark_daily(db_url, pid, days=30)

    base = (
        "INSERT INTO evaluation_runs (portfolio_id, window_start, window_end, n_observations, "
        "min_n_for_ranking, risk_free_annual, reward_total, reward_weights, inputs_as_of) "
        "VALUES (%s, CURRENT_DATE - 30, CURRENT_DATE, 30, 21, 0.05, 3.912, %s::jsonb, now())"
    )
    with pytest.raises(psycopg.errors.CheckViolation):  # reward with NO weights (the old default)
        q(db_url, base, (pid, "{}"))
    with pytest.raises(psycopg.errors.CheckViolation):  # reward with garbage weights
        q(db_url, base, (pid, '{"w_sharpe": "banana", "totally_unrelated": [1, 2, 3]}'))
    q(db_url, base, (pid, '{"w_sortino": 0.4, "w_sharpe": 0.3, "w_dd": 0.2, "w_breach": 0.1}'))
    assert q(db_url, "SELECT count(*) FROM evaluation_runs")[0][0] == 1

    # guardrail_events: the w_breach data source. An override must say who overrode it.
    with pytest.raises(psycopg.errors.CheckViolation):
        q(
            db_url,
            "INSERT INTO guardrail_events (portfolio_id, rule_key, severity, threshold, observed, action_taken) "
            "VALUES (%s, 'cash_floor', 'block', 10.0, 6.5, 'overridden')",
            (pid,),
        )
    q(
        db_url,
        "INSERT INTO guardrail_events (portfolio_id, rule_key, severity, threshold, observed, "
        "action_taken, override_by, override_reason) "
        "VALUES (%s, 'cash_floor', 'block', 10.0, 6.5, 'overridden', 'jared', 'deliberate add-on buy')",
        (pid,),
    )
    assert q(db_url, "SELECT count(*) FROM guardrail_events WHERE rule_key = 'cash_floor'")[0][0] == 1


def test_n_observations_is_verified(db_url: str) -> None:
    """Fix-pass B6: n_observations was asserted, never verified — review stored n=5000 against 9
    actual marks. The trigger checks the claim against the mark count; the supporting metrics get
    the same n >= 2 arithmetic gate the ratios always had."""
    md = str(REPO_MIGRATIONS)
    assert main(["up", "--migrations-dir", md]) == EXIT_OK
    ids = _seed_agents(db_url)
    pid = q(
        db_url,
        "INSERT INTO paper_portfolios (kind, agent_id, inception_date) "
        "VALUES ('blind', %s, CURRENT_DATE - 40) RETURNING id",
        (ids["blind"],),
    )[0][0]
    _mark_daily(db_url, pid, days=9)

    # An absurd claim (n=5000 in a 31-day window) dies at the arithmetic CHECK; a PLAUSIBLE false
    # claim — 29 against 9 actual marks, the off-by-N marking bug shape — needs the count trigger.
    with pytest.raises(psycopg.errors.CheckViolation):
        q(
            db_url,
            "INSERT INTO evaluation_runs (portfolio_id, window_start, window_end, n_observations, "
            "min_n_for_ranking, risk_free_annual, inputs_as_of) "
            "VALUES (%s, CURRENT_DATE - 30, CURRENT_DATE, 5000, 21, 0.05, now())",
            (pid,),
        )
    with pytest.raises(psycopg.errors.CheckViolation, match="claims n_observations"):
        q(
            db_url,
            "INSERT INTO evaluation_runs (portfolio_id, window_start, window_end, n_observations, "
            "min_n_for_ranking, risk_free_annual, inputs_as_of) "
            "VALUES (%s, CURRENT_DATE - 30, CURRENT_DATE, 29, 21, 0.05, now())",
            (pid,),
        )
    # hit_rate at n=1 is a coin flip wearing four decimals — gated like the ratios.
    with pytest.raises(psycopg.errors.CheckViolation):
        q(
            db_url,
            "INSERT INTO evaluation_runs (portfolio_id, window_start, window_end, n_observations, "
            "min_n_for_ranking, risk_free_annual, hit_rate, inputs_as_of) "
            "VALUES (%s, CURRENT_DATE, CURRENT_DATE, 1, 21, 0.05, 1.0, now())",
            (pid,),
        )
    # The truthful claim stores.
    q(
        db_url,
        "INSERT INTO evaluation_runs (portfolio_id, window_start, window_end, n_observations, "
        "min_n_for_ranking, risk_free_annual, inputs_as_of) "
        "VALUES (%s, CURRENT_DATE - 30, CURRENT_DATE, 9, 21, 0.05, now())",
        (pid,),
    )
    assert q(db_url, "SELECT count(*) FROM evaluation_runs")[0][0] == 1


def test_walk_forward_split_is_labelled(db_url: str) -> None:
    """Fix-pass B8: a score computed on fitted data must never be readable as an honest one —
    split/experiment/fold make §3.5's train/test boundary representable, defaulting to 'live'."""
    md = str(REPO_MIGRATIONS)
    assert main(["up", "--migrations-dir", md]) == EXIT_OK
    ids = _seed_agents(db_url)
    pid = q(
        db_url,
        "INSERT INTO paper_portfolios (kind, agent_id, inception_date) "
        "VALUES ('blind', %s, CURRENT_DATE - 40) RETURNING id",
        (ids["blind"],),
    )[0][0]
    _mark_daily(db_url, pid, days=30)

    base = (
        "INSERT INTO evaluation_runs (portfolio_id, window_start, window_end, n_observations, "
        "min_n_for_ranking, risk_free_annual, inputs_as_of{cols}) "
        "VALUES (%s, CURRENT_DATE - 30, CURRENT_DATE, 30, 21, 0.05, now(){vals})"
    )
    with pytest.raises(psycopg.errors.CheckViolation):  # unknown split label
        q(db_url, base.format(cols=", split", vals=", 'vibes'"), (pid,))
    with pytest.raises(psycopg.errors.CheckViolation):  # a fold outside an experiment is meaningless
        q(db_url, base.format(cols=", fold_index", vals=", 3"), (pid,))
    q(db_url, base.format(cols="", vals=""), (pid,))
    assert q(db_url, "SELECT split FROM evaluation_runs")[0][0] == "live"
    q(db_url, base.format(cols=", split, experiment_id, fold_index", vals=", 'test', 7, 3"), (pid,))
    assert q(db_url, "SELECT count(*) FROM evaluation_runs WHERE split = 'test'")[0][0] == 1


def test_blind_and_personas_are_comparable_books(db_url: str) -> None:
    """Fix-pass B9 (+F-3, F-10): the blind control and each persona now have the same kind of
    object to compare — a standing book — and standing books cannot carry debate machinery or
    silently multiply. Correction pass: strategy_mode is now TIED to kind by
    ck_paper_portfolios_strategy_kind, so "composites are marked 'rebalanced'" is a constraint
    rather than a comment."""
    md = str(REPO_MIGRATIONS)
    assert main(["up", "--migrations-dir", md]) == EXIT_OK
    ids = _seed_agents(db_url)
    deb, prop = _seed_debate(db_url, ids)

    # F-3: a blind book carrying debate machinery is a modelling error, not a control.
    with pytest.raises(psycopg.errors.CheckViolation):
        q(
            db_url,
            "INSERT INTO paper_portfolios (kind, agent_id, debate_id, proposal_id, inception_date) "
            "VALUES ('blind', %s, %s, %s, CURRENT_DATE - 40)",
            (ids["blind"], deb, prop),
        )

    # B9: each persona gets ONE standing composite book — the object §3.4 compares to the blind.
    q(
        db_url,
        "INSERT INTO paper_portfolios (kind, agent_id, inception_date, strategy_mode) "
        "VALUES ('agent_composite', %s, CURRENT_DATE - 40, 'rebalanced')",
        (ids["bull"],),
    )
    with pytest.raises(psycopg.errors.UniqueViolation):  # a second open composite for bull
        q(
            db_url,
            "INSERT INTO paper_portfolios (kind, agent_id, inception_date, strategy_mode) "
            "VALUES ('agent_composite', %s, CURRENT_DATE - 40, 'rebalanced')",
            (ids["bull"],),
        )
    with pytest.raises(psycopg.errors.CheckViolation):  # strategy_mode is a closed vocabulary
        q(
            db_url,
            "INSERT INTO paper_portfolios (kind, agent_id, inception_date, strategy_mode) "
            "VALUES ('agent_composite', %s, CURRENT_DATE - 40, 'yolo')",
            (ids["bear"],),
        )

    # Correction pass (re-review SHOULD-FIX-3): the column default is 'buy_and_hold', so a
    # composite inserted WITHOUT an explicit mode — the exact incomparable object the re-review
    # stored next to a rebalanced blind — must be refused, and by the strategy/kind tie
    # specifically (the match pins the constraint; every other column here is valid).
    with pytest.raises(psycopg.errors.CheckViolation, match="strategy_kind"):
        q(
            db_url,
            "INSERT INTO paper_portfolios (kind, agent_id, inception_date) "
            "VALUES ('agent_composite', %s, CURRENT_DATE - 40)",
            (ids["bear"],),
        )
    # And the other direction: a v1 counterfactual sleeve claiming to rebalance is refused.
    with pytest.raises(psycopg.errors.CheckViolation, match="strategy_kind"):
        q(
            db_url,
            "INSERT INTO paper_portfolios (kind, agent_id, debate_id, proposal_id, "
            "inception_date, strategy_mode) "
            "VALUES ('counterfactual', %s, %s, %s, CURRENT_DATE - 40, 'rebalanced')",
            (ids["bull"], deb, prop),
        )
    # A counterfactual at the default remains legal — the default and the tie agree for sleeves.
    q(
        db_url,
        "INSERT INTO paper_portfolios (kind, agent_id, debate_id, proposal_id, inception_date) "
        "VALUES ('counterfactual', %s, %s, %s, CURRENT_DATE - 40)",
        (ids["bull"], deb, prop),
    )

    # F-10: at most one OPEN real (and blind) book — two would corrupt the leaderboard.
    q(
        db_url,
        "INSERT INTO paper_portfolios (kind, agent_id, inception_date) "
        "VALUES ('real', %s, CURRENT_DATE - 40)",
        (ids["real"],),
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        q(
            db_url,
            "INSERT INTO paper_portfolios (kind, agent_id, inception_date) "
            "VALUES ('real', %s, CURRENT_DATE - 40)",
            (ids["real"],),
        )
    # Closing the open book makes room for a successor — the singleton is per OPEN book.
    q(db_url, "UPDATE paper_portfolios SET closed_at = now() WHERE kind = 'real'")
    q(
        db_url,
        "INSERT INTO paper_portfolios (kind, agent_id, inception_date) "
        "VALUES ('real', %s, CURRENT_DATE - 40)",
        (ids["real"],),
    )
