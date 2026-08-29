"""Risk engine orchestrator: transaction -> signals -> fusion -> rules -> policy -> persisted assessment."""
from __future__ import annotations

import time
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import record
from ..models import AuthEvent, Customer, RiskAssessment, RiskSignal, Transaction
from . import fusion, rules, signals
from .policy import band_for, decide

ENGINE_VERSION = "1.0"


def _load_context(db: Session, txn: Transaction) -> signals.SignalContext:
    customer = db.get(Customer, txn.customer_id)
    history = list(
        db.scalars(
            select(Transaction)
            .where(Transaction.customer_id == txn.customer_id, Transaction.id != txn.id,
                   Transaction.created_at <= txn.created_at)
            .order_by(Transaction.created_at.desc())
            .limit(200)
        )
    )
    auth = list(
        db.scalars(
            select(AuthEvent)
            .where(AuthEvent.customer_id == txn.customer_id, AuthEvent.created_at <= txn.created_at)
            .order_by(AuthEvent.created_at.desc())
            .limit(100)
        )
    )
    return signals.SignalContext(txn=txn, customer=customer, history=history, auth_events=auth)


def _device_peers(db: Session, txn: Transaction) -> list[Transaction]:
    since = txn.created_at - timedelta(hours=24)
    return list(
        db.scalars(
            select(Transaction).where(
                Transaction.device_id == txn.device_id,
                Transaction.id != txn.id,
                Transaction.created_at >= since,
                Transaction.created_at <= txn.created_at,
            )
        )
    )


def assess(db: Session, txn: Transaction) -> RiskAssessment:
    t0 = time.perf_counter()
    ctx = _load_context(db, txn)
    sigs = signals.run_all(ctx)

    base_prob = fusion.fuse_score(sigs)
    base_score = round(100 * base_prob)
    shares = fusion.leave_one_out(sigs)

    hits = rules.evaluate(ctx, _device_peers(db, txn))
    floor = max((h.floor for h in hits), default=None)
    score = max(base_score, floor) if floor is not None else base_score
    score = max(0, min(100, score))
    band = band_for(score)

    rules_fired = [h.code for h in hits]
    p95 = ctx.customer.amount_p95_paise if ctx.customer else 0
    kyc = ctx.customer.kyc_tier if ctx.customer else 2
    action, needs_review = decide(band, rules_fired, txn.amount_paise, p95, kyc)

    latency_ms = int((time.perf_counter() - t0) * 1000)
    assessment = RiskAssessment(
        transaction_id=txn.id,
        score=score,
        band=band,
        base_score=base_score,
        engine_version=ENGINE_VERSION,
        rules_fired=[{"code": h.code, "floor": h.floor, "detail": h.detail} for h in hits],
        floor_applied=floor,
        recommended_action=action,
        requires_human_review=needs_review,
        latency_ms=latency_ms,
    )
    db.add(assessment)
    db.flush()

    for s in sigs:
        db.add(RiskSignal(
            assessment_id=assessment.id,
            code=s.code,
            family=s.family,
            raw_value=s.raw_value,
            baseline_value=s.baseline_value,
            unit=s.unit,
            normalized=round(s.normalized, 4),
            weight=s.weight,
            contribution_pct=shares.get(s.code, 0.0),
            triggered=s.triggered,
            explanation=s.explanation,
        ))

    record(db, entity_type="transaction", entity_id=txn.id, action="risk_assessed",
           detail={"score": score, "band": band, "base_score": base_score,
                   "rules_fired": rules_fired, "recommended_action": action})
    db.flush()
    return assessment
