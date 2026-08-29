"""Pydantic request/response contracts for the HTTP API."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

Method = str  # "upi" | "card" | "netbanking"


class HealthOut(BaseModel):
    status: str
    db: bool
    llm_configured: bool
    model: str


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- ingestion ----------
class TransactionIn(BaseModel):
    customer_id: str
    amount_paise: int = Field(gt=0)
    method: Method = "upi"
    merchant_name: str = ""
    merchant_mcc: str = ""
    device_id: str
    ip_addr: str = ""
    ip_is_proxy: bool = False
    city: str
    lat: float
    lng: float
    created_at: datetime | None = None
    # optional ground-truth tags for seeded/simulated data
    is_attack: bool = False
    scenario_label: str | None = None


class SignalOut(ORMModel):
    code: str
    family: str
    raw_value: float
    baseline_value: float
    unit: str
    normalized: float
    weight: float
    contribution_pct: float
    triggered: bool
    explanation: str


class AssessmentOut(ORMModel):
    transaction_id: str
    score: int
    band: str
    base_score: int
    rules_fired: list
    floor_applied: int | None
    recommended_action: str
    requires_human_review: bool
    latency_ms: int
    created_at: datetime
    signals: list[SignalOut]


class TransactionOut(ORMModel):
    id: str
    customer_id: str
    amount_paise: int
    currency: str
    method: str
    status: str
    merchant_name: str
    device_id: str
    ip_addr: str
    city: str
    created_at: datetime
    is_attack: bool
    scenario_label: str | None


class IngestResult(BaseModel):
    transaction: TransactionOut
    assessment: AssessmentOut
    case_id: str | None


# ---------- cases ----------
class DecisionIn(BaseModel):
    decision: str  # APPROVE | REJECT | OVERRIDE | ESCALATE
    final_action: str
    override_reason: str | None = None


class DecisionOut(ORMModel):
    id: int
    actor: str
    decision: str
    engine_recommended_action: str
    ai_recommended_action: str
    final_action: str
    override_reason: str | None
    created_at: datetime


class FindingOut(ORMModel):
    title: str
    detail: str
    evidence_refs: list
    metric: str | None
    observed: float | None
    baseline: float | None
    unit: str
    supports_risk: bool
    confidence: float
    validation_status: str


class EvidenceOut(ORMModel):
    id: str
    source_tool: str
    source_ref: str
    metric: str
    observed_value: float | None
    baseline_value: float | None
    unit: str
    detail: str
    verified: bool
    verification_note: str


class RunOut(ORMModel):
    id: int
    status: str
    mode: str
    model: str
    latency_ms: int
    tool_call_count: int
    input_tokens: int
    output_tokens: int
    summary: str
    behavioral_deviation: str
    related_activity: str | None
    agent_risk_view: str
    concurs_with_engine: bool
    dissent_reason: str | None
    recommended_action: str
    confidence: float
    requires_human_review: bool
    grounding_verdict: str
    claims_verified: int
    claims_total: int
    failure_reason: str | None
    tool_log: list
    findings: list[FindingOut]
    started_at: datetime
    finished_at: datetime | None


class TimelineEvent(BaseModel):
    ts: datetime
    kind: str  # auth | txn | risk | agent | analyst
    label: str
    severity: str = "info"  # info | warn | danger


class CaseSummary(ORMModel):
    id: str
    transaction_id: str
    customer_id: str
    status: str
    priority: str
    score_at_open: int
    band: str
    assigned_to: str
    opened_at: datetime
    closed_at: datetime | None


class CustomerOut(ORMModel):
    id: str
    name: str
    email_masked: str
    kyc_tier: int
    segment: str
    account_created_at: datetime
    home_city: str
    txn_count: int
    amount_median_paise: int
    amount_p95_paise: int
    active_hour_start: int
    active_hour_end: int
    known_device_ids: list
    geo_radius_km: float


class RelatedEvent(BaseModel):
    link_type: str
    transaction_id: str
    customer_id: str
    amount_paise: int
    city: str
    created_at: datetime


class CaseDetail(BaseModel):
    case: CaseSummary
    transaction: TransactionOut
    customer: CustomerOut
    assessment: AssessmentOut
    timeline: list[TimelineEvent]
    related_events: list[RelatedEvent]
    evidence: list[EvidenceOut]
    runs: list[RunOut]
    decisions: list[DecisionOut]
    audit: list["AuditOut"]


class AuditOut(ORMModel):
    id: int
    entity_type: str
    entity_id: str
    actor: str
    action: str
    detail: dict
    created_at: datetime


# ---------- dashboard ----------
class DashboardMetrics(BaseModel):
    total_transactions: int
    analyzed: int
    suspicious: int
    high_risk: int
    critical: int
    review_required: int
    detection_rate: float | None
    median_time_to_decision_s: float | None
    risk_distribution: dict[str, int]


class SimulateIn(BaseModel):
    scenario: str = "account_takeover"


CaseDetail.model_rebuild()
