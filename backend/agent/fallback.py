"""Deterministic engine-only investigation.

Used when no LLM provider is configured, or the live model errors / times out.
Produces the same `Investigation` shape from the engine's own signals and the
pre-gathered read-only evidence, so the persistence, grounding-validation and UI
paths are identical - just clearly badged mode="fallback" / model="engine-only".
"""
from __future__ import annotations

from ..models import Customer, RiskAssessment, Transaction
from .schema import Finding, Investigation
from .tools import ToolResult

_METRIC_FOR = {
    "AMT_DEV": ("amount_paise", "paise"),
    "GEO_DIST": ("geo_distance_km", "km"),
    "AUTH_FAIL": ("auth_failures_window", "failures/10min"),
    "VEL_1H": ("velocity_1h", "txns/hour"),
    "DEV_NEW": ("new_device", "bool"),
}


def build_from(
    assessment: RiskAssessment,
    txn: Transaction,
    customer: Customer,
    tool_results: dict[str, ToolResult],
) -> tuple[Investigation, list]:
    """Build a deterministic Investigation from an already-scored assessment and
    the evidence already collected by the 5 read-only tools. No DB access, no LLM."""
    a = assessment
    ring = tool_results["find_related_events"]
    evidence = [ev for res in tool_results.values() for ev in res.evidence]

    findings: list[Finding] = []
    triggered = sorted(
        (s for s in a.signals if s.triggered),
        key=lambda s: s.contribution_pct, reverse=True,
    )
    for s in triggered:
        metric, unit = _METRIC_FOR.get(s.code, (None, ""))
        findings.append(Finding(
            title=f"{s.code}: {s.explanation.split('.')[0]}",
            detail=s.explanation,
            evidence_refs=[],
            metric=metric,
            observed=s.raw_value if metric else None,
            baseline=s.baseline_value if metric and s.baseline_value else None,
            unit=unit,
            supports_risk=True,
            confidence=min(0.95, 0.5 + s.normalized / 2),
        ))

    ring_customers = ring.data.get("distinct_other_customers", 0)
    related = None
    if ring_customers >= 1:
        related = (f"Device {txn.device_id} was seen on {ring_customers} other customer(s) in the "
                   f"last 48h across {ring.data.get('related_txn_count', 0)} transactions.")
        findings.append(Finding(
            title=f"Shared device across {ring_customers} other customers",
            detail=related, evidence_refs=[], metric="related_customers_device",
            observed=float(ring_customers), baseline=0.0, unit="customers",
            supports_risk=True, confidence=0.8,
        ))

    band = a.band
    summary = (
        f"Engine-only assessment. Transaction {txn.amount_paise} paise in {txn.city} scored "
        f"{a.score}/100 ({band}). {len(triggered)} signals triggered"
        + (f"; rules fired: {', '.join(r['code'] for r in a.rules_fired)}." if a.rules_fired else ".")
    )
    dev = (f"Customer median is {customer.amount_median_paise} paise over {customer.txn_count} "
           f"transactions; this transaction is "
           f"{txn.amount_paise / max(customer.amount_median_paise, 1):.1f}x that.")

    # -------- deterministic fallback escalation (seeded dissent) --------
    # The engine scores one transaction at a time. When the evidence shows the
    # same device operating across several other customers, that is a ring, and
    # the correct response is broader than "hold this transaction". This is a
    # fixed rule in the fallback investigator - NOT an LLM opinion - and the UI
    # labels it as such. A live agent forms the same judgement itself.
    engine_action = a.recommended_action
    escalatable = {"MONITOR", "STEP_UP_AUTH", "HOLD_FOR_REVIEW"}
    concurs, dissent_reason, agent_action, agent_view = True, None, engine_action, band
    if ring_customers >= 2 and engine_action in escalatable:
        concurs = False
        agent_action = "BLOCK"
        agent_view = "CRITICAL" if band != "CRITICAL" else band
        dissent_reason = (
            f"The engine assessed this as a single transaction and recommended {engine_action}. "
            f"The evidence shows an active fraud ring: device {txn.device_id} authorised "
            f"{ring.data.get('related_txn_count', 0)} transactions across {ring_customers} other "
            f"customers in the last hour. The proportionate response is to BLOCK the device, not "
            f"to hold one transaction for review."
        )
        summary += (" Escalation: shared-device activity indicates a ring broader than this "
                    "single transaction.")

    inv = Investigation(
        investigation_summary=summary,
        findings=findings,
        behavioral_deviation=dev,
        related_activity=related,
        agent_risk_view=agent_view,
        concurs_with_engine=concurs,
        dissent_reason=dissent_reason,
        recommended_action=agent_action,
        confidence=0.6 if not concurs else 0.55,
        requires_human_review=True,
    )
    return inv, evidence
