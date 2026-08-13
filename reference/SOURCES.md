# Canonical Sources — referenced from 3a, not copied

This project is self-contained but treats `3a. SpecialSprinkleSauce` as the **source of truth
for the Wasden *thinking*** (framework, screening spec, governance). We reference these files
rather than importing 3a's code, so there is no cross-repo coupling and no drift.

> Both folders are siblings in the same local projects directory; resolve them relative to this
> repo's parent rather than assuming any absolute path. 3b now has a remote
> (`Jayrod-21/Robinhood-Agentic`, private), so a collaborator's checkout will **not** include 3a —
> vendor read-only snapshots of anything load-bearing into `reference/` and note the snapshot date.

| What | Canonical path (in 3a) | Used for |
|------|------------------------|----------|
| Wasden knowledge base (full framework) | `3a. SpecialSprinkleSauce/docs/KNOWLEDGE_BASE_v2.md` | The Wasden lens: 5-bucket framework, core disciplines, philosophy, risk, behavioral. The intelligence grounding. |
| Sprinkle Sauce screening spec | `3a. SpecialSprinkleSauce/docs/sprinkle_sauce_spec.md` | Tier definitions, Piotroski rules, thresholds. Basis for `src/` screen. |
| Reference screen implementation | `3a. SpecialSprinkleSauce/backend/app/services/screening_engine.py` | The 3a Python implementation we model the lean yfinance version on. |
| Piotroski F-score logic | `3a. SpecialSprinkleSauce/backend/app/services/piotroski.py` | Single-snapshot proportional scoring reference. |
| Governance / protected-component rules | `3a. SpecialSprinkleSauce/docs/CLAUDE_v2.md` | Trade-execution safety, logging, what requires human approval. |

## What 3b owns (does NOT reference 3a)
- The operating charter (`docs/AGENTIC_ROBINHOOD_v1.md`)
- The live trading journal (`docs/agentic_journal.md`)
- The daily-scan code, yfinance data adapter, and Robinhood MCP execution glue (`src/`)
- Risk constants retuned for the $100 account (in the charter §5)
