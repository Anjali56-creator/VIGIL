"""Deterministic rule overrides.

Rules do not compute the score - they impose a *floor* on top of the statistical
score and are recorded separately, so the UI can show "score raised 71 -> 85 by
rule R_IMPOSSIBLE_TRAVEL". These encode hard domain knowledge that a purely
statistical model should never be allowed to average away.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from ..models import Transaction
from ..util import haversine_km
from .signals import SignalContext

# A rule in this set means the recommended action escalates to BLOCK (proposed).
HARD_RULES = {"R_IMPOSSIBLE_TRAVEL", "R_MULTI_CUSTOMER_DEVICE"}


@dataclass
class RuleHit:
    code: str
    floor: int
    detail: str


def _impossible_travel(ctx: SignalContext) -> RuleHit | None:
    cutoff = ctx.txn.created_at - timedelta(hours=8)
    for prev in ctx.history:  # newest first
        if prev.created_at < cutoff:
            break
        dt_h = (ctx.txn.created_at - prev.created_at).total_seconds() / 3600.0
        # Only a short-interval, long-distance jump is "impossible". Legitimate
        # travel shows up as same-city runs hours/days apart, not this.
        if dt_h <= 0 or dt_h > 3.0:
            continue
        dist = haversine_km(ctx.txn.lat, ctx.txn.lng, prev.lat, prev.lng)
        if dist < 300:
            continue
        speed = dist / dt_h
        if speed > 900:
            return RuleHit(
                "R_IMPOSSIBLE_TRAVEL", 85,
                f"{dist:.0f} km from the previous transaction in {prev.city} in "
                f"{dt_h * 60:.0f} min implies {speed:.0f} km/h - physically impossible.",
            )
    return None


def _multi_customer_device(ctx: SignalContext, others: list[Transaction]) -> RuleHit | None:
    cutoff = ctx.txn.created_at - timedelta(hours=24)
    custs = {
        t.customer_id for t in others
        if t.device_id == ctx.txn.device_id
        and t.customer_id != ctx.txn.customer_id
        and t.created_at >= cutoff
    }
    if len(custs) >= 2:
        return RuleHit(
            "R_MULTI_CUSTOMER_DEVICE", 75,
            f"Device {ctx.txn.device_id} was used by {len(custs)} other customers "
            f"in the last 24h - consistent with a fraud ring.",
        )
    return None


def _auth_storm(ctx: SignalContext) -> RuleHit | None:
    window_start = ctx.txn.created_at - timedelta(minutes=5)
    fails = [
        e for e in ctx.auth_events
        if e.created_at >= window_start and not e.success
        and e.type in {"OTP_FAIL", "PWD_FAIL", "CVV_FAIL"}
    ]
    if len(fails) >= 3:
        return RuleHit(
            "R_AUTH_STORM", 65,
            f"{len(fails)} authentication failures in the 5 minutes before a "
            f"successful transaction - consistent with credential stuffing / OTP brute force.",
        )
    return None


def evaluate(ctx: SignalContext, device_peers: list[Transaction]) -> list[RuleHit]:
    hits = [
        _impossible_travel(ctx),
        _multi_customer_device(ctx, device_peers),
        _auth_storm(ctx),
    ]
    return [h for h in hits if h is not None]
