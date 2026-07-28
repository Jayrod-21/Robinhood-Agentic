# Review: migration 004 — PostgreSQL correctness

Reviewer scope: `db/migrations/004_evaluation.up.sql`, `db/migrations/004_evaluation.destructive.down.sql`,
and the three 004 tests at the tail of `db/tests/test_runner_db.py`. Domain/statistics semantics are
owned by another reviewer; this review is Postgres correctness only. Every claim marked *(verified live)*
was reproduced against the running `rh-db` (PG16) inside a rolled-back transaction; the database was left
with 001–004 applied and all tables empty (confirmed post-review: `schema_migrations` = 4 rows, 0 data rows).

## Summary verdict: REQUEST CHANGES

Two blockers. First: the `ON DELETE CASCADE` chain from `debates` silently destroys the exact data the
framework declares unrecoverable — one `DELETE FROM debates` removed the proposal, the judgment, the
counterfactual portfolio, 30 daily marks, and the computed metrics, and fully de-linked the surviving
knowledge-base entry *(verified live)*. Second: `ck_paper_portfolios_closed` gives different answers under
different session `TimeZone` settings — the same row was accepted under `Pacific/Kiritimati` and rejected
under `UTC` *(verified live)* — the precise trap 003 documented and avoided in its own `known_at` check.
Beyond those, a cluster of real but non-blocking issues: a hole in the multi-branch shape CHECK, no
cross-parent consistency on counterfactual portfolios, `NUMERIC(12,8)` return columns that overflow at
values the charter's own target produces, several unindexed FKs, and mutable tables missing
`updated_at`/trigger.

## Bar checklist

| Bar §4 gate | Status | Notes |
|---|---|---|
| §4.1 TIMESTAMPTZ everywhere, never bare timestamp | PASS | All point-in-time columns are `TIMESTAMPTZ` |
| §4.1 NUMERIC not float for money/ratios | PASS | Throughout; but see F-5 on precision sizing |
| §4.1 CHECK constraints for domain invariants | PARTIAL | Explicit NULL-guards everywhere (good); F-2 (TZ-dependent), F-3 (shape hole), F-9 (−100% edge) |
| §4.1 Every FK explicit ON DELETE | PASS | All 15 FKs explicit; but see B-1 on the CASCADE choices |
| §4.1 Every FK indexed | FAIL | 6 unindexed FK columns, none documented as deviations (F-6); 002/003 documented theirs |
| §4.2 Named constraints/indexes, ix_/uq_/fk_/ck_ | PASS | Uniform; `fk_app_*`/`fk_prd_*` abbreviations are minor |
| §4.3 created_at/updated_at + trigger on mutable tables | FAIL | `agents`, `debates`, `paper_portfolios` are mutated in place with no `updated_at`/trigger (F-7) |
| §4.4 Indexes match stated query patterns | PARTIAL | Hot paths served *(verified with EXPLAIN)*; proposal→portfolio join is a seq scan (F-6) |
| §4.5 Tested down migration, correct drop order | PASS | Order verified against FK graph; `up→down→up` pinned in tests |
| ADR-002 filename convention | PASS | Up unmarked (creates only), down marked `.destructive` |
| TEXT + CHECK, not enum | PASS | `kind`, `scope`, `status`, `stance`, `decision`, `entry_type` |

## Findings

### BLOCKER
- **B-1** — `ON DELETE CASCADE` from `debates` destroys scored, unrecoverable evaluation history in one statement; contradicts the schema's own append-only design.
- **B-2** — `ck_paper_portfolios_closed` result depends on the client's session `TimeZone`; the same row passes or fails per connection.

### SHOULD-FIX
- **F-3** — `ck_paper_portfolios_shape`'s `blind` branch does not pin `debate_id`/`proposal_id` to NULL; a blind portfolio carrying both was accepted.
- **F-4** — No composite FK: a `counterfactual` portfolio can reference a proposal from a *different* debate and a different agent than its own columns claim.
- **F-5** — `NUMERIC(12,8)` caps `cumulative_return`/`annualized_return`/`total_return` at |v| < 10,000; the charter's own 20–40%/monthly target overflows it within ~2.5–5 years, and annualized short windows overflow trivially. `NUMERIC(12,6)` Sharpe/Sortino overflow at 10^6 (near-zero downside deviation).
- **F-6** — Unindexed FK columns: `paper_portfolios.debate_id`, `paper_portfolios.proposal_id`, `knowledge_base_entries.{debate_id, portfolio_id, evaluation_run_id}`, `agent_proposal_positions.security_id` (all seq-scan, verified); `portfolio_returns_daily.source_id` unindexed without the documentation 002/003 gave the same decision.
- **F-7** — `agents`, `debates`, `paper_portfolios` are mutable in place (`retired_at`, `status`/`completed_at`, `closed_at`) but have no `updated_at` column or trigger; only `knowledge_base_entries` got one.
- **F-8** — "Append-only" is policy, not mechanism: `rh_app` holds UPDATE and DELETE on `evaluation_runs`, `portfolio_returns_daily`, `agent_proposals`, `judgments` (verified). One `REVOKE` per table would make the claim enforced. The test named `test_evaluation_runs_are_append_only` does not (and cannot) test append-only-ness.
- **F-9** — `ck_prd_return` rejects `daily_return = -1` exactly, while `market_value` may be 0 and the comment claims only "below −100%" is impossible; a position marked to zero is unstorable.
- **F-10** — No open-portfolio singleton for `kind IN ('blind','real')`: two simultaneously open real-account books are representable, which would corrupt the leaderboard the `real` kind exists for.

### NIT
- **N-1** — `ck_debates_scope` is logically redundant with `ck_debates_scope_security` (harmless; better error messages).
- **N-2** — `debates`, `agent_proposals`, `judgments`, `paper_portfolios` lack `COMMENT ON TABLE`; 001–003 comment every table.
- **N-3** — `information_ratio` storable with `benchmark_symbol` NULL; `benchmark_symbol` has no grammar CHECK (001 checks symbol grammar) and no FK.
- **N-4** — `volatility` and `avg_win_loss` lack `>= 0` CHECKs; `n_observations` has no upper sanity bound vs the window length.
- **N-5** — `debates.status` and `completed_at` are untied: `status='complete'` with NULL `completed_at` (and the converse) are storable.
- **N-6** — Down file's `DROP TRIGGER IF EXISTS` line is unnecessary (dropping the table drops its triggers) — but it matches the 001–003 convention and is harmless on PG16 even when the table is absent (NOTICE + skip, verified).

### PRAISE
- **P-1** — `uq_agents_one_blind`/`uq_agents_one_real`: the constant-key partial unique index is the correct idiom and behaves exactly as intended — second live blind rejected, retire-then-replace accepted *(verified live)*. Do not "simplify" these away.
- **P-2** — Every nullable-column CHECK in 004 uses an explicit `IS NULL OR …` guard; I found **no** constraint that silently passes via three-valued NULL logic. That discipline is rare and worth protecting.
- **P-3** — `ck_evaluation_runs_sharpe_n`/`sortino_n` (ratio requires n ≥ 2) encodes an arithmetic impossibility at the boundary, not a style preference — and it is pinned by a test that fails if removed.
- **P-4** — `ix_evaluation_runs_portfolio (portfolio_id, window_end DESC, computed_at DESC)` serves "latest evaluation per portfolio" with a straight index scan + LIMIT *(verified with EXPLAIN)*; `ix_kb_as_of` likewise serves the as-of-T read.
- **P-5** — The leakage anchors (`debates.context_as_of`, `evaluation_runs.inputs_as_of`, `portfolio_returns_daily.priced_as_of`) are all NOT NULL — "we forgot the cutoff" is unrepresentable, which is the whole point.
- **P-6** — Filename convention per ADR-002 is exactly right: the up (pure CREATEs) is unmarked, the down is `.destructive`, and the up's `DROP TRIGGER IF EXISTS` doesn't trip the sniff (not a sniffed keyword).

## Detailed findings

### B-1 — CASCADE from `debates` silently destroys the evaluation history (data-integrity)

`004_evaluation.up.sql:126-127` (`fk_agent_proposals_debate … ON DELETE CASCADE`),
`:172-173` (`fk_judgments_debate … CASCADE`), `:204-207` (`fk_paper_portfolios_debate` and
`fk_paper_portfolios_proposal`, both CASCADE), `:242-243` (`fk_prd_portfolio … CASCADE`),
`:293-294` (`fk_evaluation_runs_portfolio … CASCADE`).

Verified live: with one debate, one proposal, one judgment, one counterfactual portfolio, 30 daily
return rows and one evaluation run in place, `DELETE FROM debates WHERE id = …` left **zero** rows in
`agent_proposals`, `judgments`, `paper_portfolios`, `portfolio_returns_daily`, and `evaluation_runs`.
The `knowledge_base_entries` row survived but with `debate_id`, `portfolio_id`, and
`evaluation_run_id` all nulled by `fk_kb_*` SET NULL (`:341-343`) — a lesson that can no longer say
what it is a lesson about.

This is exactly the data the down migration itself declares unrecoverable
(`004_evaluation.destructive.down.sql:4-5`: "NOT recoverable by replay — they depend on marks taken at
the time"). Yet the up migration lets a single unprivileged DML statement — no `--allow-destructive`
gate, no filename marker, nothing — do to one debate's history what the gated down migration does to
all of them. It also contradicts the schema's own stated philosophy: agents are "retired, never
deleted" (`:48-49`), `data_sources` is append-only via RESTRICT (001:63-67), `evaluation_runs` is
"append-only" (`:258-260`), and proposals are "the seed of every counterfactual track record"
(`:112-114`). The seed of the track record is the *most* CASCADE-exposed row in the schema.
`judgments` CASCADE additionally erases the judge's own history that `ix_judgments_agent_history`
(`:182-185`) exists to serve.

The likely intent — cheap cleanup of a failed/abandoned debate — does not need CASCADE below the
portfolio layer. Suggested matrix: keep `fk_agent_proposals_debate` and `fk_judgments_debate` CASCADE
if you want failed-debate cleanup (or make them RESTRICT too and clean up explicitly), but make
`fk_paper_portfolios_debate`, `fk_paper_portfolios_proposal`, `fk_prd_portfolio`, and
`fk_evaluation_runs_portfolio` RESTRICT. Then a debate that never produced a portfolio deletes
cleanly, and one that was ever scored refuses deletion — the same shape 001 chose for provenance.
(Per the trading-guardrail rule: RESTRICT here is loud and overridable — delete children explicitly
first — not a silent block.)

### B-2 — `ck_paper_portfolios_closed` shifts with the session TimeZone

`004_evaluation.up.sql:217`:
```sql
CONSTRAINT ck_paper_portfolios_closed CHECK (closed_at IS NULL OR closed_at::date >= inception_date)
```
`timestamptz::date` is not immutable — it reads the session `TimeZone` GUC at evaluation time.
Verified live: `closed_at = '2026-07-27 23:00:00+00'`, `inception_date = '2026-07-28'` was **accepted**
under `SET LOCAL TimeZone = 'Pacific/Kiritimati'` (UTC+14: local date 2026-07-28) and **rejected**
under `TimeZone = 'UTC'` (date 2026-07-27). Consequences: which rows are valid depends on the
connecting client's GUC, and a `pg_restore` performed under a different TimeZone can fail on rows the
live database accepted. 003 identified this exact trap and anchored its equivalent constraint
explicitly (`003_fundamentals.up.sql:96-99`: "a bare ::timestamptz cast reads the session TimeZone GUC
… which would shift the constraint boundary per client"). Fix to match:
`(closed_at AT TIME ZONE 'UTC')::date >= inception_date`. Note this constraint is also checked on
UPDATE (closing a portfolio is an UPDATE), so the exposure is ongoing, not just at insert.

### F-3 — Shape CHECK: the `blind` branch is under-constrained

`004_evaluation.up.sql:212-216`. Branch trace of `ck_paper_portfolios_shape`:
- `counterfactual` — requires agent, debate, proposal all present. Correct.
- `real` — requires agent present, debate and proposal absent. Correct.
- `blind` — requires only `agent_id IS NOT NULL`; `debate_id`/`proposal_id` are unconstrained.

Verified live: a `kind='blind'` portfolio carrying both a `debate_id` and a `proposal_id` was
accepted. The table comment (`:188-189`) groups blind with the real account ("plus the real account
and the blind agent") — a standing control book, not a per-debate artifact — so the blind branch
should read `(kind = 'blind' AND agent_id IS NOT NULL AND debate_id IS NULL AND proposal_id IS NULL)`.
As written, a writer bug attaching debate machinery to the control passes silently, and (post-B-1-fix
irony) such a row would also become deletable via the debate CASCADE. No combination that should pass
is rejected; `kind` and the referenced columns' NULL semantics are all handled explicitly, so the only
defect is this permissive branch.

### F-4 — Nothing ties a counterfactual portfolio's proposal to its own debate/agent

`004_evaluation.up.sql:202-207`. The three FKs are independent. Verified live: a `counterfactual`
row with `agent_id = bear`, `debate_id = debate-2`, `proposal_id = ` (bull's proposal in debate-1)
was accepted — the portfolio claims to be bear's counterfactual for debate 2 while actually seeded by
bull's proposal for debate 1. Every track record built by joining through this row is silently
mis-attributed. Standard fix: add `UNIQUE (id, debate_id, agent_id)` on `agent_proposals` (redundant
with the PK, exists purely as an FK target) and replace the single-column proposal FK with
`FOREIGN KEY (proposal_id, debate_id, agent_id) REFERENCES agent_proposals (id, debate_id, agent_id)`.
That makes the mismatch unrepresentable instead of a code-review hope. (`uq_agent_proposals`
(`:134`) already guarantees one proposal per (debate, agent), so this also uniquely determines the
proposal.)

### F-5 — Return-column precision overflows at values the charter targets

`004_evaluation.up.sql:234-235, 273, 276-278`. `NUMERIC(12,8)` admits |v| < 10^4 — a cumulative
return of +999,900%. The framework's stated objective (`:8`) is "20-40% monthly": 1.2^60 ≈ 56,347
(five years at the low end) and 1.4^28 ≈ 12,347 (~2.3 years at the high end) both overflow. Verified
live: `cumulative_return = 56346.51` → `numeric field overflow … must round to an absolute value less
than 10^4`. `annualized_return` is worse: even 26,800 (a strong week annualized) overflowed, and
annualizing any short hot window (1.1^252 ≈ 2.7×10^10) is far beyond range. The failure is at least
loud (insert raises), but it takes down the whole evaluation-run insert for a portfolio that merely
did very well — the exact rows the system most wants recorded. `sortino NUMERIC(12,6)` (`:272`)
similarly overflowed at 10^6, which a near-zero downside deviation reaches (the domain reviewer
should own the clamp-vs-NULL convention, but the column must not be the thing that decides).
Recommend `NUMERIC(20,8)` for the return columns (or bare `NUMERIC` — Postgres numeric is exact at
any scale; the CHECK constraints, not the typmod, are the real guards) and `NUMERIC(18,6)` for the
ratio columns. `daily_return NUMERIC(12,8)` is fine (a +999,900% *day* is not a real mark).

### F-6 — Unindexed foreign keys (Bar §4.1 P0), none documented as deviations

Verified with EXPLAIN against ~3,000-row ANALYZEd tables — all of these are seq scans:
- `paper_portfolios.debate_id` (`:204-205`): `uq_paper_portfolios_counterfactual (debate_id, agent_id) WHERE kind='counterfactual'` (`:220-221`) does **not** serve the FK's internal lookup — a partial index is only usable when the query provably implies its predicate, and the CASCADE/RESTRICT check `WHERE debate_id = $1` implies nothing about `kind`. Verified: plain `WHERE debate_id = 42` seq-scans.
- `paper_portfolios.proposal_id` (`:206-207`): also the join the stated query pattern "all proposals by this agent with their realized returns" needs (`ix_agent_proposals_agent` → **seq scan** on portfolios → returns PK). This one hits a named hot path.
- `knowledge_base_entries.debate_id`, `.portfolio_id`, `.evaluation_run_id` (`:341-343`): each parent DELETE fires a SET NULL lookup that full-scans the KB per deleted row.
- `agent_proposal_positions.security_id` (`:151-152`): also the natural "counterfactual exposure to name X" query.
- `portfolio_returns_daily.source_id` (`:244-245`): the append-only-parent rationale from 002/003 plausibly applies, but 002 (`:39-41`) and 003 (`:124-126`) each *documented* their choice; 004 is silent, and this table is closer to 003's "small enough that the index is cheap" case than to 1.6B bars.

If B-1 is fixed by flipping to RESTRICT, the indexes are still needed — RESTRICT enforcement performs
the same referencing-side lookup. Note the partial indexes that *do* exist are correctly usable where
intended: `WHERE security_id = $1` on `debates` implies `security_id IS NOT NULL`, so
`ix_debates_security` qualifies (planner confirmed it usable; it happened to prefer
`ix_debates_started` only because my synthetic data had a single security).

### F-7 — Mutable tables without `updated_at` + trigger (Bar §4.3)

- `agents` (`:25-56`): retirement is an in-place UPDATE of `retired_at` (`:48-49`), and `notes`/`display_name` are obvious correction targets — no `updated_at`, no trigger.
- `debates` (`:73-101`): `status` transitions `running → complete/failed/abandoned` and `completed_at` is set later — the table is *designed* to be updated — no `updated_at`, no trigger.
- `paper_portfolios` (`:190-218`): `closed_at` set on close — same.

001 and 003 attach the shared trigger to every mutable table and 002 explicitly documents its one
deviation (`002:56-59`); 004 attaches it only to `knowledge_base_entries` (`:356-359`) with no
comment justifying the other three. Either add the columns + triggers or document the deviation the
way 002 did.

### F-8 — "Append-only" is unenforced; the test of the same name doesn't test it

`evaluation_runs` is commented append-only (`:258-260`, `:311-313`) and `agent_proposals`/`judgments`/
`portfolio_returns_daily` are append-only in spirit (immutable observations). Verified:
`has_table_privilege('rh_app', …, 'UPDATE'/'DELETE')` is true for all four — 001's default privileges
(`001:53-54`) hand DML to the runtime role wholesale. A
`REVOKE UPDATE, DELETE ON evaluation_runs, portfolio_returns_daily, agent_proposals, judgments FROM rh_app`
in 004 would turn the policy into mechanism at zero cost (the migration role retains full access for
surgery). Relatedly, `test_evaluation_runs_are_append_only` (`test_runner_db.py:537-563`) asserts only
that two runs for the same (portfolio, window) can coexist — i.e. the *absence* of a uniqueness
constraint. It cannot fail if someone starts UPDATE-ing runs; the name promises more than the assert
delivers. Keep the coexistence assertion (it pins that no over-eager unique index gets added) but
either add the REVOKE and assert `has_table_privilege` is false, or rename the test to what it pins.

### F-9 — `ck_prd_return` bans exactly −100% while `market_value` 0 is legal

`004_evaluation.up.sql:246-248`: `ck_prd_value` allows `market_value = 0`, and the comment on
`ck_prd_return` says "A daily return below -100% is impossible" — but the predicate is
`daily_return > -1`, which bans −100% itself. Verified live: `(market_value=0, daily_return=-1)` →
CheckViolation. A 100%-concentrated counterfactual whose name goes to zero (delisting, fraud halt) is
a legitimate — and analytically important — observation that cannot be stored. Either the comment is
right and the predicate should be `>= -1`, or the domain rule really is "a book never marks to zero,"
in which case `ck_prd_value` should be `> 0` and the comment corrected. As written the pair is
internally inconsistent.

### F-10 — Two open `real` (or `blind`) books are representable

`uq_agents_one_real`/`one_blind` make the *agent* a singleton, but nothing constrains the
*portfolios*: two rows with `kind='real'`, both `closed_at IS NULL`, are accepted. The leaderboard
semantics ("the live account… on the same leaderboard", `:37`) assume one open real book. Mirror the
agent-level design: `CREATE UNIQUE INDEX uq_paper_portfolios_one_open ON paper_portfolios (kind)
WHERE kind IN ('blind','real') AND closed_at IS NULL` — same constant-key partial-unique idiom as
P-1. (Two singleton kinds in one predicate works because `kind` is the indexed key.)

### Down migration — correct (verified)

`004_evaluation.destructive.down.sql:10-20`. Drop order checked against the FK graph:
`knowledge_base_entries` (references debates, portfolios, eval runs, agents, securities) →
`evaluation_runs` → `portfolio_returns_daily` → `paper_portfolios` (references proposals, debates,
agents) → `agent_proposal_positions` → `agent_proposals` → `judgments` → `debates` → `agents`. Every
table is dropped before all of its referenced parents; 001–003 objects are untouched. 004 creates no
functions or types, and IDENTITY sequences are internal dependencies dropped with their tables, so
there is no residue — pinned end-to-end by `test_real_migrations_up_down_up`
(`test_runner_db.py:477-483`: full down to 000 then re-up, `schema_migrations` count = 4). The
`DROP TRIGGER` line (`:10`) is redundant (a table drop takes its triggers) but matches the 001–003
convention, and on PG16 `DROP TRIGGER IF EXISTS … ON missing_table` merely NOTICEs *(verified)*, so
it cannot strand a partial rollback. N-6 only.

### The tests — solid where they aim, with three gaps

`test_evaluation_schema_enforces_sample_size` (`:491-534`): both asserts are load-bearing — the
NotNullViolation fails if `n_observations` goes nullable, the CheckViolation fails if
`ck_evaluation_runs_sharpe_n` is dropped, and the closing positive insert guards against the
constraint being accidentally strengthened. Good test. (Minor: the sortino branch
`ck_evaluation_runs_sortino_n` is untested — a one-line parametrize.)

`test_evaluation_schema_shape_constraints` (`:566-593`): each assert maps to exactly one constraint
(`uq_agents_one_blind`, the counterfactual branch of `ck_paper_portfolios_shape`, `context_as_of`
NOT NULL, the ticker branch of `ck_debates_scope_security`) and each would fail if its constraint
were removed. Three gaps worth closing:
1. **The B-1 cascade is untested.** A test inserting a scored portfolio and deleting its debate would
   have surfaced the silent-destruction behavior — whichever way B-1 is resolved, pin the intended
   behavior.
2. The `blind`-branch hole (F-3) is untested — precisely the case a shape-constraint test exists to
   catch; the `slate`-with-security converse of the ticker assert is also untested.
3. `uq_agents_one_real` and `uq_agents_active` (several retired versions + one live — the interesting
   case) are untested; my live probe of retire-then-replace passed, but nothing pins it.

No test passes for a wrong reason except as noted for `test_evaluation_runs_are_append_only` (F-8).
All three use the per-test throwaway database fixture (`test_runner_db.py:53-64`) — properly isolated,
never touching rh-db.

## Coordination observations

- **For the domain/statistics reviewer:** the Sortino overflow convention (clamp vs NULL when
  downside deviation ≈ 0, F-5), the −100% daily-return question (F-9), whether `information_ratio`
  should be storable without `benchmark_symbol` (N-3), and the `reward_weights` JSONB shape (only
  `jsonb_typeof = 'object'` is enforced — key/value validation is presumably code-side).
- **For the fix pass:** B-1's resolution changes F-6's urgency but not its content — RESTRICT needs
  the same indexes CASCADE does. Fix them together.
- **Migration mechanics:** 004 is applied to the live rh-db (checksummed); per the runner's invariant
  (`test_edited_applied_migration_halts_before_any_sql`), fixes must ship as a new `005_*.up.sql`, not
  edits to 004 — unless the fix pass deliberately rolls 004 back first (`down --allow-destructive
  --target 003` is currently free: all 004 tables are empty).
- The dashboard denominator mismatch flagged at `004_evaluation.up.sql:143-146` (issue #21) is
  pre-existing and out of scope here, but the comment's account-value convention is the right anchor.
- Live DB state after this review: 001–004 applied, checksums intact, every table empty (verified).
  Probes ran inside rolled-back transactions; IDENTITY sequences advanced past a few values, which is
  normal and harmless.
