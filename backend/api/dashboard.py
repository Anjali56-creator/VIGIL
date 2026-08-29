"""Risk-operations dashboard metrics."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Case, Decision, RiskAssessment, Transaction
from ..schemas import DashboardMetrics

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/metrics", response_model=DashboardMetrics)
def metrics(db: Session = Depends(get_db)) -> DashboardMetrics:
    total = db.scalar(select(func.count()).select_from(Transaction)) or 0
    analyzed = db.scalar(select(func.count()).select_from(RiskAssessment)) or 0

    dist: dict[str, int] = {"LOW": 0, "MEDIUM": 0, "HIGH": 0, "CRITICAL": 0}
    for band, n in db.execute(select(RiskAssessment.band, func.count()).group_by(RiskAssessment.band)):
        dist[band] = n
    suspicious = dist["MEDIUM"] + dist["HIGH"] + dist["CRITICAL"]

    review_required = db.scalar(
        select(func.count()).select_from(Case).where(Case.status == "REVIEW_REQUIRED")
    ) or 0

    # Detection rate: of ground-truth attack transactions, how many scored HIGH/CRITICAL.
    attacks = list(db.scalars(select(Transaction).where(Transaction.is_attack.is_(True))))
    detection_rate = None
    if attacks:
        caught = sum(1 for t in attacks if t.assessment and t.assessment.band in ("HIGH", "CRITICAL"))
        detection_rate = round(caught / len(attacks), 3)

    # Median analyst time-to-decision (case opened -> first decision).
    times: list[float] = []
    for case in db.scalars(select(Case)):
        if case.decisions:
            first = min(case.decisions, key=lambda d: d.created_at)
            times.append((first.created_at - case.opened_at).total_seconds())
    median_ttd = None
    if times:
        times.sort()
        mid = len(times) // 2
        median_ttd = round(times[mid] if len(times) % 2 else (times[mid - 1] + times[mid]) / 2, 1)

    return DashboardMetrics(
        total_transactions=total,
        analyzed=analyzed,
        suspicious=suspicious,
        high_risk=dist["HIGH"],
        critical=dist["CRITICAL"],
        review_required=review_required,
        detection_rate=detection_rate,
        median_time_to_decision_s=median_ttd,
        risk_distribution=dist,
    )
