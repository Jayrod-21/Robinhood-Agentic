-- 025 — what KIND of instrument each security is, so a universe can be a universe.
--
-- THE STATE THIS REPLACES (issue #41)
--   securities holds 19,745 rows and is described in DATA_INVENTORY.md as "everything that traded",
--   because load_daily_bars.py derives a bar for every symbol in the Polygon archive — the entire
--   US tape. security_type, name and exchange were populated on NINETEEN of those rows.
--
--   Measured with the classifier this migration constrains: 47.3% common stock, 26.3% ETF, and
--   26.4% — 5,149 securities — warrants, units, rights, share classes, or instruments the data
--   provider does not carry at all. A screen, a backtest, or a Testing Lab training run over that
--   universe is reading SPAC warrants and unexercised rights as though they were companies.
--
-- WHY THIS ALSO CLOSES THE GAP AUDIT'S OPEN ITEM
--   The same 107 holes the audit could not explain are, overwhelmingly, not mysteries: 72 warrants,
--   8 units, 3 rights, 2 share classes and 16 untracked instruments. A delisted warrant HAS no
--   provider history — the absence that made them 'provider_unresolvable' is the EXPECTED answer
--   for that instrument form, not evidence of a ticker recycle. Splicing them would have fabricated
--   identity breaks that never happened.
--
--   So a new disposition, non_common_instrument, and it is TERMINAL. Six holes survive it —
--   DMN, DTC, KNW, OAS, ROCC, SNMP — every one a real company with a real corporate event
--   (Oasis→Chord, Ranger Oil→Baytex, Solo Brands delisted, Know Labs reverse split). Those stay
--   non-terminal, which is correct: they are a residue for a human, not noise to clear.
--
-- WHY security_type IS CONSTRAINED AND NOT FREE TEXT
--   Every consumer of it branches on the value. A typo'd 'stocks' would silently drop a company out
--   of the investable universe with nothing to notice — the failure mode this project keeps finding,
--   where a stored value means something other than its name says.

ALTER TABLE securities
    ADD CONSTRAINT ck_securities_security_type CHECK (
        security_type IS NULL OR security_type = ANY (ARRAY[
            'stock', 'etf', 'warrant', 'unit', 'right', 'share_class', 'untracked'
        ])
    );

-- NULL stays legal on purpose: it means "the classifier has not run for this row", which is a
-- different fact from any of the seven values above and must not be forced to impersonate one.
COMMENT ON COLUMN securities.security_type IS
    'Instrument form: stock | etf | warrant | unit | right | share_class | untracked. '
    'NULL = never classified, which is NOT the same as untracked (the provider does not carry it). '
    'Populated by db/load_instrument_types.py from two bulk FMP lists plus symbol convention; '
    'db/instrument_class.py::is_investable is the single definition of what a universe may draw '
    'from. See issue #41.';

-- The universe query, indexed. Partial because the investable set is under three-quarters of the
-- table and it is the only slice anything downstream ever wants.
CREATE INDEX IF NOT EXISTS ix_securities_investable ON securities (symbol)
    WHERE security_type IN ('stock', 'etf');

CREATE INDEX IF NOT EXISTS ix_securities_type ON securities (security_type);

-- ── the gap audit gains a terminal disposition for instrument form ────────────────────────────

ALTER TABLE price_gap_audit
    DROP CONSTRAINT IF EXISTS ck_price_gap_audit_disposition;

ALTER TABLE price_gap_audit
    ADD CONSTRAINT ck_price_gap_audit_disposition CHECK (disposition = ANY (ARRAY[
        'pending_review',
        'identity_break',
        'provider_unresolvable',
        'split_missing',
        'halt_consistent',
        'continuity_confirmed',
        'spliced',
        'halt_accepted',
        -- NEW. The hole is on a warrant, unit, right, share class or untracked instrument, so the
        -- provider's silence is the expected answer for that form rather than an unresolved
        -- question. Terminal: verify_daily_series check 7 no longer fails on it.
        'non_common_instrument'
    ]));
