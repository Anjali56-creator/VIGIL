"""Trigger and read AI investigations."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..agent.runner import investigate
from ..db import get_db
from ..models import AgentRun, Case
from ..schemas import RunOut

router = APIRouter(prefix="/api", tags=["investigations"])


@router.post("/cases/{case_id}/investigate", response_model=RunOut)
def start_investigation(case_id: str, db: Session = Depends(get_db)):
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404, "case not found")
    if case.status in ("APPROVED", "BLOCKED", "RESOLVED"):
        raise HTTPException(409, f"case is {case.status}; cannot investigate")
    run = investigate(db, case)
    db.commit()
    db.refresh(run)
    return RunOut.model_validate(run)


@router.get("/runs/{run_id}", response_model=RunOut)
def get_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(AgentRun, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return RunOut.model_validate(run)
