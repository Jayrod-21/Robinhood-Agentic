"""Detecting a jury that agreed with itself in the same voice.

Measured over 2,145 real judgments before this existed: the confidence 0.72 appeared 1,269 times —
59% of every vote ever cast. In one TMO debate 8 of 10 jurors returned exactly `SELL 0.72` while
citing genuinely different evidence: P/E, FCF yield, PEG, sector rotation, price action, crowding.
Ten lenses producing different ARGUMENTS and one NUMBER.

The operator noticed before the software did — "most of the confidence intervals are almost always
the same" — which is the failure these tests exist to make impossible to miss again.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.debate import calibration


def _votes(*confidences):
    return [SimpleNamespace(confidence=c) for c in confidences]


# ── the failure that was actually happening ───────────────────────────────────────────────────


def test_the_real_tmo_panel_is_flagged() -> None:
    """The measured jury: eight jurors at 0.72, two dissenting slightly. Break: raise the flatness
    threshold until this passes, and the panel that started all of this reads as healthy."""
    signals = calibration.signals(_votes(*([0.72] * 8), 0.68, 0.62))

    assert signals, "the panel that prompted this work must not read as healthy"
    assert any("returned exactly 0.72" in s for s in signals)


def test_a_perfectly_flat_panel_is_flagged() -> None:
    signals = calibration.signals(_votes(*([0.72] * 10)))

    assert any("effectively constant" in s for s in signals)


def test_a_genuinely_varied_panel_is_not_flagged() -> None:
    """Break: flag on any repeated value. A real panel repeats numbers sometimes, and an alarm that
    fires on every debate is one nobody reads."""
    assert calibration.signals(_votes(0.9, 0.45, 0.72, 0.6, 0.85, 0.5, 0.78, 0.35, 0.66, 0.55)) == []


def test_uniform_certainty_is_its_own_signal() -> None:
    """Distinct from flatness: a panel that is uniformly certain has stopped discriminating in the
    other direction."""
    signals = calibration.signals(_votes(0.95, 0.96, 0.97, 0.94, 0.99, 0.93, 0.98, 0.95))

    assert any("near-certainty" in s for s in signals)


def test_uniform_ambivalence_points_at_the_evidence_not_the_jury() -> None:
    """Ten lenses that all say "I cannot tell" is a statement about what they were given to read."""
    signals = calibration.signals(_votes(0.5, 0.52, 0.48, 0.5, 0.55, 0.5, 0.51, 0.49))

    assert any("too\n" in s or "too thin" in s for s in signals)


def test_a_short_panel_is_not_judged() -> None:
    """Three jurors landing near each other is not evidence of a habit."""
    assert calibration.signals(_votes(0.7, 0.7, 0.7)) == []


def test_missing_confidences_do_not_crash_it() -> None:
    """195 judgments in the live table have a NULL confidence."""
    votes = [SimpleNamespace(confidence=None) for _ in range(10)]
    assert calibration.signals(votes) == []
    assert calibration.confidence_summary(votes)["usable"] is False


# ── the summary a page can act on ─────────────────────────────────────────────────────────────


def test_usable_is_false_when_the_numbers_carry_no_information() -> None:
    """The page renders a confidence bar, which ASSERTS a measurement. A constant is not a
    measurement, and `usable: False` is how the page learns to say so instead of drawing it."""
    assert calibration.confidence_summary(_votes(*([0.72] * 10)))["usable"] is False


def test_usable_is_true_for_a_real_spread() -> None:
    summary = calibration.confidence_summary(_votes(0.9, 0.45, 0.72, 0.6, 0.85, 0.5, 0.78, 0.35))

    assert summary["usable"] is True
    assert summary["n"] == 8
    assert summary["min"] == 0.35 and summary["max"] == 0.9
    assert summary["stdev"] > 0.05


def test_an_empty_panel_summarises_without_raising() -> None:
    assert calibration.confidence_summary([])["n"] == 0


# ── it annotates; it must never decide ────────────────────────────────────────────────────────


def test_the_verdict_is_unchanged_by_a_flat_panel() -> None:
    """The decision stays a VOTE COUNT. That matters more once Gemini jurors sit on the same panel:
    a Gemini 0.8 and a Claude 0.8 cannot be assumed to mean the same thing, but a vote is a vote.

    Break: weight the verdict by confidence. This goes red, and cross-family verdicts become
    incomparable.
    """
    from app.debate.aggregate import aggregate
    from app.debate.schemas import Decision, JurorVote, Vote

    flat = [
        JurorVote(agent_id=i, focus_area="lens", vote=Vote.SELL, confidence=0.72, reasoning="r")
        for i in range(1, 9)
    ] + [
        JurorVote(agent_id=9, focus_area="lens", vote=Vote.HOLD, confidence=0.68, reasoning="r"),
        JurorVote(agent_id=10, focus_area="lens", vote=Vote.HOLD, confidence=0.62, reasoning="r"),
    ]
    result = aggregate(flat, jury_size=10)

    assert result.decision == Decision.SELL, "8 of 10 is a decisive majority regardless of flatness"
    assert result.calibration_signals, "and the panel is annotated as suspect"
    assert result.confidence["usable"] is False
