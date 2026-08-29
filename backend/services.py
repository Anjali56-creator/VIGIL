"""Cross-cutting service functions shared by the API routers."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .audit import record
from .models import (
    AgentRun,
    AuditLog,
    AuthEvent,
    Case,
    CaseEvidence,
    Customer,
    Decision,
    RiskAssessment,
    Transaction,
)
from .schemas import RelatedEvent, TimelineEvent
from .util import now_ist, rupees

PRIORITY_BY_BAND = {"LOW": "LOW", "MEDIUM": "LOW", "HIGH": "HIGH", "CRITICAL": "CRITICAL"}


def next_case_id(db: Session) -> str:
    year = now_ist().year
    n = db.scalar(select(func.count()).select_from(Case)) or 0
    return f"CASE-{year}-{n + 1:04d}"


def open_case(db: Session, txn: Transaction, assessment: RiskAssessment, *,
              actor: str = "system") -> Case:
    existing = db.scalar(select(Case).where(Case.transaction_id == txn.id))
    if existing:
        return existing
    case = Case(
        id=next_case_id(db),
        transaction_id=txn.id,
        customer_id=txn.customer_id,
        status="NEW",
        priority=PRIORITY_BY_BAND.get(assessment.band, "MEDIUM"),
        score_at_open=assessment.score,
        band=assessment.band,
    )
    db.add(case)
    db.flush()
    record(db, entity_type="case", entity_id=case.id, action="case_opened", actor=actor,
           detail={"transaction_id": txn.id, "score": assessment.score, "band": assessment.band,
                   "trigger": "auto" if actor == "system" else "manual"})
    db.flush()
    return case


def related_events(db: Session, txn: Transaction, window_h: int = 48) -> list[RelatedEvent]:
    since = txn.created_at - timedelta(hours=window_h)
    out: list[RelatedEvent] = []
    seen: set[tuple[str, str]] = set()
    for link_type, col, val in (
        ("shared_device", Transaction.device_id, txn.device_id),
        ("shared_ip", Transaction.ip_addr, txn.ip_addr),
    ):
        if not val:
            continue
        rows = db.scalars(
            select(Transaction).where(
                col == val,
                Transaction.id != txn.id,
                Transaction.created_at >= since,
            ).order_by(Transaction.created_at.desc()).limit(25)
        )
        for r in rows:
            key = (link_type, r.id)
            if key in seen:
                continue
            seen.add(key)
            out.append(RelatedEvent(
                link_type=link_type, transaction_id=r.id, customer_id=r.customer_id,
                amount_paise=r.amount_paise, city=r.city, created_at=r.created_at,
            ))
    return out


def build_timeline(db: Session, case: Case) -> list[TimelineEvent]:
    txn = db.get(Transaction, case.transaction_id)
    ev: list[TimelineEvent] = []

    window_start = txn.created_at - timedelta(hours=2)
    auth = db.scalars(
        select(AuthEvent).where(
            AuthEvent.customer_id == case.customer_id,
            AuthEvent.created_at >= window_start,
            AuthEvent.created_at <= txn.created_at + timedelta(minutes=5),
        ).order_by(AuthEvent.created_at)
    )
    for a in auth:
        sev = "info" if a.success else "warn"
        verb = "succeeded" if a.success else "failed"
        ev.append(TimelineEvent(ts=a.created_at, kind="auth",
                                label=f"{a.type.replace('_', ' ').title()} {verb} (device {a.device_id or 'n/a'})",
                                severity=sev))

    prior = db.scalars(
        select(Transaction).where(
            Transaction.customer_id == case.customer_id,
            Transaction.created_at >= window_start,
            Transaction.created_at < txn.created_at,
        ).order_by(Transaction.created_at)
    )
    for p in prior:
        ev.append(TimelineEvent(ts=p.created_at, kind="txn",
                                label=f"Transaction {rupees(p.amount_paise)} in {p.city}", severity="info"))

    ev.append(TimelineEvent(ts=txn.created_at, kind="txn",
                            label=f"Transaction {rupees(txn.amount_paise)} in {txn.city} "
                                  f"via {txn.method.upper()} (device {txn.device_id})",
                            severity="danger"))

    a = db.scalar(select(RiskAssessment).where(RiskAssessment.transaction_id == txn.id))
    if a:
        ev.append(TimelineEvent(ts=a.created_at, kind="risk",
                                label=f"Risk engine flagged transaction: score {a.score} {a.band} "
                                      f"-> {a.recommended_action}",
                                severity="danger" if a.band in ("HIGH", "CRITICAL") else "warn"))
    ev.append(TimelineEvent(ts=case.opened_at, kind="risk", label=f"Case {case.id} opened", severity="warn"))

    for run in sorted(case.runs, key=lambda r: r.started_at):
        ev.append(TimelineEvent(ts=run.started_at, kind="agent",
                                label=f"AI investigation started ({run.mode})", severity="info"))
        if run.finished_at:
            ev.append(TimelineEvent(ts=run.finished_at, kind="agent",
                                    label=f"AI recommendation: {run.recommended_action} "
                                          f"(confidence {run.confidence:.0%}, "
                                          f"{run.claims_verified}/{run.claims_total} claims verified)",
                                    severity="info"))

    for d in sorted(case.decisions, key=lambda x: x.created_at):
        ev.append(TimelineEvent(ts=d.created_at, kind="analyst",
                                label=f"{d.actor} {d.decision} -> {d.final_action}"
                                      + (f" (override: {d.override_reason})" if d.override_reason else ""),
                                severity="warn" if d.decision == "OVERRIDE" else "info"))

    return sorted(ev, key=lambda e: e.ts)


_STATUS_BY_DECISION = {
    "APPROVE": "APPROVED",
    "REJECT": "BLOCKED",
    "OVERRIDE": None,      # resolved via final_action below
    "ESCALATE": "REVIEW_REQUIRED",
}


def apply_decision(db: Session, case: Case, *, decision: str, final_action: str,
                   override_reason: str | None, actor: str = "analyst@demo") -> Decision:
    txn = db.get(Transaction, case.transaction_id)
    assessment = db.scalar(select(RiskAssessment).where(RiskAssessment.transaction_id == txn.id))
    last_run = max(case.runs, key=lambda r: r.started_at, default=None)
    engine_action = assessment.recommended_action if assessment else ""
    ai_action = last_run.recommended_action if last_run else ""

    diverges = final_action != engine_action
    is_override = decision == "OVERRIDE" or diverges
    if is_override and not (override_reason and override_reason.strip()):
        raise ValueError(
            "override_reason is required when overriding or when the final action "
            "differs from the engine recommendation"
        )

    rec = Decision(
        case_id=case.id,
        actor=actor,
        decision=decision,
        engine_recommended_action=engine_action,
        ai_recommended_action=ai_action,
        final_action=final_action,
        override_reason=override_reason.strip() if override_reason else None,
    )
    db.add(rec)

    if decision == "OVERRIDE" or is_override:
        case.status = "BLOCKED" if final_action in ("BLOCK", "HOLD_FOR_REVIEW") else "APPROVED"
    else:
        case.status = _STATUS_BY_DECISION.get(decision) or case.status
    if case.status in ("APPROVED", "BLOCKED"):
        case.closed_at = now_ist()

    record(db, entity_type="case", entity_id=case.id, action="analyst_decision", actor=actor,
           detail={"decision": decision, "final_action": final_action,
                   "engine_recommended": engine_action, "ai_recommended": ai_action,
                   "override": is_override, "override_reason": override_reason,
                   "new_status": case.status})
    db.flush()
    return rec


def audit_for(db: Session, case: Case) -> list[AuditLog]:
    ids = {case.id, case.transaction_id}
    return list(
        db.scalars(
            select(AuditLog).where(AuditLog.entity_id.in_(ids)).order_by(AuditLog.created_at)
        )
    )
