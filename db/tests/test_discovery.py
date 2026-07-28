"""Discovery, filename-based destructive classification, byte-level rejection, the best-effort
keyword sniff (including its documented holes), --target validation, and the CLI exit-code
contract — no database required.

Destructiveness lives in the FILENAME (ADR-002): `NNN_name.destructive.up.sql` /
`NNN_name.destructive.down.sql`. Nothing inside a file can influence its classification, so the
directive-forgery class that survived three verification rounds has no code to attack.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from migrate import (
    EXIT_CONNECTION,
    EXIT_OK,
    EXIT_VALIDATION,
    FILENAME_RE,
    MigrationError,
    MissingPair,
    UnmarkedDestructiveSql,
    discover_migrations,
    main,
    validate_target,
)


def write_pair(
    d: Path,
    version: str,
    name: str,
    up: str = "SELECT 1;",
    down: str = "SELECT 2;",
    *,
    up_destructive: bool = False,
    down_destructive: bool = False,
) -> None:
    up_mark = ".destructive" if up_destructive else ""
    down_mark = ".destructive" if down_destructive else ""
    (d / f"{version}_{name}{up_mark}.up.sql").write_text(up, encoding="utf-8")
    (d / f"{version}_{name}{down_mark}.down.sql").write_text(down, encoding="utf-8")


# ── discovery taxonomy ────────────────────────────────────────────────────────────────────────


def test_discovers_in_order_with_cached_text_and_checksum(tmp_path: Path) -> None:
    write_pair(tmp_path, "002", "second")
    write_pair(tmp_path, "001", "first", up="CREATE TABLE a (i int);")
    migs = discover_migrations(tmp_path)
    assert [(m.version, m.name) for m in migs] == [("001", "first"), ("002", "second")]
    assert migs[0].up_sql == "CREATE TABLE a (i int);"
    assert len(migs[0].checksum) == 64

    # SF-4 regression: the text is read ONCE at discovery. A file edited afterwards must not
    # change what the runner validates, checksums, or executes.
    (tmp_path / "001_first.up.sql").write_text("SELECT 3;", encoding="utf-8")
    assert migs[0].up_sql == "CREATE TABLE a (i int);"


def test_missing_partner_is_a_hard_error(tmp_path: Path) -> None:
    (tmp_path / "001_x.up.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MissingPair):
        discover_migrations(tmp_path)


def test_bad_filename_rejected(tmp_path: Path) -> None:
    write_pair(tmp_path, "001", "ok")
    (tmp_path / "01_short.up.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError):
        discover_migrations(tmp_path)


@pytest.mark.parametrize(
    "filename",
    [
        "002_extra.up.SQL",  # R4-S2: uppercase extension — was silently skipped, `up` exited 0
        "002_extra.UP.sql",  # uppercase direction
        "002_extra.up.sql ",  # trailing space
        "002_extra.up.sql.",  # trailing dot
        "002_extra.up.sql\n",  # trailing newline (also exercises FILENAME_RE's \Z anchor)
        "002_extra.txt",  # version-prefixed stray: plausibly an intended migration
    ],
)
def test_near_miss_migration_files_are_refused_not_skipped(tmp_path: Path, filename: str) -> None:
    """Round 4 R4-S2: a `.suffix != '.sql'` check silently dropped these, so `up` printed
    'no pending migrations' and exited 0 while the migration never ran — success reported, schema
    change lost, and `status` would never mention it. Anything version-prefixed or with a
    .sql-like extension must refuse discovery loudly. Rejection (not case-insensitive adoption)
    is deliberate: the grammar is all-lowercase everywhere else, and adopting `.SQL` would invite
    `002_x.up.sql` + `002_x.up.SQL` ambiguity."""
    write_pair(tmp_path, "001", "ok")
    (tmp_path / filename).write_text("CREATE TABLE t (i int);", encoding="utf-8")
    with pytest.raises(MigrationError, match="Refusing to skip"):
        discover_migrations(tmp_path)


def test_uppercase_sql_extension_fails_the_run_loudly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The end-to-end shape of R4-S2: `up` must exit 1 at discovery, not print 'no pending
    migrations' and exit 0. No connection config is set, so exit 1 (not 3) proves discovery
    refused before any connect attempt."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("PGHOST", raising=False)
    write_pair(tmp_path, "001", "ok")
    (tmp_path / "002_extra.up.SQL").write_text("CREATE TABLE t (i int);", encoding="utf-8")
    (tmp_path / "002_extra.down.SQL").write_text("SELECT 1;", encoding="utf-8")
    assert main(["up", "--migrations-dir", str(tmp_path)]) == EXIT_VALIDATION


def test_unrelated_files_and_directories_are_still_ignored(tmp_path: Path) -> None:
    """The loud near-miss check must not turn discovery paranoid: entries that could not be an
    intended migration (docs, dotfiles, subdirectories) stay ignored."""
    write_pair(tmp_path, "001", "ok")
    (tmp_path / "README.md").write_text("notes\n", encoding="utf-8")
    (tmp_path / ".gitkeep").write_text("", encoding="utf-8")
    (tmp_path / "archive").mkdir()
    (mig,) = discover_migrations(tmp_path)
    assert mig.version == "001"


def test_directory_named_like_a_migration_is_refused(tmp_path: Path) -> None:
    """A directory (or dangling symlink) whose NAME matches the grammar would previously be
    silently skipped by the is_file() guard — the same silent-success failure mode as R4-S2."""
    write_pair(tmp_path, "001", "ok")
    (tmp_path / "002_fake.up.sql").mkdir()
    with pytest.raises(MigrationError, match="not a regular file"):
        discover_migrations(tmp_path)


def test_filename_re_rejects_trailing_newline() -> None:
    """Round 4 NIT-1: `$` in Python also matches before a trailing newline, so the grammar
    accepted '001_x.up.sql\\n'. The anchor must be \\Z, which matches only at the true end."""
    assert FILENAME_RE.match("001_x.up.sql\n") is None
    assert FILENAME_RE.match("001_x.up.sql") is not None
    assert FILENAME_RE.match("001_x.destructive.down.sql") is not None


def test_same_version_two_names_rejected(tmp_path: Path) -> None:
    (tmp_path / "001_alpha.up.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "001_beta.down.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match="two different names"):
        discover_migrations(tmp_path)


def test_mixed_version_widths_rejected(tmp_path: Path) -> None:
    """SF-5 regression: sorted(["1000","999"]) is lexically wrong; discovery must refuse."""
    write_pair(tmp_path, "999", "old")
    write_pair(tmp_path, "1000", "new")
    with pytest.raises(MigrationError, match="mixed version widths"):
        discover_migrations(tmp_path)


def test_missing_directory_is_a_validation_error(tmp_path: Path) -> None:
    with pytest.raises(MigrationError):
        discover_migrations(tmp_path / "nope")


# ── filename classification: the single source of destructiveness ─────────────────────────────


def test_destructive_marker_parsed_per_direction(tmp_path: Path) -> None:
    write_pair(tmp_path, "001", "both", down_destructive=True, up_destructive=True)
    write_pair(tmp_path, "002", "down_only", down_destructive=True)
    write_pair(tmp_path, "003", "neither")
    migs = {m.name: m for m in discover_migrations(tmp_path)}
    assert (migs["both"].up_destructive, migs["both"].down_destructive) == (True, True)
    assert (migs["down_only"].up_destructive, migs["down_only"].down_destructive) == (False, True)
    assert (migs["neither"].up_destructive, migs["neither"].down_destructive) == (False, False)
    # The marker is not part of the migration's identity.
    assert migs["down_only"].version == "002"
    assert migs["down_only"].name == "down_only"


@pytest.mark.parametrize(
    "filename",
    [
        "001_x.destructiv.up.sql",  # misspelled marker
        "001_x.destructivee.up.sql",
        "001_x.Destructive.up.sql",  # wrong case — the grammar is all-lowercase
        "001_x.up.destructive.sql",  # marker in the wrong position
        "001_x.destructive.destructive.up.sql",  # doubled marker
        "001_x.destructive.sql",  # marker but no direction
    ],
)
def test_malformed_marker_spellings_rejected_loudly(tmp_path: Path, filename: str) -> None:
    """The grammar has exactly one marker spelling in exactly one position; anything close-but-
    wrong must fail discovery rather than silently classify as non-destructive."""
    write_pair(tmp_path, "001", "x")
    (tmp_path / filename).write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError):
        discover_migrations(tmp_path)


def test_name_containing_the_word_destructive_is_not_marked(tmp_path: Path) -> None:
    # The marker requires a literal '.', which the name charset excludes — no ambiguity.
    write_pair(tmp_path, "001", "destructive_cleanup")
    (mig,) = discover_migrations(tmp_path)
    assert mig.name == "destructive_cleanup"
    assert mig.up_destructive is False


def test_marked_and_unmarked_file_for_same_direction_is_a_duplicate(tmp_path: Path) -> None:
    write_pair(tmp_path, "001", "x")
    (tmp_path / "001_x.destructive.up.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match="duplicate up"):
        discover_migrations(tmp_path)


# ── byte-level rejection at discovery ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("direction", ["up", "down"])
def test_nul_byte_rejected(tmp_path: Path, direction: str) -> None:
    """Round-3 NEW2-S2: libpq truncates the query at the first NUL, so the server would execute
    only part of a file the runner checksums and records in full. Reject at discovery."""
    write_pair(tmp_path, "001", "x")
    (tmp_path / f"001_x.{direction}.sql").write_bytes(
        b"CREATE TABLE before_nul (i int);\n\x00\nCREATE TABLE after_nul (i int);\n"
    )
    with pytest.raises(MigrationError, match="NUL byte"):
        discover_migrations(tmp_path)


def test_utf8_bom_rejected(tmp_path: Path) -> None:
    write_pair(tmp_path, "001", "x")
    (tmp_path / "001_x.up.sql").write_bytes(b"\xef\xbb\xbfSELECT 1;")
    with pytest.raises(MigrationError, match="BOM"):
        discover_migrations(tmp_path)


def test_invalid_utf8_is_a_clean_migration_error(tmp_path: Path) -> None:
    """Round-3 NEW2-N3: a mis-encoded file must produce the module's one-line diagnostic (exit 1
    via main), not a UnicodeDecodeError traceback."""
    write_pair(tmp_path, "001", "x")
    (tmp_path / "001_x.up.sql").write_bytes(b"SELECT '\xff\xfe';")
    with pytest.raises(MigrationError, match="not valid UTF-8"):
        discover_migrations(tmp_path)


# ── the best-effort sniff: what it catches ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE t;",
        "drop table t;",
        "DROP\n    TABLE t;",  # keyword split across lines is still the keyword
        "DROP SCHEMA s CASCADE;",
        "DROP DATABASE d;",
        "TRUNCATE t;",
    ],
)
@pytest.mark.parametrize("direction", ["up", "down"])
def test_unmarked_destructive_keywords_refused(tmp_path: Path, sql: str, direction: str) -> None:
    kwargs = {"up": sql} if direction == "up" else {"down": sql}
    write_pair(tmp_path, "001", "x", **kwargs)
    with pytest.raises(UnmarkedDestructiveSql, match="not marked"):
        discover_migrations(tmp_path)


@pytest.mark.parametrize(
    "sql",
    [
        "-- this migration replaces the old DROP TABLE approach\nSELECT 1;",  # comment
        "INSERT INTO log VALUES ('ran DROP TABLE cleanup');",  # string literal
        "DO $$ BEGIN RAISE NOTICE 'TRUNCATE happens elsewhere'; END $$;",  # dollar-quoted body
    ],
)
def test_sniff_refuses_even_comments_and_literals_by_design(tmp_path: Path, sql: str) -> None:
    """PINNED ON PURPOSE: the sniff reads the RAW text. Stripping comments/literals first would
    mean re-growing the lexer whose forgeries caused this redesign. A false positive here costs
    one rename (or rewording); a false negative costs data."""
    write_pair(tmp_path, "001", "x", up=sql)
    with pytest.raises(UnmarkedDestructiveSql):
        discover_migrations(tmp_path)


@pytest.mark.parametrize(
    "sql",
    [
        "drop/**/table users;",  # R4-B1 #1: 4 characters defeated the whitespace-only sniff
        "DROP -- x\nTABLE users;",  # R4-B1 #2: line comment between the keywords
        "DROP/* a\nb */TABLE users;",  # R4-B1 #3: multi-line block comment between the keywords
        "drop /* x */ /* y */ table t;",  # several comments and whitespace mixed
        "DROP\n-- hidden\n-- more\nTABLE t;",  # stacked line comments
    ],
)
@pytest.mark.parametrize("direction", ["up", "down"])
def test_comment_separated_keywords_refused(tmp_path: Path, sql: str, direction: str) -> None:
    """Round 4 R4-B1: PostgreSQL's lexer treats a comment as a token separator, so every body
    here is a valid DROP TABLE — and each applied UNMARKED with exit 0 against the old
    `DROP\\s+TABLE` sniff. The separator alternation (_SEP) closes the comment shapes."""
    kwargs = {"up": sql} if direction == "up" else {"down": sql}
    write_pair(tmp_path, "001", "x", **kwargs)
    with pytest.raises(UnmarkedDestructiveSql, match="not marked"):
        discover_migrations(tmp_path)


@pytest.mark.parametrize(
    "sql",
    [
        "DROP OWNED BY rh_app;",  # R4-S1: cascades to every object the role owns
        "DROP MATERIALIZED VIEW mv;",  # R4-S1: a matview stores rows; dropping it loses them
    ],
)
def test_drop_owned_and_drop_materialized_view_refused(tmp_path: Path, sql: str) -> None:
    """Round 4 R4-S1: `001_core_schema` creates the `rh_app` role, so a future role-retirement
    migration writing DROP OWNED BY is the realistic accident shape — it applied unmarked with
    exit 0 before these keywords joined the sniff list."""
    write_pair(tmp_path, "001", "x", up=sql)
    with pytest.raises(UnmarkedDestructiveSql, match="not marked"):
        discover_migrations(tmp_path)


def test_sniff_error_names_the_exact_rename(tmp_path: Path) -> None:
    write_pair(tmp_path, "007", "cleanup", up="DROP TABLE old_junk;")
    with pytest.raises(UnmarkedDestructiveSql, match=r"007_cleanup\.destructive\.up\.sql"):
        discover_migrations(tmp_path)


def test_marked_file_with_destructive_sql_is_discovered(tmp_path: Path) -> None:
    write_pair(tmp_path, "001", "x", up="DROP TABLE t;", up_destructive=True)
    (mig,) = discover_migrations(tmp_path)
    assert mig.up_destructive is True


def test_marked_file_without_destructive_keywords_is_allowed(tmp_path: Path) -> None:
    # Over-marking is legal and safe: the author declares destructiveness the sniff cannot see
    # (mass DELETE FROM, DROP COLUMN) by renaming the file.
    write_pair(tmp_path, "001", "x", up="DELETE FROM t;", up_destructive=True)
    (mig,) = discover_migrations(tmp_path)
    assert mig.up_destructive is True


def test_drop_index_is_not_sniffed(tmp_path: Path) -> None:
    # Recreatable objects lose no rows; forcing the marker onto them would train people to
    # scatter it reflexively.
    write_pair(tmp_path, "001", "x", up="DROP INDEX IF EXISTS idx_old;")
    (mig,) = discover_migrations(tmp_path)
    assert mig.up_destructive is False


def test_truncated_word_does_not_trip_truncate(tmp_path: Path) -> None:
    write_pair(tmp_path, "001", "x", up="-- values are truncated to 2dp downstream\nSELECT 1;")
    (mig,) = discover_migrations(tmp_path)
    assert mig.up_destructive is False


def test_inert_directive_comment_does_not_classify(tmp_path: Path) -> None:
    """The old `-- migrate:` directives are dead. One claiming non-destructive above a DROP TABLE
    changes nothing: the unmarked filename still refuses the file."""
    write_pair(tmp_path, "001", "x", up="-- migrate: non-destructive\nDROP TABLE users;")
    with pytest.raises(UnmarkedDestructiveSql):
        discover_migrations(tmp_path)


# ── the best-effort sniff: what it deliberately does NOT catch ────────────────────────────────
# These tests pin the sniff's DOCUMENTED holes so the docs cannot drift back into overclaiming
# (rounds 1-4 each found prose asserting more than the code did). If one of these starts being
# refused, the sniff has grown toward a lexer again — re-read ADR-002's history first.


@pytest.mark.parametrize(
    "sql",
    [
        "DO $$ BEGIN EXECUTE 'DR' || 'OP TABLE users'; END $$;",  # R4-B1 #4: string concat
        "DO $$ BEGIN EXECUTE format('%s %s %I', 'DROP', 'TABLE', 'users'); END $$;",  # #5
        "DO $$ BEGIN EXECUTE 'TRUNC' || 'ATE users'; END $$;",  # #7
    ],
)
def test_dynamic_sql_is_invisible_to_the_sniff_as_documented(tmp_path: Path, sql: str) -> None:
    """A destructive statement built at runtime contains no keyword any text rule can see —
    deciding whether arbitrary SQL destroys data would require executing it. So an UNMARKED file
    carrying one discovers cleanly and is classified non-destructive: the filename says so, and
    nothing else gets a vote. The filename marker, set by the author, is the ONLY control for
    this shape — exactly what migrate.py, ADR-002, and TESTS.md now say."""
    write_pair(tmp_path, "001", "x", up=sql)
    (mig,) = discover_migrations(tmp_path)
    assert mig.up_destructive is False


def test_delete_from_is_not_sniffed_marker_is_the_control(tmp_path: Path) -> None:
    """The sniff has NEVER covered mass DELETE FROM (or DROP COLUMN) — documented from the start
    as the reason the explicit filename marker exists. Pinned so the hole stays documented rather
    than re-emerging as a surprise in a future review."""
    write_pair(tmp_path, "001", "x", up="DELETE FROM t WHERE true;")
    (mig,) = discover_migrations(tmp_path)
    assert mig.up_destructive is False


# ── no path worse than linear (round-3 NEW2-S3 was O(n²)) ─────────────────────────────────────


def test_discovery_is_fast_on_large_and_adversarial_files(tmp_path: Path) -> None:
    """The deleted scanner's identifier-backscan was quadratic: 40 kB of 'a$b$b$b…' took 73 s.
    Discovery is now regex + byte scans, all linear. 1.5 MB of the exact adversarial shape, a
    1.5 MB realistic body, and 2 MB of sniff-regex bait must discover in well under 5 s
    (measured ~0.1 s total)."""
    adversarial = "SELECT a" + "$b" * 750_000 + ";"  # ≥1.5 MB, the old worst case
    realistic = "INSERT INTO t VALUES (1);\n" * 60_000  # ≥1.5 MB of plain statements
    # ≥2 MB of sniff-prefix bait: the separator-tolerant sniff regex must fail fast at every
    # 'DROP' (nothing in the keyword list ever follows), not backtrack (measured ~0.08 s).
    sniff_bait = "DROP " * 400_000 + "x;"
    write_pair(tmp_path, "001", "adversarial", up=adversarial)
    write_pair(tmp_path, "002", "realistic", up=realistic)
    write_pair(tmp_path, "003", "sniff_bait", up=sniff_bait)
    t0 = time.monotonic()
    migs = discover_migrations(tmp_path)
    elapsed = time.monotonic() - t0
    assert len(migs) == 3
    assert elapsed < 5.0, f"discovery took {elapsed:.1f}s on 5 MB — a superlinear path is back"


# ── --target validation (SF-1) ────────────────────────────────────────────────────────────────


def test_validate_target_accepts_known_versions_and_down_sentinel(tmp_path: Path) -> None:
    write_pair(tmp_path, "001", "a")
    write_pair(tmp_path, "002", "b")
    migs = discover_migrations(tmp_path)
    assert validate_target("002", "up", migs) == "002"
    assert validate_target("000", "down", migs) == "000"
    assert validate_target(None, "up", migs) is None


@pytest.mark.parametrize(
    ("target", "command"),
    [
        ("2", "up"),  # unpadded: lexically compares wrong — previously over-applied silently
        ("1", "down"),  # unpadded: previously a silent no-op rollback
        ("000", "up"),  # sentinel only exists for down
        ("004", "up"),  # not a discovered version
        ("abc", "down"),
    ],
)
def test_validate_target_rejects_bad_targets(tmp_path: Path, target: str, command: str) -> None:
    write_pair(tmp_path, "001", "a")
    write_pair(tmp_path, "002", "b")
    migs = discover_migrations(tmp_path)
    with pytest.raises(MigrationError, match="invalid --target"):
        validate_target(target, command, migs)


def test_main_rejects_bad_target_before_connecting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No DATABASE_URL in the environment: reaching the connection step would exit 3, so exit 1
    # proves validation fired first.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("PGHOST", raising=False)
    write_pair(tmp_path, "001", "a")
    assert main(["up", "--migrations-dir", str(tmp_path), "--target", "2"]) == EXIT_VALIDATION


def test_main_maps_sniff_refusal_to_exit_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Discovery-time refusal, so no connection config is needed — and exit 1 (not 3) proves the
    # sniff fired before any connect attempt: the gate is evaluated even for --dry-run planning.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("PGHOST", raising=False)
    write_pair(tmp_path, "001", "x", up="DROP TABLE t;")
    assert main(["up", "--dry-run", "--migrations-dir", str(tmp_path)]) == EXIT_VALIDATION


# ── CLI exit-code contract (SF-6) ─────────────────────────────────────────────────────────────


def test_usage_error_exits_validation_not_sql(capsys: pytest.CaptureFixture[str]) -> None:
    """argparse's default exit code (2) collides with 'SQL execution failure'. A typo must be a
    validation failure (1), never the code that means 'go look at the database'."""
    assert main(["frobnicate"]) == EXIT_VALIDATION
    assert main([]) == EXIT_VALIDATION
    err = capsys.readouterr().err
    assert "error" in err


def test_help_exits_ok(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == EXIT_OK
    assert "migration runner" in capsys.readouterr().out


def test_no_connection_config_exits_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("PGHOST", raising=False)
    write_pair(tmp_path, "001", "a")
    assert main(["status", "--migrations-dir", str(tmp_path)]) == EXIT_CONNECTION
