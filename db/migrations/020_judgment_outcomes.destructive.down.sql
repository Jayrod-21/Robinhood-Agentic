-- Destructive by filename (ADR-002): this drops every scored outcome. They are derived from prices
-- and can be recomputed by db/score_judgments.py, but the recomputation uses TODAY's rule — so if
-- the rule has changed since, the restored table will not match what calibration reported before.
DROP TABLE IF EXISTS judgment_outcomes;
