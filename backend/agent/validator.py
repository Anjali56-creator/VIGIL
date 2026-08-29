"""Grounding validation for agent findings.

Four gates:
  1. Reference integrity - every evidence_ref must resolve to an id in this run's ledger.
  2. Numeric re-verification - recognized metrics are recomputed from the database
     and compared to the agent's claimed numbers (tolerance +-1%).
  3. (caller) one repair attempt on failure.
  4. Fail visible - unverifiable / refuted findings are persisted with a status the
     UI renders with a warning, never silently dropped.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import AuthEvent, Customer, Transaction
from ..util import haversine_km
from .schema import Finding, Investigation

TOLERANCE = 0.01
_FAIL_TYPES = {"OTP_FAIL", "PWD_FAIL", "CVV_FAIL", "PAYMENT_DECLINE"}


@dataclass
class FindingVerdict:
    finding: Finding
    status: str  # VERIFIED | UNVERIFIED | REFUTED
    note: str


@dataclass
class GroundingResult:
    verdict: str  # PASS | PARTIAL | FAIL
    claims_total: int
    claims_verified: int
    per_finding: list[FindingVerdict]
    violations: list[str]


def _truth(db: Session, txn: Transaction, customer: Customer, metric: str) -> float | None:
    if metric == "amount_paise":
        return float(txn.amount_paise)
    if metric == "amount_median_paise":
        return float(customer.amount_median_paise)
    if metric == "amount_multiple":
        return round(txn.amount_paise / max(customer.amount_median_paise, 1), 2)
    if metric == "new_device":
        return 0.0 if txn.device_id in (customer.known_device_ids or []) else 1.0
    if metric == "geo_distance_km":
        return round(haversine_km(txn.lat, txn.lng, customer.home_lat, customer.home_lng), 1)
    if metric == "velocity_1h":
        since = txn.created_at - timedelta(hours=1)
        return float(db.scalar(
            select(func.count()).select_from(Transaction).where(
                Transaction.customer_id == customer.id, Transaction.id != txn.id,
                Transaction.created_at >= since, Transaction.created_at <= txn.created_at,
            )
        ) or 0)
    if metric == "auth_failures_window":
        since = txn.created_at - timedelta(minutes=10)
        rows = db.scalars(select(AuthEvent).where(
            AuthEvent.customer_id == customer.id,
            AuthEvent.created_at >= since,
            AuthEvent.created_at <= txn.created_at,
        ))
        return float(sum(1 for e in rows if not e.success and e.type in _FAIL_TYPES))
    if metric in ("related_customers_device", "related_customers_ip"):
        col = Transaction.ip_addr if metric.endswith("ip") else Transaction.device_id
        val = txn.ip_addr if metric.endswith("ip") else txn.device_id
        since = txn.created_at - timedelta(hours=48)
        rows = db.scalars(select(Transaction).where(
            col == val, Transaction.id != txn.id,
            Transaction.created_at >= since, Transaction.created_at <= txn.created_at,
        ))
        return float(len({r.customer_id for r in rows if r.customer_id != customer.id}))
    if metric == "impossible_travel_speed_kmh":
        prev = db.scalars(select(Transaction).where(
            Transaction.customer_id == customer.id, Transaction.id != txn.id,
            Transaction.created_at < txn.created_at,
            Transaction.created_at >= txn.created_at - timedelta(hours=8),
        ).order_by(Transaction.created_at.desc()))
        best = 0.0
        for p in prev:
            dt_h = (txn.created_at - p.created_at).total_seconds() / 3600.0
            if dt_h <= 0:
                continue
            dist = haversine_km(txn.lat, txn.lng, p.lat, p.lng)
            if dist >= 200:
                best = max(best, dist / dt_h)
        return round(best, 0) if best else None
    return None


def _close(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return False
    scale = max(abs(a), abs(b), 1.0)
    return abs(a - b) / scale <= TOLERANCE


def validate(db: Session, txn: Transaction, customer: Customer, inv: Investigation,
             ledger_ids: set[str]) -> GroundingResult:
    per: list[FindingVerdict] = []
    violations: list[str] = []
    claims_total = 0
    claims_verified = 0

    for f in inv.findings:
        bad_refs = [r for r in f.evidence_refs if r not in ledger_ids]
        if bad_refs:
            msg = f"Finding '{f.title}' cites unknown evidence id(s): {bad_refs}"
            violations.append(msg)
            per.append(FindingVerdict(f, "REFUTED", msg))
            continue

        if not f.metric:
            note = "Qualitative finding; not independently re-verified." if f.evidence_refs \
                else "No evidence cited."
            if not f.evidence_refs:
                violations.append(f"Finding '{f.title}' cites no evidence.")
            per.append(FindingVerdict(f, "UNVERIFIED", note))
            continue

        claims_total += 1
        truth = _truth(db, txn, customer, f.metric)
        if truth is None:
            per.append(FindingVerdict(f, "UNVERIFIED", f"Metric '{f.metric}' could not be recomputed."))
            continue
        if _close(f.observed, truth):
            claims_verified += 1
            per.append(FindingVerdict(f, "VERIFIED", f"{f.metric}={truth} confirmed against database."))
        else:
            msg = f"Finding '{f.title}': claimed {f.metric}={f.observed}, database says {truth}."
            violations.append(msg)
            per.append(FindingVerdict(f, "REFUTED", msg))

    if claims_total == 0:
        verdict = "PASS" if not violations else "FAIL"
    elif claims_verified == claims_total and not violations:
        verdict = "PASS"
    elif claims_verified == 0:
        verdict = "FAIL"
    else:
        verdict = "PARTIAL"

    return GroundingResult(verdict, claims_total, claims_verified, per, violations)
