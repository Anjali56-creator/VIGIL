"""SQLAlchemy ORM models. Money is stored as integer paise everywhere - never float."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .util import now_ist


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    email_masked: Mapped[str] = mapped_column(String)
    kyc_tier: Mapped[int] = mapped_column(Integer, default=2)
    segment: Mapped[str] = mapped_column(String, default="retail")
    account_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    home_city: Mapped[str] = mapped_column(String)
    home_lat: Mapped[float] = mapped_column(Float)
    home_lng: Mapped[float] = mapped_column(Float)

    # Behavioural baseline (computed at seed time, read by the risk engine).
    txn_count: Mapped[int] = mapped_column(Integer, default=0)
    amount_median_paise: Mapped[int] = mapped_column(Integer, default=0)
    amount_mad_paise: Mapped[int] = mapped_column(Integer, default=0)
    amount_p95_paise: Mapped[int] = mapped_column(Integer, default=0)
    mean_hourly_txns: Mapped[float] = mapped_column(Float, default=0.05)
    active_hour_start: Mapped[int] = mapped_column(Integer, default=8)
    active_hour_end: Mapped[int] = mapped_column(Integer, default=22)
    known_device_ids: Mapped[list] = mapped_column(JSON, default=list)
    geo_radius_km: Mapped[float] = mapped_column(Float, default=25.0)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=True)

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="customer")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    amount_paise: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String, default="INR")
    method: Mapped[str] = mapped_column(String, default="upi")  # upi | card | netbanking
    status: Mapped[str] = mapped_column(String, default="captured")
    merchant_name: Mapped[str] = mapped_column(String, default="")
    merchant_mcc: Mapped[str] = mapped_column(String, default="")

    device_id: Mapped[str] = mapped_column(String, index=True)
    ip_addr: Mapped[str] = mapped_column(String, default="")
    ip_is_proxy: Mapped[bool] = mapped_column(Boolean, default=False)
    city: Mapped[str] = mapped_column(String)
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_ist, index=True)

    # Ground truth - used only for detection metrics, never shown to the engine or agent.
    is_attack: Mapped[bool] = mapped_column(Boolean, default=False)
    scenario_label: Mapped[str | None] = mapped_column(String, nullable=True)

    customer: Mapped["Customer"] = relationship(back_populates="transactions")
    assessment: Mapped["RiskAssessment"] = relationship(back_populates="transaction", uselist=False)


class AuthEvent(Base):
    __tablename__ = "auth_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    device_id: Mapped[str] = mapped_column(String, default="")
    ip_addr: Mapped[str] = mapped_column(String, default="")
    type: Mapped[str] = mapped_column(String)  # LOGIN | OTP_FAIL | PWD_FAIL | PAYMENT_DECLINE | CVV_FAIL
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_ist, index=True)


class RiskAssessment(Base):
    __tablename__ = "risk_assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transaction_id: Mapped[str] = mapped_column(ForeignKey("transactions.id"), index=True)
    score: Mapped[int] = mapped_column(Integer)
    band: Mapped[str] = mapped_column(String)  # LOW | MEDIUM | HIGH | CRITICAL
    base_score: Mapped[int] = mapped_column(Integer, default=0)  # score before rule floors
    engine_version: Mapped[str] = mapped_column(String, default="1.0")
    rules_fired: Mapped[list] = mapped_column(JSON, default=list)
    floor_applied: Mapped[int | None] = mapped_column(Integer, nullable=True)
    floor_rule: Mapped[str | None] = mapped_column(String, nullable=True)  # code of the governing rule
    recommended_action: Mapped[str] = mapped_column(String)
    requires_human_review: Mapped[bool] = mapped_column(Boolean, default=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_ist)

    transaction: Mapped["Transaction"] = relationship(back_populates="assessment")
    signals: Mapped[list["RiskSignal"]] = relationship(back_populates="assessment", cascade="all, delete-orphan")


class RiskSignal(Base):
    __tablename__ = "risk_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    assessment_id: Mapped[int] = mapped_column(ForeignKey("risk_assessments.id"), index=True)
    code: Mapped[str] = mapped_column(String)
    family: Mapped[str] = mapped_column(String)
    raw_value: Mapped[float] = mapped_column(Float)
    baseline_value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String)
    normalized: Mapped[float] = mapped_column(Float)          # 0..1 severity
    weight: Mapped[float] = mapped_column(Float)
    contribution_pct: Mapped[float] = mapped_column(Float)    # leave-one-out attribution share
    triggered: Mapped[bool] = mapped_column(Boolean, default=False)
    explanation: Mapped[str] = mapped_column(Text)

    assessment: Mapped["RiskAssessment"] = relationship(back_populates="signals")


class Case(Base):
    __tablename__ = "cases"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # CASE-2026-0001
    transaction_id: Mapped[str] = mapped_column(ForeignKey("transactions.id"), index=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    status: Mapped[str] = mapped_column(String, default="NEW")
    # NEW | INVESTIGATING | REVIEW_REQUIRED | APPROVED | BLOCKED | RESOLVED
    priority: Mapped[str] = mapped_column(String, default="MEDIUM")
    score_at_open: Mapped[int] = mapped_column(Integer)
    band: Mapped[str] = mapped_column(String)
    assigned_to: Mapped[str] = mapped_column(String, default="analyst@demo")
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_ist)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    runs: Mapped[list["AgentRun"]] = relationship(back_populates="case", cascade="all, delete-orphan")
    evidence: Mapped[list["CaseEvidence"]] = relationship(back_populates="case", cascade="all, delete-orphan")
    decisions: Mapped[list["Decision"]] = relationship(back_populates="case", cascade="all, delete-orphan")


class CaseEvidence(Base):
    """One row per data point the agent retrieved. Written BEFORE findings so the
    agent can only cite ids that already exist. This is the anti-hallucination core."""
    __tablename__ = "case_evidence"

    # EV-nnn, numbered per case. The id is only unique within a case, so the
    # primary key is composite (case_id, id) - two cases can both have an EV-001.
    id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), primary_key=True, index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id"))
    source_tool: Mapped[str] = mapped_column(String)
    source_ref: Mapped[str] = mapped_column(String, default="")
    metric: Mapped[str] = mapped_column(String)
    observed_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str] = mapped_column(String, default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verification_note: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_ist)

    case: Mapped["Case"] = relationship(back_populates="evidence")


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    status: Mapped[str] = mapped_column(String, default="RUNNING")  # RUNNING | COMPLETED | FALLBACK | FAILED
    mode: Mapped[str] = mapped_column(String, default="llm")        # llm | fallback
    model: Mapped[str] = mapped_column(String, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_ist)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer, default=0)

    summary: Mapped[str] = mapped_column(Text, default="")
    behavioral_deviation: Mapped[str] = mapped_column(Text, default="")
    related_activity: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_risk_view: Mapped[str] = mapped_column(String, default="")
    concurs_with_engine: Mapped[bool] = mapped_column(Boolean, default=True)
    dissent_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    recommended_action: Mapped[str] = mapped_column(String, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    requires_human_review: Mapped[bool] = mapped_column(Boolean, default=True)

    grounding_verdict: Mapped[str] = mapped_column(String, default="")  # PASS | PARTIAL | FAIL
    claims_verified: Mapped[int] = mapped_column(Integer, default=0)
    claims_total: Mapped[int] = mapped_column(Integer, default=0)
    failure_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    tool_log: Mapped[list] = mapped_column(JSON, default=list)  # [{seq,tool,args,summary,ms}]

    case: Mapped["Case"] = relationship(back_populates="runs")
    findings: Mapped[list["AgentFinding"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class AgentFinding(Base):
    __tablename__ = "agent_findings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    title: Mapped[str] = mapped_column(String)
    detail: Mapped[str] = mapped_column(Text)
    evidence_refs: Mapped[list] = mapped_column(JSON, default=list)
    metric: Mapped[str | None] = mapped_column(String, nullable=True)
    observed: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str] = mapped_column(String, default="")
    supports_risk: Mapped[bool] = mapped_column(Boolean, default=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    validation_status: Mapped[str] = mapped_column(String, default="UNVERIFIED")  # VERIFIED | UNVERIFIED | REFUTED

    run: Mapped["AgentRun"] = relationship(back_populates="findings")


class Decision(Base):
    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    actor: Mapped[str] = mapped_column(String, default="analyst@demo")
    decision: Mapped[str] = mapped_column(String)  # APPROVE | REJECT | OVERRIDE | ESCALATE
    engine_recommended_action: Mapped[str] = mapped_column(String, default="")
    ai_recommended_action: Mapped[str] = mapped_column(String, default="")
    final_action: Mapped[str] = mapped_column(String)
    override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_ist)

    case: Mapped["Case"] = relationship(back_populates="decisions")


class AuditLog(Base):
    """Append-only. No code path issues UPDATE or DELETE against this table."""
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String)
    entity_id: Mapped[str] = mapped_column(String, index=True)
    actor: Mapped[str] = mapped_column(String, default="system")
    action: Mapped[str] = mapped_column(String)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_ist, index=True)


Index("ix_txn_device_created", Transaction.device_id, Transaction.created_at)
Index("ix_txn_customer_created", Transaction.customer_id, Transaction.created_at)
Index("ix_auth_customer_created", AuthEvent.customer_id, AuthEvent.created_at)
Index("ix_cases_status_priority", Case.status, Case.priority)
