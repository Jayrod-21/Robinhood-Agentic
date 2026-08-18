-- Reverse of 015. Destructive: dropping requested_notional discards the only record of what a
-- notional order asked for, and restoring NOT NULL on requested_qty would fail against any such
-- row. Back up `orders` first.
ALTER TABLE orders DROP CONSTRAINT IF EXISTS ck_orders_notional_positive;
ALTER TABLE orders DROP CONSTRAINT IF EXISTS ck_orders_qty_or_notional;
ALTER TABLE orders DROP COLUMN IF EXISTS requested_notional;
ALTER TABLE orders ALTER COLUMN requested_qty SET NOT NULL;
