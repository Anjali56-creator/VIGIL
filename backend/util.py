"""Small shared helpers."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    """Naive wall-clock IST.

    SQLite does not persist tzinfo, so datetimes read back from the DB are naive.
    To keep every comparison consistent we use naive IST throughout the app.
    """
    return datetime.now(IST).replace(tzinfo=None)


def to_naive_ist(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(IST).replace(tzinfo=None)
    return dt


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def poisson_sf(k: int, lam: float) -> float:
    """P(X >= k) for X ~ Poisson(lam). Stable for the small k this app deals with."""
    if k <= 0:
        return 1.0
    if lam <= 0:
        return 0.0
    # cdf(k-1) via summation
    term = math.exp(-lam)
    cdf = term
    for i in range(1, k):
        term *= lam / i
        cdf += term
    return max(0.0, min(1.0, 1.0 - cdf))


def rupees(paise: int) -> str:
    return f"Rs {paise / 100:,.2f}"
