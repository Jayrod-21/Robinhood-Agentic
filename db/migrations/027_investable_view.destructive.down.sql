-- Destructive by FILENAME, and this one is genuinely mild: dropping a view destroys no data, and
-- the view is one line of SQL that can be recreated exactly.
--
-- Marked destructive anyway because the runner classifies by what the SQL DOES, not by how much it
-- costs — DROP is DROP, and the seventh appearance of "but this one is only derived" (014, 022,
-- 023, 024, 025, 026) is not the moment to start making exceptions. What breaks on the way down is
-- every consumer that selects from it, which is the Lab.
DROP VIEW IF EXISTS investable_securities;
