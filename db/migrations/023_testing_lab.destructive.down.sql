-- Destructive by FILENAME, because it drops tables — and the runner checks the name, not the body.
--
-- Worth saying plainly, since the same argument was tried and refused on 014 and again on 022: it
-- is tempting to call this non-destructive because Lab results are "just experiments" and can be
-- re-run. They cannot. A walk-forward validation is a measurement of a specific model against a
-- specific dataset at a specific time, and re-running it after the code has moved produces a
-- different number, not the same one. What is lost is the record of what was true when the decision
-- was made.
--
-- Order matters: model_runs and sweeps both reference experiments.
DROP TABLE IF EXISTS sweeps;
DROP TABLE IF EXISTS model_runs;
DROP TABLE IF EXISTS experiments;
