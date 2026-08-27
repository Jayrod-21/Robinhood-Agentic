"""Whether a jury actually deliberated, or just agreed with itself in the same voice.

WHY THIS EXISTS
    Measured over 2,145 real judgments before it did: the confidence 0.72 appeared 1,269 times —
    59% of every vote ever cast. In one TMO debate, 8 of 10 jurors returned exactly `SELL 0.72`.
    The ten lenses were producing genuinely different ARGUMENTS — P/E, FCF yield, sector rotation,
    price action — and collapsing onto one NUMBER.

    That is not a small cosmetic problem. The page renders a confidence bar, which asserts a
    measurement; a constant is not a measurement. And the operator who noticed ("most of the
    confidence intervals are almost always the same") was reading the panel correctly while the
    panel was telling them nothing.

WHAT IT DOES NOT DO
    Change the verdict. `aggregate` counts votes and will keep counting votes — that stays
    deliberately family-agnostic, which matters more once Gemini jurors sit on the same panel and
    a Gemini 0.8 and a Claude 0.8 cannot be assumed to mean the same thing. These signals annotate
    a result; they never decide one.
"""

from __future__ import annotations

import statistics

# Below this, the panel's confidences are effectively one number with rounding noise. Chosen from
# the observed failure: a jury where 8 of 10 said 0.72 and two said 0.68/0.62 has a standard
# deviation of about 0.035, and that jury should be flagged.
_FLAT_CONFIDENCE_STDEV = 0.05

# A jury that unanimously agrees is not automatically suspect — sometimes a name really is obvious.
# It becomes suspect when it is ALSO flat: ten lenses that all saw something different should not
# arrive at the same number, and when they do, the number is a habit rather than a reading.
_MIN_VOTES_TO_JUDGE = 4


def signals(votes) -> list[str]:
    """Ways this jury's output should be read with suspicion. Empty is the healthy case.

    Each string is written to be shown to an operator as-is, because the audience for "your jury
    is not discriminating" is the person deciding whether to trust the verdict.
    """
    found: list[str] = []
    confidences = [
        float(v.confidence) for v in votes if getattr(v, "confidence", None) is not None
    ]
    if len(confidences) < _MIN_VOTES_TO_JUDGE:
        return found

    spread = statistics.stdev(confidences)
    if spread < _FLAT_CONFIDENCE_STDEV:
        found.append(
            f"confidence is effectively constant across the panel (sd {spread:.3f}) — ten lenses "
            f"reading different evidence should not converge on one number; treat these as "
            f"unweighted votes, not as strengths"
        )

    modal = max(set(confidences), key=confidences.count)
    modal_share = confidences.count(modal) / len(confidences)
    if modal_share >= 0.6 and len(confidences) >= 5:
        found.append(
            f"{confidences.count(modal)} of {len(confidences)} jurors returned exactly {modal:g} — "
            f"a repeated identical value is a model habit, not agreement"
        )

    # Every juror at the ceiling or the floor. Distinct from flatness: a panel that is uniformly
    # certain has stopped discriminating in the other direction.
    if all(c >= 0.9 for c in confidences):
        found.append("every juror reported near-certainty — the panel is not discriminating")
    elif all(c <= 0.55 for c in confidences):
        found.append(
            "every juror reported near-ambivalence — the evidence given to the panel may be too "
            "thin to judge on"
        )
    return found


def confidence_summary(votes) -> dict:
    """Descriptive statistics for the panel's confidence, for display and for later analysis.

    `usable` is the field that matters: False means the numbers are present but carry no
    information, and a page showing a confidence bar should say so rather than draw it.
    """
    confidences = [
        float(v.confidence) for v in votes if getattr(v, "confidence", None) is not None
    ]
    if not confidences:
        return {"n": 0, "mean": None, "stdev": None, "min": None, "max": None, "usable": False}

    spread = statistics.stdev(confidences) if len(confidences) > 1 else 0.0
    return {
        "n": len(confidences),
        "mean": round(statistics.mean(confidences), 4),
        "stdev": round(spread, 4),
        "min": min(confidences),
        "max": max(confidences),
        # False when the panel's confidences are one number wearing a distribution's clothes.
        "usable": len(confidences) >= _MIN_VOTES_TO_JUDGE and spread >= _FLAT_CONFIDENCE_STDEV,
    }


# ── paired panels: did the families actually disagree? ────────────────────────────────────────
#
# The whole reason to run ten lenses twice, once per model family, is that a disagreement then has
# exactly one explanation available: same evidence, same lens, different model. Splitting the lenses
# 5/5 between providers would have cost nothing extra and made every disagreement unattributable.
#
# What this must NOT do is treat a family split as a tie to be resolved. A 10-10 split along family
# lines is the single most informative thing this panel can produce — it says the question is
# genuinely model-dependent — and rendering it as a generic deadlock would throw that away.

# Below this level of agreement between the families, the panel is not reading one question the same
# way. Chosen loosely on purpose: the first weeks of a paired panel are a MEASUREMENT of how often
# the families diverge, not a signal to act on, and a tight threshold would manufacture alarm before
# there is a baseline to compare against.
_FAMILY_AGREEMENT_FLOOR = 0.6


def family_signals(votes) -> list[str]:
    """How the model families compared, when more than one sat on the panel.

    Empty when the panel was single-family — there is nothing to compare — or when the families
    landed in the same place, which is the uninteresting case.
    """
    by_provider: dict[str, list] = {}
    for v in votes:
        by_provider.setdefault(getattr(v, "provider", "anthropic"), []).append(v)
    if len(by_provider) < 2:
        return []

    found: list[str] = []
    majorities = {}
    for provider, group in by_provider.items():
        counts: dict[str, int] = {}
        for v in group:
            counts[v.vote.value] = counts.get(v.vote.value, 0) + 1
        top = max(counts.items(), key=lambda kv: kv[1])
        majorities[provider] = (top[0], top[1] / len(group), len(group))

    verdicts = {m[0] for m in majorities.values()}
    if len(verdicts) > 1:
        detail = ", ".join(
            f"{p} {v} ({share:.0%} of {n})" for p, (v, share, n) in sorted(majorities.items())
        )
        found.append(
            f"the model families reached DIFFERENT verdicts — {detail}. This is a finding, not a "
            f"tie: the same lenses read the same evidence and disagreed by family, which points at "
            f"the question being model-dependent rather than at either family being wrong"
        )

    # Agreement measured per LENS, which is the comparison pairing exists to make possible.
    paired_lenses = 0
    disagreeing = []
    seen: dict[str, dict[str, str]] = {}
    for v in votes:
        seen.setdefault(v.focus_area, {})[getattr(v, "provider", "anthropic")] = v.vote.value
    for lens, per_provider in seen.items():
        if len(per_provider) < 2:
            continue
        paired_lenses += 1
        if len(set(per_provider.values())) > 1:
            disagreeing.append(lens)

    if paired_lenses:
        agreement = 1 - (len(disagreeing) / paired_lenses)
        if agreement < _FAMILY_AGREEMENT_FLOOR:
            found.append(
                f"the families disagreed on {len(disagreeing)} of {paired_lenses} paired lenses "
                f"({agreement:.0%} agreement) — {', '.join(sorted(disagreeing)[:6])}"
            )
    return found


def family_summary(votes) -> dict:
    """Per-family vote counts and per-lens agreement, for display and for later analysis.

    Recorded on every paired debate so the question "do these families actually differ, and where"
    can be answered from history rather than from impressions. Until there are enough debates to
    answer it, a cross-family AGREEMENT is not extra confidence — it may only mean the question was
    easy.
    """
    by_provider: dict[str, dict[str, int]] = {}
    for v in votes:
        provider = getattr(v, "provider", "anthropic")
        by_provider.setdefault(provider, {"BUY": 0, "SELL": 0, "HOLD": 0})[v.vote.value] += 1

    per_lens: dict[str, dict[str, str]] = {}
    for v in votes:
        per_lens.setdefault(v.focus_area, {})[getattr(v, "provider", "anthropic")] = v.vote.value
    paired = {k: p for k, p in per_lens.items() if len(p) > 1}
    agreed = sum(1 for p in paired.values() if len(set(p.values())) == 1)

    return {
        "providers": by_provider,
        "paired_lenses": len(paired),
        "lenses_agreed": agreed,
        "agreement": round(agreed / len(paired), 4) if paired else None,
        "disagreed_on": sorted(k for k, p in paired.items() if len(set(p.values())) > 1),
    }
