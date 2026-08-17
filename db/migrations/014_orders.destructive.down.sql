-- Reverse of 014_orders.
--
-- Named .destructive. because it drops tables. The first draft was not, on the reasoning that both
-- tables are empty when the migration is applied — the runner refused it, and the runner is right.
-- Destructiveness is a property of what the SQL DOES, not of what happens to be in the table on the
-- day it runs. An empty `orders` today is a populated one the moment anything trades, and a
-- classification that depends on timing is a classification nobody can rely on.
--
-- If you are here to roll back a live system: back up `orders` and `execution_arming` first. A
-- rollback that erases what was traded is not a rollback.

DROP TABLE IF EXISTS execution_arming;
DROP TABLE IF EXISTS orders;
