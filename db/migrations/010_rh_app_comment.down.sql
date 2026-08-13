-- 010_rh_app_comment (down) — remove the rh_app role comment.
--
-- 001 set no role comment, so NULL restores the exact prior catalog state. Nothing stored in a
-- table is touched; the filename carries no destructive marker (the runner's blanket rollback
-- gate still applies, as it does to every down).

COMMENT ON ROLE rh_app IS NULL;
