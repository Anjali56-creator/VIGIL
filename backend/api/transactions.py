"""Transaction ingestion and listing."""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import Case, Customer, Transaction
from ..risk.engine import assess
from ..schemas import AssessmentOut, CustomerOut, IngestResult, TransactionIn, TransactionOut
from ..services import open_case
from ..util import now_ist, to_naive_ist

router = APIRouter(prefix="/api/transactions", tags=["transactions"])


class TransactionInBody(TransactionIn):
    id: str | None = None


class TransactionListItem(BaseModel):
    id: str
    customer_id: str
    amount_paise: int
    method: str
    city: str
    device_id: str
    created_at: datetime
    is_attack: bool
    scenario_label: str | None
    score: int | None
    band: str | None
    recommended_action: str | None


class TransactionDetail(BaseModel):
    transaction: TransactionOut
    assessment: AssessmentOut | None
    customer: CustomerOut | None
    case_id: str | None


@router.post("", response_model=IngestResult)
def ingest(body: TransactionInBody, db: Session = Depends(get_db)) -> IngestResult:
    if not db.get(Customer, body.customer_id):
        raise HTTPException(404, f"unknown customer {body.customer_id}")

    txn = Transaction(
        id=body.id or f"txn_{uuid.uuid4().hex[:12]}",
        customer_id=body.customer_id,
        amount_paise=body.amount_paise,
        method=body.method,
        merchant_name=body.merchant_name,
        merchant_mcc=body.merchant_mcc,
        device_id=body.device_id,
        ip_addr=body.ip_addr,
        ip_is_proxy=body.ip_is_proxy,
        city=body.city,
        lat=body.lat,
        lng=body.lng,
        created_at=to_naive_ist(body.created_at) or now_ist(),
        is_attack=body.is_attack,
        scenario_label=body.scenario_label,
    )
    db.add(txn)
    db.flush()

    assessment = assess(db, txn)
    case_id = None
    if assessment.score >= settings.case_open_threshold:
        case_id = open_case(db, txn, assessment).id
    db.commit()
    db.refresh(assessment)
    db.refresh(txn)

    return IngestResult(
        transaction=TransactionOut.model_validate(txn),
        assessment=AssessmentOut.model_validate(assessment),
        case_id=case_id,
    )


@router.get("", response_model=list[TransactionListItem])
def list_transactions(
    db: Session = Depends(get_db),
    band: str | None = Query(None),
    customer_id: str | None = Query(None),
    suspicious_only: bool = Query(False),
    limit: int = Query(100, le=500),
):
    stmt = select(Transaction).order_by(Transaction.created_at.desc()).limit(limit)
    if customer_id:
        stmt = stmt.where(Transaction.customer_id == customer_id)
    out: list[TransactionListItem] = []
    for t in db.scalars(stmt):
        a = t.assessment
        if band and (not a or a.band != band):
            continue
        if suspicious_only and (not a or a.score < 30):
            continue
        out.append(TransactionListItem(
            id=t.id, customer_id=t.customer_id, amount_paise=t.amount_paise,
            method=t.method, city=t.city, device_id=t.device_id,
            created_at=t.created_at, is_attack=t.is_attack, scenario_label=t.scenario_label,
            score=a.score if a else None, band=a.band if a else None,
            recommended_action=a.recommended_action if a else None,
        ))
    return out


@router.get("/{txn_id}", response_model=TransactionDetail)
def get_transaction(txn_id: str, db: Session = Depends(get_db)):
    t = db.get(Transaction, txn_id)
    if not t:
        raise HTTPException(404, "transaction not found")
    case = db.scalar(select(Case).where(Case.transaction_id == txn_id))
    return TransactionDetail(
        transaction=TransactionOut.model_validate(t),
        assessment=AssessmentOut.model_validate(t.assessment) if t.assessment else None,
        customer=CustomerOut.model_validate(t.customer) if t.customer else None,
        case_id=case.id if case else None,
    )
