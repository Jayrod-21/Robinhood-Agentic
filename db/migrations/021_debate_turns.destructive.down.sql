-- Destructive by filename (ADR-002): this discards every transcript. The turns are model output
-- that cost real tokens to produce and cannot be regenerated — rerunning a debate produces a
-- DIFFERENT argument, not the same one again. The JSON file records under logs/debates are the
-- only other copy, and they are no longer tracked in git.
DROP TABLE IF EXISTS debate_turns;
