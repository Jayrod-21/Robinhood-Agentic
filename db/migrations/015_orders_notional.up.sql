-- 015_orders_notional — let an order be sized in DOLLARS, not only shares.
--
-- 014 assumed every order names a share count. A notional order ("$500 of NVDA") does not: the
-- share count is decided by the fill, and at submission time it genuinely does not exist.
--
-- The alternative was to divide the notional by a current price and store the result in
-- `requested_qty`. That would put an ESTIMATE in a column whose name says it is what the operator
-- asked for, in the table that exists to record what the operator asked for. The estimate would
-- then disagree with the fill by a little, forever, with nothing marking it as a guess.
--
-- So: one of the two is set, never both, never neither — enforced rather than documented.

ALTER TABLE orders ADD COLUMN requested_notional numeric(18, 2);
ALTER TABLE orders ALTER COLUMN requested_qty DROP NOT NULL;

ALTER TABLE orders ADD CONSTRAINT ck_orders_qty_or_notional CHECK (
    (requested_qty IS NOT NULL AND requested_notional IS NULL)
    OR (requested_qty IS NULL AND requested_notional IS NOT NULL)
);
ALTER TABLE orders ADD CONSTRAINT ck_orders_notional_positive CHECK (
    requested_notional IS NULL OR requested_notional > 0
);

COMMENT ON COLUMN orders.requested_notional IS
    'Dollar amount requested, for notional orders. Exactly one of requested_qty / '
    'requested_notional is set. The share count for a notional order is a property of the FILL '
    '(filled_qty), never of the request — storing an estimated qty here would put a guess in the '
    'column that records what was asked for.';
