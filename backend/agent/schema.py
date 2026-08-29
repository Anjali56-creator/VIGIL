"""Structured output contract for the investigation agent.

Deliberately has NO numeric 0-100 score field - the agent cannot emit a score.
`recommended_action` here is advisory; policy.py computes the binding one.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

RISK_VIEWS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
AGENT_ACTIONS = ("ALLOW", "MONITOR", "STEP_UP_AUTH", "HOLD_FOR_REVIEW", "BLOCK")

# Metrics the grounding validator can independently re-verify against the database.
VERIFIABLE_METRICS = (
    "amount_paise",
    "amount_median_paise",
    "amount_multiple",
    "auth_failures_window",
    "velocity_1h",
    "geo_distance_km",
    "new_device",
    "related_customers_device",
    "impossible_travel_speed_kmh",
)


class Finding(BaseModel):
    title: str = Field(description="Short claim, e.g. 'Amount 6.4x the customer median'")
    detail: str = Field(description="One or two sentences explaining the finding")
    evidence_refs: list[str] = Field(
        default_factory=list,
        description="Evidence ledger ids that support this finding, e.g. ['EV-002','EV-005']. "
                    "Only ids returned by tools may be cited.",
    )
    metric: str | None = Field(
        default=None,
        description=f"If quantitative, one of: {', '.join(VERIFIABLE_METRICS)}. Otherwise null.",
    )
    observed: float | None = Field(default=None, description="Observed numeric value for `metric`")
    baseline: float | None = Field(default=None, description="Baseline / expected value for `metric`")
    unit: str = Field(default="")
    supports_risk: bool = Field(default=True, description="True if this finding raises suspicion")
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)


class Investigation(BaseModel):
    investigation_summary: str = Field(description="3-5 sentence narrative of what happened")
    findings: list[Finding] = Field(default_factory=list)
    behavioral_deviation: str = Field(description="How current behaviour compares with this customer's history")
    related_activity: str | None = Field(
        default=None, description="Cross-account / shared-device / ring observations, or null if none",
    )
    agent_risk_view: str = Field(description=f"One of {RISK_VIEWS}")
    concurs_with_engine: bool = Field(description="Does the agent agree with the engine's band?")
    dissent_reason: str | None = Field(
        default=None, description="If concurs_with_engine is false, why the agent disagrees",
    )
    recommended_action: str = Field(description=f"Advisory. One of {AGENT_ACTIONS}")
    confidence: float = Field(ge=0.0, le=1.0)
    requires_human_review: bool = Field(default=True)


def submit_tool_schema() -> dict:
    """JSON schema for the forced `submit_investigation` tool call."""
    schema = Investigation.model_json_schema()
    return {
        "name": "submit_investigation",
        "description": "Submit the final structured investigation result. Call this exactly once, "
                       "after you have gathered evidence with the read tools.",
        "input_schema": schema,
    }
