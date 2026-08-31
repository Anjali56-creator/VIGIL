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

# Plain-English labels for the dashboard queue cards. Display only.
_SIGNAL_LABEL = {
    "AMT_DEV": "Unusual transaction amount", "VEL_1H": "Many payments in a short time",
    "AUTH_FAIL": "Multiple failed attempts", "DEV_NEW": "New device",
    "GEO_DIST": "Unusual location", "TIME_ODD": "Unusual time of day",
}
_RULE_LABEL = {
    "R_IMPOSSIBLE_TRAVEL": "Impossible travel",
    "R_MULTI_CUSTOMER_DEVICE": "Device shared with other customers",
    "R_AUTH_STORM": "Burst of failed logins",
}


def _why_flagged(assessment) -> list[str]:
    if not assessment:
        return []
    out: list[str] = []
    for s in sorted(assessment.signals, key=lambda x: x.contribution_pct, reverse=True):
        if s.triggered and s.code in _SIGNAL_LABEL:
            out.append(_SIGNAL_LABEL[s.code])
    for r in (assessment.rules_fired or []):
        lbl = _RULE_LABEL.get(r["code"])
        if lbl and lbl not in out:
            out.append(lbl)
    return out[:5]


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
    out: list[CaseSummary] = []
    for c in cases:
        s = CaseSummary.model_validate(c)
        txn = db.get(Transaction, c.transaction_id)
        if txn:
            s.amount_paise = txn.amount_paise
            s.city = txn.city
            s.method = txn.method
            s.merchant_name = txn.merchant_name
            s.why = _why_flagged(txn.assessment)
        out.append(s)
    return out


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
