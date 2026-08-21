-- Destructive by FILENAME, because it drops a table — which is what the runner checks, and it is
-- right to. The first draft of this file argued the drop was safe since cycle_runs is only
-- progress telemetry, derived from work whose real output is the debate records, the judgments and
-- the report. The runner refused it, and the same reasoning was refused once before on 014.
--
-- Destructiveness is what the SQL DOES, not what the table happens to hold today. What is lost is
-- the timeline of what ran when, which cannot be reconstructed from the results.
DROP TABLE IF EXISTS cycle_runs;
