"""Case queue and case detail aggregate."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Case, RiskAssessment, Transaction
from ..schemas import (
    AssessmentOut,
    AuditOut,
    CaseDetail,
    CaseSummary,
    CustomerOut,
    DecisionOut,
    EvidenceOut,
    RunOut,
    TransactionOut,
)
from ..services import audit_for, build_timeline, open_case, related_events

router = APIRouter(prefix="/api/cases", tags=["cases"])

_PRIORITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}


class OpenCaseBody(BaseModel):
    transaction_id: str


@router.get("", response_model=list[CaseSummary])
def list_cases(db: Session = Depends(get_db), status: str | None = Query(None),
               band: str | None = Query(None)):
    stmt = select(Case)
    if status:
        stmt = stmt.where(Case.status == status)
    if band:
        stmt = stmt.where(Case.band == band)
    cases = sorted(
        db.scalars(stmt),
        key=lambda c: (_PRIORITY_ORDER.get(c.priority, 9), -c.opened_at.timestamp()),
    )
    return [CaseSummary.model_validate(c) for c in cases]


@router.post("", response_model=CaseSummary)
def open_case_manual(body: OpenCaseBody, db: Session = Depends(get_db)):
    txn = db.get(Transaction, body.transaction_id)
    if not txn:
        raise HTTPException(404, "transaction not found")
    a = db.scalar(select(RiskAssessment).where(RiskAssessment.transaction_id == txn.id))
    if not a:
        raise HTTPException(409, "transaction has no risk assessment")
    case = open_case(db, txn, a, actor="analyst@demo")
    db.commit()
    return CaseSummary.model_validate(case)


@router.get("/{case_id}", response_model=CaseDetail)
def case_detail(case_id: str, db: Session = Depends(get_db)):
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404, "case not found")
    txn = db.get(Transaction, case.transaction_id)
    assessment = db.scalar(select(RiskAssessment).where(RiskAssessment.transaction_id == txn.id))

    return CaseDetail(
        case=CaseSummary.model_validate(case),
        transaction=TransactionOut.model_validate(txn),
        customer=CustomerOut.model_validate(txn.customer),
        assessment=AssessmentOut.model_validate(assessment),
        timeline=build_timeline(db, case),
        related_events=related_events(db, txn),
        evidence=[EvidenceOut.model_validate(e) for e in sorted(case.evidence, key=lambda x: x.id)],
        runs=[RunOut.model_validate(r) for r in sorted(case.runs, key=lambda r: r.started_at)],
        decisions=[DecisionOut.model_validate(d) for d in sorted(case.decisions, key=lambda d: d.created_at)],
        audit=[AuditOut.model_validate(a) for a in audit_for(db, case)],
    )


@router.patch("/{case_id}/status", response_model=CaseSummary)
def set_status(case_id: str, new_status: str = Query(...), db: Session = Depends(get_db)):
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404, "case not found")
    case.status = new_status
    db.commit()
    return CaseSummary.model_validate(case)
