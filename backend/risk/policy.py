"""Action policy. Deterministic mapping from severity band to recommended action.

The recommended action is computed HERE, never by the LLM. No action above
ALLOW is ever executed without a recorded human decision.
"""
from __future__ import annotations

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
    """Return (recommended_action, requires_human_review).

    The severity band alone determines the recommended action, so the score and
    the recommendation can never be semantically inconsistent:

        LOW      -> ALLOW            (no human review)
        MEDIUM   -> HOLD_FOR_REVIEW  (human review)
        HIGH     -> HOLD_FOR_REVIEW  (human review)
        CRITICAL -> BLOCK            (human review)

    A CRITICAL assessment ALWAYS recommends BLOCK - there is no branch by which
    it can degrade to a softer action. Should a future business rule need to
    justify a different action for a specific CRITICAL pattern, add it here as an
    explicit, named exception (it has access to ``rules_fired``, ``amount_paise``
    and ``kyc_tier`` for that purpose).
    """
    if band == "LOW":
        return "ALLOW", False
    if band == "CRITICAL":
        return "BLOCK", True
    # MEDIUM and HIGH
    return "HOLD_FOR_REVIEW", True
