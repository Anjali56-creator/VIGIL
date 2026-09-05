"""Synthetic data generator.

Produces a realistic population where every customer has a *personality* (amount
distribution, active hours, home city, device set, transaction rate) and 90 days
of history consistent with it, plus a few scripted attack sequences.

This is synthetic demo data. It does NOT represent real Razorpay production data.
"""
from __future__ import annotations

import math
import random
from datetime import timedelta

import numpy as np
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .models import (
    AgentFinding,
    AgentRun,
    AuditLog,
    AuthEvent,
    Case,
    CaseEvidence,
    Customer,
    Decision,
    RiskAssessment,
    RiskSignal,
    Transaction,
)
from .risk.engine import assess
from .services import open_case
from .util import haversine_km, now_ist

SEED = 42

CITIES = {
    "Mumbai": (19.076, 72.877), "Pune": (18.520, 73.856), "Delhi": (28.704, 77.102),
    "Bengaluru": (12.972, 77.594), "Chennai": (13.083, 80.270), "Hyderabad": (17.385, 78.487),
    "Kolkata": (22.573, 88.364), "Ahmedabad": (23.023, 72.571), "Jaipur": (26.912, 75.787),
    "Lucknow": (26.847, 80.947), "Chandigarh": (30.733, 76.779), "Surat": (21.170, 72.831),
}
CITY_NAMES = list(CITIES)

MERCHANTS = [
    ("BigBazaar Retail", "5411"), ("Swiggy", "5812"), ("Amazon Pay", "5999"),
    ("IRCTC", "4112"), ("Jio Recharge", "4814"), ("Apollo Pharmacy", "5912"),
    ("Croma Electronics", "5732"), ("MakeMyTrip", "4722"), ("Zerodha", "6211"),
    ("BESCOM Bill", "4900"),
]
FIRST = ["Aarav", "Vivaan", "Aditya", "Diya", "Ananya", "Ishaan", "Kabir", "Sara",
         "Rohan", "Meera", "Arjun", "Nisha", "Kiara", "Vihaan"]
LAST = ["Sharma", "Verma", "Iyer", "Nair", "Reddy", "Gupta", "Bose", "Khan",
        "Patel", "Mehta", "Rao", "Das"]

def _wipe(db: Session) -> None:
    from .db import Base

    # Delete in reverse foreign-key dependency order so constraints never trip.
    for table in reversed(Base.metadata.sorted_tables):
        db.execute(delete(table))
    db.flush()


def _jitter(lat: float, lng: float, rng: random.Random, km: float = 12.0) -> tuple[float, float]:
    d = km / 111.0
    return lat + rng.uniform(-d, d), lng + rng.uniform(-d, d)


def _build_customers(db: Session, rng: random.Random) -> list[Customer]:
    customers: list[Customer] = []
    now = now_ist()
    for i in range(12):
        city = CITY_NAMES[i % len(CITY_NAMES)]
        lat, lng = CITIES[city]
        typical = rng.choice([1500, 2500, 4000, 6000, 9000, 14000]) * 100  # paise
        start = rng.choice([7, 8, 9, 10])
        end = rng.choice([20, 21, 22, 23])
        devices = [f"DEV-{1000 + i * 7 + k}" for k in range(rng.choice([1, 1, 2]))]
        c = Customer(
            id=f"CUST-{1001 + i}",
            name=f"{FIRST[i % len(FIRST)]} {LAST[i % len(LAST)]}",
            email_masked=f"{FIRST[i % len(FIRST)][:2].lower()}***@example.com",
            kyc_tier=rng.choice([1, 2, 2, 3]),
            segment=rng.choice(["retail", "retail", "retail", "sme"]),
            account_created_at=now - timedelta(days=rng.randint(400, 1500)),
            home_city=city, home_lat=lat, home_lng=lng,
            mean_hourly_txns=round(rng.uniform(0.04, 0.12), 3),
            active_hour_start=start, active_hour_end=end,
            known_device_ids=devices,
            geo_radius_km=rng.choice([20.0, 25.0, 30.0]),
        )
        # stash the personal amount scale for history generation
        c._typical = typical  # type: ignore[attr-defined]
        db.add(c)
        customers.append(c)
    db.flush()
    return customers


def _history_for(db: Session, c: Customer, rng: random.Random) -> None:
    """Fully deterministic given `rng` - no numpy RNG, no process hashing."""
    now = now_ist()
    days = 90
    active_hours = (c.active_hour_end - c.active_hour_start) or 12
    n = max(24, int(c.mean_hourly_txns * active_hours * days))
    typical = getattr(c, "_typical", 300000)
    lat0, lng0 = c.home_lat, c.home_lng

    # Chronological walk with realistic *trips*: the customer occasionally travels
    # to one city and stays there for a run of transactions over 1-4 days, then
    # returns home. This avoids teleport artefacts between consecutive events.
    timestamps = sorted(now - timedelta(days=rng.uniform(0, days)) for _ in range(n))
    trip_city: str | None = None
    trip_until = now
    last_ts: "None | object" = None
    amounts: list[int] = []
    for ts in timestamps:
        hour = int(min(23, max(0, rng.gauss((c.active_hour_start + c.active_hour_end) / 2, 2.5))))
        ts = ts.replace(hour=hour, minute=rng.randint(0, 59), second=rng.randint(0, 59))
        amt = int(min(typical * 5, max(5000, rng.lognormvariate(math.log(typical), 0.5))))
        amounts.append(amt)

        # A city change is only allowed if enough time has passed to actually
        # travel there - otherwise defer it, so consecutive events never teleport.
        can_move = last_ts is None or (ts - last_ts) >= timedelta(hours=8)
        if can_move:
            if trip_city and ts > trip_until:
                trip_city = None
            if not trip_city and rng.random() < 0.02:
                trip_city = rng.choice([x for x in CITY_NAMES if x != c.home_city])
                trip_until = ts + timedelta(days=rng.uniform(1, 4))
        last_ts = ts

        if trip_city:
            clat, clng = _jitter(*CITIES[trip_city], rng, 8)
            city = trip_city
        else:
            clat, clng = _jitter(lat0, lng0, rng, 10)
            city = c.home_city

        m_name, m_mcc = rng.choice(MERCHANTS)
        db.add(Transaction(
            id=f"txn_{c.id.lower()}_{rng.randrange(16**10):010x}",
            customer_id=c.id, amount_paise=amt, method=rng.choice(["upi", "upi", "card", "netbanking"]),
            merchant_name=m_name, merchant_mcc=m_mcc,
            device_id=rng.choice(c.known_device_ids),
            ip_addr=f"49.{rng.randint(1, 250)}.{rng.randint(1, 250)}.{rng.randint(1, 250)}",
            city=city, lat=clat, lng=clng, created_at=ts, status="captured",
        ))
        if rng.random() < 0.15:
            db.add(AuthEvent(customer_id=c.id, device_id=rng.choice(c.known_device_ids),
                             type="LOGIN", success=True, created_at=ts - timedelta(minutes=rng.randint(1, 30))))
    db.flush()
    arr = np.array(amounts)
    c.txn_count = int(len(arr))
    c.amount_median_paise = int(np.median(arr))
    c.amount_mad_paise = int(max(1, np.median(np.abs(arr - np.median(arr)))))
    c.amount_p95_paise = int(np.percentile(arr, 95))
    db.flush()


def _account_takeover(db: Session, victim: Customer, *, when=None, historical: bool = False,
                      rng: random.Random, with_ring: bool = True) -> Transaction:
    """Scripted ATO sequence: login -> new device -> OTP failures -> far-city high-value txn."""
    t = when or now_ist()
    # Deterministically choose a genuinely distant city so the far-city transaction
    # reliably reads as impossible travel from the victim's home.
    far = max((x for x in CITY_NAMES if x != victim.home_city),
              key=lambda x: haversine_km(victim.home_lat, victim.home_lng, *CITIES[x]))
    flat, flng = _jitter(*CITIES[far], rng, 6)
    bad_device = f"DEV-9{rng.randint(100, 999)}"
    bad_ip = f"185.{rng.randint(1, 250)}.{rng.randint(1, 250)}.{rng.randint(1, 250)}"

    # A genuine low-value transaction in the victim's home city ~35 min earlier -
    # the attacker strikes while the victim is active. This is what makes the
    # far-city transaction "impossible travel".
    hlat, hlng = _jitter(victim.home_lat, victim.home_lng, rng, 6)
    legit_prior = Transaction(
        id=f"txn_pre_{victim.id.lower()}_{rng.randrange(16**8):08x}",
        customer_id=victim.id, amount_paise=int(victim.amount_median_paise * rng.uniform(0.4, 1.1)),
        method="card", merchant_name="Cafe Coffee Day", merchant_mcc="5814",
        device_id=victim.known_device_ids[0],
        ip_addr=f"49.{rng.randint(1, 250)}.{rng.randint(1, 250)}.{rng.randint(1, 250)}",
        city=victim.home_city, lat=hlat, lng=hlng,
        created_at=t - timedelta(minutes=35), status="captured",
    )
    db.add(legit_prior)
    db.flush()
    assess(db, legit_prior)

    db.add(AuthEvent(customer_id=victim.id, device_id=victim.known_device_ids[0],
                     type="LOGIN", success=True, created_at=t - timedelta(minutes=11)))
    for k in (7, 5, 3):
        db.add(AuthEvent(customer_id=victim.id, device_id=bad_device, ip_addr=bad_ip,
                         type="OTP_FAIL", success=False, created_at=t - timedelta(minutes=k)))
    db.add(AuthEvent(customer_id=victim.id, device_id=bad_device, ip_addr=bad_ip,
                     type="LOGIN", success=True, created_at=t - timedelta(minutes=1)))

    if with_ring:
        others = [c for c in db.scalars(select(Customer)) if c.id != victim.id]
        for peer in rng.sample(others, 2):
            ring_txn = Transaction(
                id=f"txn_ring_{peer.id.lower()}_{rng.randrange(16**8):08x}",
                customer_id=peer.id, amount_paise=rng.randint(4000, 25000),
                method="upi", merchant_name="Prepaid Load", merchant_mcc="6540",
                device_id=bad_device, ip_addr=bad_ip, city=far, lat=flat, lng=flng,
                created_at=t - timedelta(minutes=rng.randint(15, 38)), status="captured",
                is_attack=True, scenario_label="account_takeover_ring",
            )
            db.add(ring_txn)
            db.flush()
            assess(db, ring_txn)

    # ~3-4x the median: a clearly high-value transaction, but tuned so the *statistical*
    # base score stays below the R_IMPOSSIBLE_TRAVEL floor (85). That keeps the demo
    # narrative honest - the deterministic rule is visibly what lifts the score to
    # CRITICAL, not the amount signal saturating on its own.
    amt = int(victim.amount_median_paise * rng.uniform(3.2, 4.2))
    txn = Transaction(
        id=f"txn_ato_{victim.id.lower()}_{rng.randrange(16**8):08x}",
        customer_id=victim.id, amount_paise=amt, method="upi",
        merchant_name="Gift Card Store", merchant_mcc="5947",
        device_id=bad_device, ip_addr=bad_ip, ip_is_proxy=True,
        city=far, lat=flat, lng=flng, created_at=t, status="captured",
        is_attack=True, scenario_label="account_takeover",
    )
    db.add(txn)
    db.flush()
    assessment = assess(db, txn)
    if historical and assessment.score >= 60:
        open_case(db, txn, assessment)
    return txn


# --- LIVE card-testing ring: fully pinned so the judge demo is reproducible -----
# Always the same focus customer, the same 3 ring peers, the same shared device and
# the same small home-city amounts. The only rule that fires is the deterministic
# multi-customer-device floor (75 -> HIGH); the statistical base stays in MEDIUM.
# Nothing here can trigger impossible travel (every leg is in the customer's own
# home city) or an amount anomaly (every leg is a card-testing micro-charge far
# below the customer's p95). The ring is built once per demo session and reused:
# re-running the scenario returns the same case instead of stacking extra
# velocity / device evidence that would drift the score into CRITICAL. "Reset
# demo" wipes everything, and the next run rebuilds the ring from scratch.
_CT_FOCUS = "CUST-1006"                       # Hyderabad, active 09-22, no same-day history
_CT_RING_PEERS = ("CUST-1003", "CUST-1007", "CUST-1009")
_CT_DEVICE = "DEV-8000"
_CT_IP = "196.10.20.30"
_CT_HOUR = 14                                 # calm business hour -> no time-of-day signal
_CT_FOCUS_TXN_ID = "txn_ct_focus_live"
# (minutes-before-focus, amount in paise) for the 8 small authorisations.
_CT_LEGS = ((58, 8200), (52, 9100), (46, 7600), (40, 10400),
            (34, 8800), (28, 9600), (22, 7400), (16, 11200))
_CT_FOCUS_PAISE = 9300


def _card_testing_live(db: Session, primary: Customer) -> Transaction:
    """Deterministic card-testing ring for the live judge demo.

    Reproducible outcome, every run: score 75, band HIGH, recommendation
    HOLD_FOR_REVIEW - driven purely by the R_MULTI_CUSTOMER_DEVICE rule floor.

    Idempotent: if the ring already exists this session, return its focus
    transaction unchanged (no deletes, so it is safe to call while an
    investigation on the same case is in flight)."""
    existing = db.get(Transaction, _CT_FOCUS_TXN_ID)
    if existing is not None:
        if existing.assessment is None:
            assess(db, existing)
        return existing

    t = now_ist().replace(hour=_CT_HOUR, minute=30, second=0, microsecond=0)

    peers = [p for p in (db.get(Customer, cid) for cid in _CT_RING_PEERS)
             if p and p.id != primary.id]
    pool = [primary, *peers]

    for i, (mins, paise) in enumerate(_CT_LEGS):
        cust = pool[i % len(pool)]
        clat, clng = _jitter(cust.home_lat, cust.home_lng, random.Random(i), 4)
        small = Transaction(
            id=f"txn_ct_{cust.id.lower()}_live{i}",
            customer_id=cust.id, amount_paise=paise,
            method="card", merchant_name="Online Wallet Top-up", merchant_mcc="6540",
            device_id=_CT_DEVICE, ip_addr=_CT_IP, city=cust.home_city, lat=clat, lng=clng,
            created_at=t - timedelta(minutes=mins), status="captured",
            is_attack=True, scenario_label="card_testing",
        )
        db.add(small)
        db.flush()
        assess(db, small)

    db.add(AuthEvent(customer_id=primary.id, device_id=_CT_DEVICE, ip_addr=_CT_IP,
                     type="PAYMENT_DECLINE", success=False, created_at=t - timedelta(minutes=6)))

    clat, clng = _jitter(primary.home_lat, primary.home_lng, random.Random(99), 4)
    txn = Transaction(
        id=_CT_FOCUS_TXN_ID,
        customer_id=primary.id, amount_paise=_CT_FOCUS_PAISE,
        method="card", merchant_name="Online Wallet Top-up", merchant_mcc="6540",
        device_id=_CT_DEVICE, ip_addr=_CT_IP, city=primary.home_city, lat=clat, lng=clng,
        created_at=t, status="captured", is_attack=True, scenario_label="card_testing",
    )
    db.add(txn)
    db.flush()
    assess(db, txn)
    return txn


def _card_testing(db: Session, primary: Customer, *, when=None, historical: bool = False,
                  rng: random.Random, ring_size: int = 4) -> Transaction:
    """Card-testing ring: one device runs many small authorisations across several
    customers within ~1 hour.

    Each individual transaction is statistically mild (tiny amount, home city), so
    the anomaly score lands in MEDIUM. It is the deterministic multi-customer-device
    rule that raises the score to HIGH - a visible, explainable rule floor. The
    focus transaction is the latest one, on `primary`.

    The live path (historical=False, used by the demo's "Run scenario") is fully
    pinned and idempotent - see `_card_testing_live`. The historical path below
    (used once by `seed()`, deterministic via SEED=42) seeds the dissent-demo case.
    """
    if not historical:
        return _card_testing_live(db, primary)

    # Pin the ring to a calm business hour so the focus transaction carries no
    # incidental time-of-day signal. The whole point of this scenario is that the
    # deterministic multi-customer-device RULE (not the anomaly score) is what
    # lifts it from MEDIUM to HIGH - that has to be reproducible at any wall-clock
    # time the demo is run.
    t = (when or now_ist()).replace(hour=14, minute=30, second=0, microsecond=0)
    bad_device = f"DEV-8{rng.randint(100, 999)}"
    bad_ip = f"196.{rng.randint(1, 250)}.{rng.randint(1, 250)}.{rng.randint(1, 250)}"
    others = [c for c in db.scalars(select(Customer)) if c.id != primary.id]
    pool = [primary, *rng.sample(others, ring_size - 1)]

    minute = 58
    for i in range(ring_size * 2):  # ~8 small authorisations across the pool
        cust = pool[i % len(pool)]
        clat, clng = _jitter(cust.home_lat, cust.home_lng, rng, 8)
        small = Transaction(
            id=f"txn_ct_{cust.id.lower()}_{rng.randrange(16**8):08x}",
            customer_id=cust.id, amount_paise=rng.randint(4000, 16000),
            method="card", merchant_name="Online Wallet Top-up", merchant_mcc="6540",
            device_id=bad_device, ip_addr=bad_ip, city=cust.home_city, lat=clat, lng=clng,
            created_at=t - timedelta(minutes=minute), status="captured",
            is_attack=True, scenario_label="card_testing",
        )
        db.add(small)
        db.flush()
        assess(db, small)
        minute = max(1, minute - rng.randint(6, 9))

    # one decline - a mild auth signal, not enough to trip the auth-storm rule
    db.add(AuthEvent(customer_id=primary.id, device_id=bad_device, ip_addr=bad_ip,
                     type="PAYMENT_DECLINE", success=False, created_at=t - timedelta(minutes=6)))

    clat, clng = _jitter(primary.home_lat, primary.home_lng, rng, 6)
    txn = Transaction(
        id=f"txn_ct_focus_{primary.id.lower()}_{rng.randrange(16**8):08x}",
        customer_id=primary.id, amount_paise=rng.randint(6000, 13000),
        method="card", merchant_name="Online Wallet Top-up", merchant_mcc="6540",
        device_id=bad_device, ip_addr=bad_ip, city=primary.home_city, lat=clat, lng=clng,
        created_at=t, status="captured", is_attack=True, scenario_label="card_testing",
    )
    db.add(txn)
    db.flush()
    assessment = assess(db, txn)
    if historical:
        open_case(db, txn, assessment)
    return txn


def _normal(db: Session, customer: Customer, *, when=None, historical: bool = False,
            rng: random.Random) -> Transaction:
    """A completely unremarkable transaction: known device, home city, active hour,
    amount near the customer's median. Exercises the engine's *restraint* - it should
    score LOW and open no case. Nothing is tagged as an attack."""
    t = when or now_ist()
    mid = (customer.active_hour_start + customer.active_hour_end) // 2
    t = t.replace(hour=max(0, min(23, mid)), minute=rng.randint(0, 59), second=0, microsecond=0)
    dev = (list(customer.known_device_ids) or ["DEV-0000"])[0]
    hlat, hlng = _jitter(customer.home_lat, customer.home_lng, rng, 5)
    m_name, m_mcc = rng.choice(MERCHANTS)
    txn = Transaction(
        id=f"txn_normal_{customer.id.lower()}_{rng.randrange(16**8):08x}",
        customer_id=customer.id,
        amount_paise=int(customer.amount_median_paise * rng.uniform(0.85, 1.15)),
        method="upi", merchant_name=m_name, merchant_mcc=m_mcc,
        device_id=dev,
        ip_addr=f"49.{rng.randint(1, 250)}.{rng.randint(1, 250)}.{rng.randint(1, 250)}",
        city=customer.home_city, lat=hlat, lng=hlng, created_at=t, status="captured",
        is_attack=False, scenario_label="normal",
    )
    db.add(txn)
    db.flush()
    assessment = assess(db, txn)
    if historical and assessment.score >= 60:
        open_case(db, txn, assessment)
    return txn


def _suspicious(db: Session, customer: Customer, *, when=None, historical: bool = False,
                rng: random.Random) -> Transaction:
    """One genuinely odd transaction, but no smoking gun: a NEW device plus a moderately
    high amount (kept below p95 so the amount alone does not dominate). No auth failures,
    no shared device, no travel, no hard rule. Lands MEDIUM/HIGH -> HOLD_FOR_REVIEW,
    usually with no case opened. Not tagged as an attack - it is 'unusual', not 'known fraud'."""
    t = when or now_ist()
    mid = (customer.active_hour_start + customer.active_hour_end) // 2
    t = t.replace(hour=max(0, min(23, mid)), minute=rng.randint(0, 59), second=0, microsecond=0)
    new_dev = f"DEV-7{rng.randint(100, 999)}"
    hlat, hlng = _jitter(customer.home_lat, customer.home_lng, rng, 8)
    cap = max(int(customer.amount_p95_paise * 0.9), customer.amount_median_paise + 1)
    amount = min(int(customer.amount_median_paise * rng.uniform(2.2, 2.8)), cap)
    txn = Transaction(
        id=f"txn_susp_{customer.id.lower()}_{rng.randrange(16**8):08x}",
        customer_id=customer.id, amount_paise=amount,
        method="card", merchant_name="Croma Electronics", merchant_mcc="5732",
        device_id=new_dev,
        ip_addr=f"49.{rng.randint(1, 250)}.{rng.randint(1, 250)}.{rng.randint(1, 250)}",
        city=customer.home_city, lat=hlat, lng=hlng, created_at=t, status="captured",
        is_attack=False, scenario_label="suspicious_new_device",
    )
    db.add(txn)
    db.flush()
    assessment = assess(db, txn)
    if historical and assessment.score >= 60:
        open_case(db, txn, assessment)
    return txn


def seed(db: Session) -> dict:
    rng = random.Random(SEED)
    _wipe(db)
    customers = _build_customers(db, rng)
    for c in customers:
        _history_for(db, c, rng)
    db.flush()

    # Score every historical transaction so "analysed" == "total" and the risk
    # distribution / detection metrics are real. Cases are NOT auto-opened here -
    # only the scripted attacks below (and live-injected ones) create cases.
    for txn in db.scalars(select(Transaction).order_by(Transaction.created_at)):
        assess(db, txn)
    db.flush()

    # historical attacks so the queue and metrics are non-empty on first load
    past1 = _account_takeover(db, customers[3], when=now_ist() - timedelta(days=2, hours=3),
                              historical=True, rng=rng)
    past2 = _account_takeover(db, customers[7], when=now_ist() - timedelta(days=5, hours=1),
                              historical=True, rng=rng, with_ring=False)
    # a card-testing ring: engine floors it to HIGH via a deterministic rule, and
    # the investigator escalates further - this is the seeded-dissent demo case.
    # Pin the ring to a fixed business hour so the focus transaction's statistical
    # score is stable (no incidental odd-hour signal) - the narrative is that the
    # deterministic rule, not the anomaly score, is what lifts it to HIGH.
    past3 = _card_testing(db, customers[5],
                          when=(now_ist() - timedelta(days=1)).replace(hour=14, minute=30,
                                                                       second=0, microsecond=0),
                          historical=True, rng=rng)

    db.commit()
    n_txn = db.scalar(select(func.count()).select_from(Transaction))
    return {"customers": len(customers), "transactions": n_txn,
            "historical_attacks": [past1.id, past2.id],
            "dissent_case_txn": past3.id}


SCENARIOS = ("normal", "suspicious", "account_takeover", "card_testing")

_BUILDERS = {
    "normal": _normal,
    "suspicious": _suspicious,
    "account_takeover": _account_takeover,
    "card_testing": _card_testing,
}

_DEFAULT_VICTIM = {
    "normal": "CUST-1001",
    "suspicious": "CUST-1005",
    "account_takeover": "CUST-1002",
    "card_testing": "CUST-1006",
}


def inject_scenario(db: Session, scenario: str = "account_takeover",
                    customer_id: str | None = None) -> dict:
    rng = random.Random()  # live scenario uses fresh randomness for ids
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario '{scenario}'")
    cid = customer_id or _DEFAULT_VICTIM[scenario]
    victim = db.get(Customer, cid)
    if not victim:
        raise ValueError("victim customer not found - seed the database first")

    txn = _BUILDERS[scenario](db, victim, when=now_ist(), historical=False, rng=rng)

    assessment = txn.assessment
    case = open_case(db, txn, assessment) if assessment.score >= 60 else None
    db.commit()
    return {
        "scenario": scenario,
        "transaction_id": txn.id,
        "customer_id": victim.id,
        "score": assessment.score,
        "band": assessment.band,
        "recommended_action": assessment.recommended_action,
        "requires_human_review": assessment.requires_human_review,
        "case_id": case.id if case else None,
    }


if __name__ == "__main__":  # python -m backend.seed
    import json

    from .db import SessionLocal, init_db

    init_db()
    with SessionLocal() as _db:
        print(json.dumps(seed(_db), indent=2, default=str))
