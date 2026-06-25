"""Live multi-agent debate engine.

Mirrors 3a's jury/decision pipeline as running code: a bull and bear researcher build the two
cases, a jury of independent agents votes BUY/SELL/HOLD, and a deterministic aggregator turns the
votes into a decision (6+ decisive, 5-5 escalates to a human, otherwise HOLD). Each agent is a
real Anthropic API call; the engine streams progress so the dashboard can render it like 3a's
node stepper and jury grid.
"""
