"""Individual risk-signal detectors.

Each detector returns a Signal describing one behavioural deviation with the raw
observed value, the customer's baseline for comparison, and a normalized 0..1
severity. Nothing here fuses or scores - that is fusion.py's job.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from ..models import AuthEvent, Customer, Transaction
from ..util import haversine_km, poisson_sf, rupees

# Base weights applied to each signal's normalized severity before fusion.
WEIGHTS: dict[str, float] = {
    "AMT_DEV": 0.80,
    "VEL_1H": 0.70,
    "AUTH_FAIL": 0.85,
    "DEV_NEW": 0.60,
    "GEO_DIST": 0.50,
    "TIME_ODD": 0.35,
}

FAMILY: dict[str, str] = {
    "AMT_DEV": "amount",
    "VEL_1H": "velocity",
    "AUTH_FAIL": "authentication",
    "DEV_NEW": "device",
    "GEO_DIST": "geo",
    "TIME_ODD": "temporal",
}


@dataclass
class Signal:
    code: str
    raw_value: float
    baseline_value: float
    unit: str
    normalized: float
    explanation: str
    weight: float = 0.0
    family: str = ""

    def __post_init__(self) -> None:
        self.normalized = max(0.0, min(1.0, self.normalized))
        self.weight = WEIGHTS.get(self.code, 0.5)
        self.family = FAMILY.get(self.code, "other")

    @property
    def triggered(self) -> bool:
        return self.normalized >= 0.15


@dataclass
class SignalContext:
    txn: Transaction
    customer: Customer
    history: list[Transaction] = field(default_factory=list)   # prior txns, newest first
    auth_events: list[AuthEvent] = field(default_factory=list)  # prior auth events, newest first


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def amount_deviation(ctx: SignalContext) -> Signal:
    x = ctx.txn.amount_paise
    median = max(ctx.customer.amount_median_paise, 1)
    mad = ctx.customer.amount_mad_paise
    if mad >= 1:
        mz = 0.6745 * (x - median) / mad
        ratio = x / median
        # saturating map: |mz|<=3 -> 0 ; grows smoothly beyond
        import math

        norm = 1.0 - math.exp(-max(0.0, abs(mz) - 4.0) / 6.0) if mz > 0 else 0.0
        expl = (
            f"Transaction {rupees(x)} is {ratio:.1f}x the customer's median of {rupees(median)} "
            f"(robust z-score {mz:.1f}; anything above 3 is unusual)."
        )
        return Signal("AMT_DEV", raw_value=float(x), baseline_value=float(median),
                      unit="paise", normalized=norm, explanation=expl)
    # near-constant spender: fall back to plain ratio
    ratio = x / median
    norm = _clamp01((ratio - 2.0) / 6.0)
    expl = f"Transaction {rupees(x)} is {ratio:.1f}x the customer's typical {rupees(median)}."
    return Signal("AMT_DEV", raw_value=float(x), baseline_value=float(median),
                  unit="paise", normalized=norm, explanation=expl)


def velocity_1h(ctx: SignalContext) -> Signal:
    window_start = ctx.txn.created_at - timedelta(hours=1)
    k = sum(1 for t in ctx.history if t.created_at >= window_start)
    lam = max(ctx.customer.mean_hourly_txns, 0.05)
    threshold = lam + 2.0
    norm = _clamp01((k - threshold) / 5.0)
    p_more = poisson_sf(k + 1, lam)
    expl = (
        f"{k} transactions in the hour before this one; the customer averages "
        f"{lam:.2f}/hour. Probability of {k + 1}+ in an hour is {p_more * 100:.2f}%."
    )
    return Signal("VEL_1H", raw_value=float(k), baseline_value=round(lam, 3),
                  unit="txns/hour", normalized=norm, explanation=expl)


_FAIL_TYPES = {"OTP_FAIL", "PWD_FAIL", "CVV_FAIL", "PAYMENT_DECLINE"}


def auth_failures(ctx: SignalContext) -> Signal:
    window_start = ctx.txn.created_at - timedelta(minutes=10)
    fails = [
        e for e in ctx.auth_events
        if e.created_at >= window_start and (e.type in _FAIL_TYPES and not e.success)
    ]
    k = len(fails)
    norm = _clamp01(k / 4.0)
    kinds = ", ".join(sorted({e.type for e in fails})) or "none"
    expl = (
        f"{k} failed authentication events ({kinds}) in the 10 minutes before this "
        f"transaction. The customer's historical rate is near zero."
    )
    return Signal("AUTH_FAIL", raw_value=float(k), baseline_value=0.0,
                  unit="failures/10min", normalized=norm, explanation=expl)


def device_novelty(ctx: SignalContext) -> Signal:
    known = list(ctx.customer.known_device_ids or [])
    is_new = ctx.txn.device_id not in known
    if not is_new:
        return Signal("DEV_NEW", raw_value=0.0, baseline_value=float(len(known)),
                      unit="bool", normalized=0.0,
                      explanation=f"Device {ctx.txn.device_id} is one of {len(known)} known devices.")
    norm = 0.6
    high_value = ctx.txn.amount_paise > max(ctx.customer.amount_p95_paise, 1)
    if high_value:
        norm = 1.0
    expl = (
        f"Device {ctx.txn.device_id} has never been seen on this account "
        f"(known devices: {known or 'none'})."
        + (" First use is also a high-value transaction." if high_value else "")
    )
    return Signal("DEV_NEW", raw_value=1.0, baseline_value=float(len(known)),
                  unit="bool", normalized=norm, explanation=expl)


def geo_distance(ctx: SignalContext) -> Signal:
    dist = haversine_km(ctx.txn.lat, ctx.txn.lng, ctx.customer.home_lat, ctx.customer.home_lng)
    radius = max(ctx.customer.geo_radius_km, 5.0)
    ratio = dist / radius
    # Distance alone is weak evidence (people travel); ramps in slowly and never
    # fully saturates on its own - it needs a corroborating signal to matter.
    norm = _clamp01((ratio - 2.0) / 18.0)
    expl = (
        f"Transaction originated in {ctx.txn.city}, {dist:.0f} km from the customer's "
        f"usual area around {ctx.customer.home_city} (typical radius {radius:.0f} km)."
    )
    return Signal("GEO_DIST", raw_value=round(dist, 1), baseline_value=round(radius, 1),
                  unit="km", normalized=norm, explanation=expl)


def time_of_day(ctx: SignalContext) -> Signal:
    hour = ctx.txn.created_at.hour  # created_at is naive wall-clock IST
    start, end = ctx.customer.active_hour_start, ctx.customer.active_hour_end
    in_window = start <= hour <= end if start <= end else (hour >= start or hour <= end)
    if in_window:
        return Signal("TIME_ODD", raw_value=float(hour), baseline_value=float(start),
                      unit="hour", normalized=0.0,
                      explanation=f"{hour:02d}:00 is within the customer's active window "
                                  f"{start:02d}:00-{end:02d}:00.")
    d_start = min((hour - start) % 24, (start - hour) % 24)
    d_end = min((hour - end) % 24, (end - hour) % 24)
    gap = min(d_start, d_end)
    norm = _clamp01(gap / 4.0)
    expl = (
        f"{hour:02d}:00 is {gap}h outside the customer's active window "
        f"{start:02d}:00-{end:02d}:00."
    )
    return Signal("TIME_ODD", raw_value=float(hour), baseline_value=float(end),
                  unit="hour", normalized=norm, explanation=expl)


DETECTORS = (amount_deviation, velocity_1h, auth_failures, device_novelty, geo_distance, time_of_day)


def run_all(ctx: SignalContext) -> list[Signal]:
    return [d(ctx) for d in DETECTORS]
