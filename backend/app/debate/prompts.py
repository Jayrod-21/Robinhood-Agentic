"""Prompts for the bull/bear researchers and the 10-agent jury.

The ten juror lenses mirror 3a's jury design so the two systems read alike. Each juror gets the
same evidence (fundamentals, live price, the bull and bear cases) but judges through one lens, then
casts a single BUY/SELL/HOLD vote with a confidence and a short reason.
"""

from __future__ import annotations

# Grounding shared by every agent in the debate. Wasden fundamentals-first, tuned for the live
# aggressive Agentic cash account (concentration is the aggression lever, not leverage or churn).
# Deliberately not pinned to a dollar figure — the founding stake was $100 and the balance moves.
SYSTEM_GROUNDING = (
    "You are an analyst on Cary Wasden's fundamentals-first trading team, judging a single equity "
    "for a small, aggressive, long-only cash account. The edge is buying durable cash machines at a "
    "fair price with a real forward catalyst, and always knowing the exit before the entry. "
    "Concentration is the source of aggression — not leverage, not day-trading churn. Be decisive "
    "and honest about risk. This is analysis for a human to act on, never an order."
)

# (agent_id, focus_area, lens) — ported from 3a's ten-juror design.
JUROR_PERSPECTIVES: list[tuple[int, str, str]] = [
    (1, "valuation", "Valuation metrics — P/E, PEG, price vs. intrinsic value. Is it cheap or expensive for what you get?"),
    (2, "cash_flow", "Cash-flow quality — free-cash-flow yield, conversion, leverage. Is this a real cash machine?"),
    (3, "growth", "Growth & profitability — revenue growth, margins, returns on capital. Is the business compounding?"),
    (4, "macro_rates", "Fed & rates — how do the current rate path and liquidity regime help or hurt this name?"),
    (5, "sector", "Sector rotation & industry dynamics — is capital rotating toward or away from this group?"),
    (6, "tail_risk", "Downside & tail risk — what is the realistic worst case, and how likely is it?"),
    (7, "risk_reward", "Risk-reward asymmetry — is the upside meaningfully larger than the downside from here?"),
    (8, "technical", "Trend & price action — what is the chart saying about supply, demand, and timing?"),
    (9, "sentiment", "Volume & sentiment — positioning, crowding, and whether the trade is over- or under-owned."),
    (10, "wasden_framework", "Wasden 5-bucket framework — bucket fit, cheap/expensive, dated catalyst, and a clear exit."),
]


def juror_user_prompt(
    ticker: str,
    focus_area: str,
    lens: str,
    price: float | None,
    fundamentals: dict | None,
    bull_case: str,
    bear_case: str,
) -> str:
    """Build the per-juror user message. Telegraphic by design — the reader is a model."""
    fund_lines = _format_fundamentals(fundamentals)
    price_line = f"Live price: ${price:.2f}" if price is not None else "Live price: unavailable"
    return (
        f"Ticker: {ticker}\n"
        f"Your lens: {lens}\n\n"
        f"{price_line}\n"
        f"Fundamentals:\n{fund_lines}\n\n"
        f"BULL case:\n{bull_case}\n\n"
        f"BEAR case:\n{bear_case}\n\n"
        f"Judge {ticker} ONLY through your {focus_area} lens. Weigh the evidence above, then call it. "
        f"Submit your verdict by calling the cast_vote tool: vote BUY, SELL, or HOLD, a confidence "
        f"from 0 to 1, and one or two sentences of reasoning specific to your lens."
    )


def researcher_prompt(ticker: str, side: str, price: float | None, fundamentals: dict | None) -> str:
    """Bull or bear researcher prompt. ``side`` is 'bull' or 'bear'."""
    fund_lines = _format_fundamentals(fundamentals)
    price_line = f"Live price: ${price:.2f}" if price is not None else "Live price: unavailable"
    stance = (
        "Make the strongest HONEST case to BUY and hold this name"
        if side == "bull"
        else "Make the strongest HONEST case to AVOID or SELL this name"
    )
    return (
        f"Ticker: {ticker}\n{price_line}\nFundamentals:\n{fund_lines}\n\n"
        f"{stance}. Ground it in the fundamentals and a forward, falsifiable thesis "
        f"(mechanism, catalyst, what would prove you wrong). 4-7 tight sentences. No hedging to the "
        f"other side — that's the other researcher's job."
    )


def _format_fundamentals(fundamentals: dict | None) -> str:
    if not fundamentals:
        return "  (none available)"
    keys = [
        ("name", "Name"),
        ("sector", "Sector"),
        ("market_cap", "Market cap"),
        ("peg", "PEG"),
        ("fcf_yield", "FCF yield %"),
        ("trailing_pe", "Trailing P/E"),
        ("forward_pe", "Forward P/E"),
        ("gross_margin", "Gross margin"),
        ("revenue_growth", "Revenue growth"),
    ]
    lines = []
    for key, label in keys:
        val = fundamentals.get(key)
        if val is not None:
            lines.append(f"  {label}: {val}")
    return "\n".join(lines) if lines else "  (none available)"


# Tool the jurors must call — forces a structured, parseable vote.
CAST_VOTE_TOOL = {
    "name": "cast_vote",
    "description": "Submit your single verdict on the ticker, judged through your assigned lens.",
    "input_schema": {
        "type": "object",
        "properties": {
            "vote": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
            "confidence": {"type": "number", "description": "0.0 to 1.0"},
            "reasoning": {"type": "string", "description": "1-2 sentences, specific to your lens"},
        },
        "required": ["vote", "confidence", "reasoning"],
    },
}
