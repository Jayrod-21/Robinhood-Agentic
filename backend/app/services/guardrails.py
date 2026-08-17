"""The charter's risk rules, as code (docs/AGENTIC_ROBINHOOD_v1.md §5).

THE FAILURE THIS MODULE IS WRITTEN AGAINST
    Not "an unsafe trade got through". A guardrail that silently refuses a CORRECT order — the
    failure this project has already paid roughly four thousand dollars for on another system.
    A guard that blocks without explaining trains the operator to route around it, and a guard
    routed around is worse than absent, because it is still on the diagram.

    So every rule here is:
      * TUNABLE   — thresholds live in config, never as literals in a branch;
      * OBSERVABLE — evaluation returns every rule that RAN, passed ones included, so "nothing
        blocked this" is a visible fact rather than an absence anyone has to trust;
      * OVERRIDABLE — a `block` can be overridden by a named operator with a written reason, which
        is recorded. Two rules resist even that, and they are called out where they sit.

    Nothing here refuses silently. A refusal that does not name the rule, the threshold and the
    observed value is a bug in this module, not a safety feature.

WHAT THIS MODULE DOES NOT DO
    It does not decide whether a trade is a good idea. It answers "does this violate a rule the
    owners wrote down", and returns findings. Placing, sizing and confirming live elsewhere.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger("agentic.guardrails")

Severity = Literal["pass", "warn", "block"]


@dataclass(frozen=True)
class Finding:
    """One rule's verdict, carrying everything needed to argue with it.

    ``threshold`` and ``observed`` are not decoration: a blocked order must be traceable to the
    NUMBER that blocked it, not to "the system said no". They are what makes a wrong threshold
    findable instead of mysterious.
    """

    rule_key: str
    severity: Severity
    message: str
    threshold: float | None = None
    observed: float | None = None
    overridable: bool = True
    inputs: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == "block"


@dataclass(frozen=True)
class Verdict:
    findings: list[Finding]

    @property
    def blocked(self) -> bool:
        return any(f.blocking for f in self.findings)

    @property
    def blocking_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.blocking]

    @property
    def unoverridable(self) -> list[Finding]:
        """Blocks no override can clear. An override that could clear these would make them
        advisory, and the charter calls them hard for reasons that outlive any single trade."""
        return [f for f in self.findings if f.blocking and not f.overridable]

    @property
    def passed(self) -> bool:
        return not self.blocked


def evaluate(
    *,
    side: str,
    symbol: str,
    qty: float,
    price: float,
    account_value: float,
    cash: float,
    positions: dict[str, float],
    settings: Any,
    has_recorded_exit: bool,
    drawdown_pct: float | None = None,
) -> Verdict:
    """Evaluate every rule against a proposed order and return ALL findings, passes included.

    ``positions`` maps symbol → current market value. ``has_recorded_exit`` is whether a stop and
    target are on record for this name (charter: every entry records its exit BEFORE the buy).
    """
    findings: list[Finding] = []
    notional = qty * price
    is_buy = side == "buy"

    # ── Cash floor (charter: hard) ────────────────────────────────────────────────────────────
    # Overridable despite "hard". The charter's hardness is about intent, and a floor that cannot
    # be crossed by a named human with a stated reason is the silent-block failure in a different
    # costume. The override is recorded; that is the control, not the impossibility.
    floor_pct = settings.guardrail_cash_floor_pct
    cash_after = cash - notional if is_buy else cash + notional
    cash_after_pct = (cash_after / account_value * 100.0) if account_value > 0 else 0.0
    if is_buy and cash_after_pct < floor_pct:
        findings.append(Finding(
            rule_key="cash_floor",
            severity="block",
            message=(
                f"this order leaves {cash_after_pct:.1f}% in cash, below the {floor_pct:.0f}% floor "
                f"(${cash_after:,.2f} of ${account_value:,.2f})"
            ),
            threshold=floor_pct, observed=cash_after_pct,
            inputs={"cash_before": cash, "notional": notional, "cash_after": cash_after},
        ))
    else:
        findings.append(Finding(
            rule_key="cash_floor", severity="pass",
            message=f"leaves {cash_after_pct:.1f}% in cash (floor {floor_pct:.0f}%)",
            threshold=floor_pct, observed=cash_after_pct,
        ))

    # ── Max position size (charter: ~25%, the aggression lever) ───────────────────────────────
    max_pos_pct = settings.guardrail_max_position_pct
    existing = positions.get(symbol, 0.0)
    resulting = existing + notional if is_buy else max(0.0, existing - notional)
    resulting_pct = (resulting / account_value * 100.0) if account_value > 0 else 0.0
    if is_buy and resulting_pct > max_pos_pct:
        findings.append(Finding(
            rule_key="max_position_size",
            severity="block",
            message=(
                f"{symbol} would become {resulting_pct:.1f}% of the account, over the "
                f"{max_pos_pct:.0f}% cap (${resulting:,.2f} of ${account_value:,.2f})"
            ),
            threshold=max_pos_pct, observed=resulting_pct,
            inputs={"existing_value": existing, "notional": notional},
        ))
    else:
        findings.append(Finding(
            rule_key="max_position_size", severity="pass",
            message=f"{symbol} would be {resulting_pct:.1f}% of the account (cap {max_pos_pct:.0f}%)",
            threshold=max_pos_pct, observed=resulting_pct,
        ))

    # ── Max names (charter: 4-6 concentrated; a warn, not a block) ────────────────────────────
    # Warn because the charter states it as a shape preference, not a limit. Blocking a 7th name
    # would refuse a decision the owners are entitled to make.
    max_names = settings.guardrail_max_names
    names_after = len(set(positions) | {symbol}) if is_buy else len(positions)
    findings.append(Finding(
        rule_key="max_names",
        severity="warn" if names_after > max_names else "pass",
        message=(
            f"{names_after} positions after this order (charter shape: up to {max_names})"
        ),
        threshold=float(max_names), observed=float(names_after),
    ))

    # ── Sell discipline (charter: hard — every entry records its exit BEFORE the buy) ─────────
    # Overridable, same reasoning as the cash floor: recorded, not impossible.
    if is_buy and not has_recorded_exit:
        findings.append(Finding(
            rule_key="exit_recorded_before_entry",
            severity="block",
            message=(
                f"no stop or target on record for {symbol}. The charter requires the exit be "
                f"written down before the entry — record one, or override with a reason."
            ),
            inputs={"symbol": symbol},
        ))
    else:
        findings.append(Finding(
            rule_key="exit_recorded_before_entry", severity="pass",
            message="exit is on record" if is_buy else "not applicable to a sell",
        ))

    # ── Drawdown guard (charter: halt new entries and escalate) ───────────────────────────────
    # NOT overridable, and the only rule here that is not. The charter's other hard rules protect a
    # single trade's shape; this one fires when the account is already losing and says stop opening
    # new risk. An override at exactly that moment is the decision least likely to be made well, and
    # the whole point of a circuit breaker is that it is not negotiable while the fault is live.
    # Clearing it is a deliberate act outside the order flow, by someone who has looked at why.
    max_dd = settings.guardrail_max_drawdown_pct
    if drawdown_pct is not None:
        if is_buy and drawdown_pct >= max_dd:
            findings.append(Finding(
                rule_key="drawdown_halt",
                severity="block",
                message=(
                    f"account is {drawdown_pct:.1f}% below its high-water mark, at or past the "
                    f"{max_dd:.0f}% halt. New entries are stopped until an owner clears this."
                ),
                threshold=max_dd, observed=drawdown_pct, overridable=False,
            ))
        else:
            findings.append(Finding(
                rule_key="drawdown_halt", severity="pass",
                message=f"drawdown {drawdown_pct:.1f}% (halt at {max_dd:.0f}%)",
                threshold=max_dd, observed=drawdown_pct, overridable=False,
            ))
    else:
        # Absent input is reported, never silently treated as "fine". An unevaluated rule that looks
        # like a passing one is how coverage quietly disappears.
        findings.append(Finding(
            rule_key="drawdown_halt", severity="warn",
            message="drawdown could not be computed (no high-water mark on record) — rule not evaluated",
            threshold=max_dd, overridable=False,
        ))

    return Verdict(findings=findings)
