"""Action policy. Deterministic matrix over (band, rules, amount, kyc).

The recommended action is computed HERE, never by the LLM. No action above
MONITOR is ever executed without a recorded human decision.
"""
from __future__ import annotations

from .rules import HARD_RULES

ACTIONS = ("ALLOW", "MONITOR", "STEP_UP_AUTH", "HOLD_FOR_REVIEW", "BLOCK")


def band_for(score: int) -> str:
    if score < 35:
        return "LOW"
    if score < 62:
        return "MEDIUM"
    if score < 82:
        return "HIGH"
    return "CRITICAL"


def decide(band: str, rules_fired: list[str], amount_paise: int, amount_p95_paise: int,
           kyc_tier: int) -> tuple[str, bool]:
    """Return (recommended_action, requires_human_review)."""
    high_value = amount_paise > max(amount_p95_paise, 1)
    hard = any(r in HARD_RULES for r in rules_fired)

    if band == "LOW":
        return "ALLOW", False
    if band == "MEDIUM":
        return "MONITOR", False
    if band == "HIGH":
        if high_value or rules_fired:
            return "HOLD_FOR_REVIEW", True
        return "STEP_UP_AUTH", True
    # CRITICAL
    if hard:
        return "BLOCK", True
    return "HOLD_FOR_REVIEW", True
