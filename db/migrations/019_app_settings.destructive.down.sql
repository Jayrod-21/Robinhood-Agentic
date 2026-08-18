-- Destructive by filename (ADR-002): dropping these discards every tuning an operator has made and
-- the record of who made it. The thresholds fall back to the defaults compiled into the registry,
-- so the app keeps working — but the history is gone and cannot be reconstructed.
DROP TABLE IF EXISTS app_settings_history;
DROP TABLE IF EXISTS app_settings;
