"""Read-only tools available to the investigation agent.

Every tool only reads. There is no tool that writes, blocks, refunds, or mutates
anything - the agent is architecturally incapable of taking a financial action.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AuthEvent, Customer, RiskAssessment, Transaction
from ..util import haversine_km, now_ist


@dataclass
class EvidenceItem:
    metric: str
    observed_value: float | None
    baseline_value: float | None
    unit: str
    detail: str
    source_ref: str = ""


@dataclass
class ToolResult:
    data: dict
    evidence: list[EvidenceItem] = field(default_factory=list)


@dataclass
class ToolContext:
    db: Session
    txn: Transaction
    customer: Customer


_FAIL_TYPES = {"OTP_FAIL", "PWD_FAIL", "CVV_FAIL", "PAYMENT_DECLINE"}

_SIGNAL_METRIC = {
    "AMT_DEV": ("amount_paise", "paise"),
    "GEO_DIST": ("geo_distance_km", "km"),
    "AUTH_FAIL": ("auth_failures_window", "failures/10min"),
    "VEL_1H": ("velocity_1h", "txns/hour"),
    "DEV_NEW": ("new_device", "bool"),
}


def _history(ctx: ToolContext, days: int, limit: int = 500) -> list[Transaction]:
    since = ctx.txn.created_at - timedelta(days=days)
    return list(ctx.db.scalars(
        select(Transaction).where(
            Transaction.customer_id == ctx.customer.id,
            Transaction.id != ctx.txn.id,
            Transaction.created_at <= ctx.txn.created_at,
            Transaction.created_at >= since,
        ).order_by(Transaction.created_at.desc()).limit(limit)
    ))


# --------------------------------------------------------------------------- tools
def get_customer_profile(ctx: ToolContext) -> ToolResult:
    c = ctx.customer
    age_days = (now_ist() - c.account_created_at).days
    data = {
        "customer_id": c.id,
        "name_masked": c.name[:1] + "***",
        "kyc_tier": c.kyc_tier,
        "segment": c.segment,
        "account_age_days": age_days,
        "home_city": c.home_city,
        "historical_txn_count": c.txn_count,
        "amount_median_paise": c.amount_median_paise,
        "amount_p95_paise": c.amount_p95_paise,
        "mean_hourly_txns": round(c.mean_hourly_txns, 3),
        "active_hours": f"{c.active_hour_start:02d}:00-{c.active_hour_end:02d}:00 IST",
        "known_device_ids": list(c.known_device_ids or []),
    }
    ev = [EvidenceItem("amount_median_paise", float(c.amount_median_paise), None, "paise",
                       f"Customer median transaction is {c.amount_median_paise} paise over "
                       f"{c.txn_count} historical transactions.", source_ref=c.id)]
    return ToolResult(data, ev)


def get_risk_assessment(ctx: ToolContext) -> ToolResult:
    a = ctx.db.scalar(select(RiskAssessment).where(RiskAssessment.transaction_id == ctx.txn.id))
    if not a:
        return ToolResult({"error": "no assessment found"}, [])
    signals = []
    ev: list[EvidenceItem] = []
    for s in sorted(a.signals, key=lambda x: x.contribution_pct, reverse=True):
        signals.append({
            "code": s.code, "family": s.family, "raw_value": s.raw_value,
            "baseline_value": s.baseline_value, "unit": s.unit,
            "normalized": s.normalized, "contribution_pct": s.contribution_pct,
            "explanation": s.explanation,
        })
        if s.triggered and s.code in _SIGNAL_METRIC:
            metric, unit = _SIGNAL_METRIC[s.code]
            ev.append(EvidenceItem(metric, s.raw_value, s.baseline_value, unit,
                                   s.explanation, source_ref=f"signal:{s.code}"))
    data = {
        "score": a.score, "band": a.band, "base_score": a.base_score,
        "floor_applied": a.floor_applied,
        "rules_fired": [{"code": r["code"], "detail": r["detail"]} for r in (a.rules_fired or [])],
        "engine_recommended_action": a.recommended_action,
        "requires_human_review": a.requires_human_review,
        "signals": signals,
    }
    for r in (a.rules_fired or []):
        if r["code"] == "R_IMPOSSIBLE_TRAVEL":
            ev.append(EvidenceItem("impossible_travel_speed_kmh", None, 700.0, "km/h",
                                   r["detail"], source_ref="rule:R_IMPOSSIBLE_TRAVEL"))
    return ToolResult(data, ev)


def get_transaction_history(ctx: ToolContext, window_days: int = 90, limit: int = 50) -> ToolResult:
    import numpy as np

    hist = _history(ctx, window_days)
    amounts = [t.amount_paise for t in hist]
    median = float(np.median(amounts)) if amounts else 0.0
    p95 = float(np.percentile(amounts, 95)) if amounts else 0.0
    span_h = max((hist[0].created_at - hist[-1].created_at).total_seconds() / 3600.0, 1.0) if len(hist) > 1 else 1.0
    data = {
        "window_days": window_days,
        "count": len(hist),
        "median_paise": round(median),
        "p95_paise": round(p95),
        "max_paise": max(amounts) if amounts else 0,
        "mean_hourly_txns": round(len(hist) / span_h, 3),
        "transactions": [
            {"id": t.id, "amount_paise": t.amount_paise, "city": t.city,
             "device_id": t.device_id, "method": t.method,
             "created_at": t.created_at.isoformat()}
            for t in hist[:limit]
        ],
    }
    ev = [EvidenceItem("amount_median_paise", round(median), None, "paise",
                       f"Median of {len(hist)} transactions in the last {window_days} days "
                       f"is {round(median)} paise.", source_ref="history")]
    if amounts:
        ratio = ctx.txn.amount_paise / max(median, 1)
        ev.append(EvidenceItem("amount_multiple", round(ratio, 2), 1.0, "x",
                               f"Current {ctx.txn.amount_paise} paise is {ratio:.2f}x the "
                               f"{window_days}-day median.", source_ref="history"))
    return ToolResult(data, ev)


def get_auth_events(ctx: ToolContext, window_minutes: int = 120) -> ToolResult:
    since = ctx.txn.created_at - timedelta(minutes=window_minutes)
    rows = list(ctx.db.scalars(
        select(AuthEvent).where(
            AuthEvent.customer_id == ctx.customer.id,
            AuthEvent.created_at >= since,
            AuthEvent.created_at <= ctx.txn.created_at + timedelta(minutes=2),
        ).order_by(AuthEvent.created_at)
    ))
    fails_10m = sum(
        1 for e in rows
        if not e.success and e.type in _FAIL_TYPES
        and e.created_at >= ctx.txn.created_at - timedelta(minutes=10)
    )
    data = {
        "window_minutes": window_minutes,
        "total_events": len(rows),
        "failures_in_window": sum(1 for e in rows if not e.success),
        "failures_last_10min": fails_10m,
        "events": [
            {"type": e.type, "success": e.success, "device_id": e.device_id,
             "created_at": e.created_at.isoformat()}
            for e in rows
        ],
    }
    ev = [EvidenceItem("auth_failures_window", float(fails_10m), 0.0, "failures/10min",
                       f"{fails_10m} failed auth events in the 10 minutes before the transaction.",
                       source_ref="auth_events")]
    return ToolResult(data, ev)


def find_related_events(ctx: ToolContext, link_type: str = "shared_device", window_hours: int = 48) -> ToolResult:
    since = ctx.txn.created_at - timedelta(hours=window_hours)
    col = Transaction.ip_addr if link_type == "shared_ip" else Transaction.device_id
    val = ctx.txn.ip_addr if link_type == "shared_ip" else ctx.txn.device_id
    rows = list(ctx.db.scalars(
        select(Transaction).where(
            col == val, Transaction.id != ctx.txn.id,
            Transaction.created_at >= since, Transaction.created_at <= ctx.txn.created_at,
        ).order_by(Transaction.created_at.desc()).limit(50)
    )) if val else []
    others = sorted({t.customer_id for t in rows if t.customer_id != ctx.customer.id})
    data = {
        "link_type": link_type,
        "link_value": val,
        "window_hours": window_hours,
        "related_txn_count": len(rows),
        "distinct_other_customers": len(others),
        "other_customer_ids": others,
        "events": [
            {"transaction_id": t.id, "customer_id": t.customer_id,
             "amount_paise": t.amount_paise, "city": t.city,
             "created_at": t.created_at.isoformat()}
            for t in rows[:25]
        ],
    }
    metric = "related_customers_device" if link_type != "shared_ip" else "related_customers_ip"
    ev = [EvidenceItem(metric, float(len(others)), 0.0, "customers",
                       f"{val} was used by {len(others)} other customer(s) in {window_hours}h "
                       f"({len(rows)} related transactions).", source_ref=f"{link_type}:{val}")]
    return ToolResult(data, ev)


REGISTRY = {
    "get_customer_profile": get_customer_profile,
    "get_risk_assessment": get_risk_assessment,
    "get_transaction_history": get_transaction_history,
    "get_auth_events": get_auth_events,
    "find_related_events": find_related_events,
}


def tool_schemas() -> list[dict]:
    return [
        {"name": "get_customer_profile",
         "description": "KYC tier, account age, behavioural baseline and known devices for the customer.",
         "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
        {"name": "get_risk_assessment",
         "description": "The deterministic engine's score, band, per-signal attribution and any rules fired.",
         "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
        {"name": "get_transaction_history",
         "description": "Prior transactions for the customer with summary statistics.",
         "input_schema": {"type": "object", "properties": {
             "window_days": {"type": "integer", "default": 90},
             "limit": {"type": "integer", "default": 50}}, "additionalProperties": False}},
        {"name": "get_auth_events",
         "description": "Login / OTP / password / CVV / decline events for the customer before the transaction.",
         "input_schema": {"type": "object", "properties": {
             "window_minutes": {"type": "integer", "default": 120}}, "additionalProperties": False}},
        {"name": "find_related_events",
         "description": "Other transactions sharing this transaction's device or IP - used to detect fraud rings.",
         "input_schema": {"type": "object", "properties": {
             "link_type": {"type": "string", "enum": ["shared_device", "shared_ip"], "default": "shared_device"},
             "window_hours": {"type": "integer", "default": 48}}, "additionalProperties": False}},
    ]
