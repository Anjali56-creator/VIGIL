"""Analyst decisions (human-in-the-loop)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Case
from ..schemas import AuditOut, DecisionIn, DecisionOut
from ..services import apply_decision, audit_for

router = APIRouter(prefix="/api/cases", tags=["decisions"])


@router.post("/{case_id}/decision", response_model=DecisionOut)
def decide(case_id: str, body: DecisionIn, db: Session = Depends(get_db)):
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404, "case not found")
    try:
        rec = apply_decision(
            db, case, decision=body.decision, final_action=body.final_action,
            override_reason=body.override_reason, actor="analyst@demo",
        )
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    db.commit()
    db.refresh(rec)
    return DecisionOut.model_validate(rec)


@router.get("/{case_id}/audit", response_model=list[AuditOut])
def case_audit(case_id: str, db: Session = Depends(get_db)):
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404, "case not found")
    return [AuditOut.model_validate(a) for a in audit_for(db, case)]
