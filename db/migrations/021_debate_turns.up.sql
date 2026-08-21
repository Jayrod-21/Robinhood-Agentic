-- 021 — the exchange itself, turn by turn.
--
-- WHY THE TRANSCRIPT NEEDS A TABLE
--   `agent_proposals` holds one row per side: the case each researcher ended up making. That was
--   the whole record while the engine produced exactly one statement each — bull and bear written
--   CONCURRENTLY, neither having seen the other, both handed straight to the jury.
--
--   That was not a debate. It was two monologues and a vote, and it could not show the thing a
--   debate exists to show: whether a case survives being contradicted by someone trying to break
--   it. With rebuttal rounds there is now an argument with an order to it, and the order carries
--   the meaning — a concession in round 2 is only legible next to the claim in round 1 that forced
--   it.
--
-- ONE ROW PER TURN, ORDERED
--   (debate_id, round_no, side) is unique: a side speaks once per round. `kind` distinguishes an
--   opening from a rebuttal from a closing, because they are read differently — an opening is a
--   position, a rebuttal is a response, and flattening them loses which is which.

CREATE TABLE IF NOT EXISTS debate_turns (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    debate_id  bigint NOT NULL REFERENCES debates(id) ON DELETE CASCADE,
    round_no   integer NOT NULL,
    side       text NOT NULL,
    kind       text NOT NULL,
    content    text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_debate_turns_round CHECK (round_no >= 1),
    CONSTRAINT ck_debate_turns_side  CHECK (side = ANY (ARRAY['bull','bear'])),
    CONSTRAINT ck_debate_turns_kind  CHECK (kind = ANY (ARRAY['opening','rebuttal','closing'])),
    CONSTRAINT ck_debate_turns_content CHECK (length(btrim(content)) > 0),
    CONSTRAINT uq_debate_turns UNIQUE (debate_id, round_no, side)
);

CREATE INDEX IF NOT EXISTS ix_debate_turns_debate ON debate_turns (debate_id, round_no, side);

GRANT SELECT, INSERT ON debate_turns TO rh_app;
GRANT USAGE, SELECT ON SEQUENCE debate_turns_id_seq TO rh_app;
