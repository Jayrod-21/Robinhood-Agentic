-- Destructive by FILENAME. It drops the ledger that says who paid for what.
--
-- Eighth outing for "it is only derived" (014, 022, 023, 024, 025, 026, 027), and here the argument
-- is not even tempting: this data exists nowhere else. The provider's billing console shows a total
-- per key, not a per-call attribution with a model and a purpose — and it is exactly the record two
-- people would consult to settle up.
DROP VIEW IF EXISTS llm_spend_by_owner;
DROP TABLE IF EXISTS llm_usage;
