"""End-to-end smoke test for the Vigil backend.

Runs against an in-memory database, seeds it, and exercises the full workflow:
ingest/score -> case -> investigate (fallback) -> grounding -> analyst decision -> audit.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///./_test_vigil.db")

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    for f in ("_test_vigil.db", "_test_vigil.db-wal", "_test_vigil.db-shm"):
        try:
            os.remove(f)
        except OSError:
            pass
    from backend.main import app

    with TestClient(app) as c:
        c.post("/api/admin/reset")
        yield c
    for f in ("_test_vigil.db", "_test_vigil.db-wal", "_test_vigil.db-shm"):
        try:
            os.remove(f)
        except OSError:
            pass


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["db"] is True


def test_seed_produces_realistic_distribution(client):
    m = client.get("/api/dashboard/metrics").json()
    assert m["total_transactions"] > 500
    assert m["analyzed"] == m["total_transactions"]
    d = m["risk_distribution"]
    # the overwhelming majority of a normal population is LOW risk
    assert d["LOW"] / m["analyzed"] > 0.85
    assert d["CRITICAL"] < 10


def test_engine_scores_normal_transaction_low(client):
    cust = "CUST-1001"
    r = client.post("/api/transactions", json={
        "customer_id": cust, "amount_paise": 250000, "method": "upi",
        "device_id": "DEV-1000", "city": "Mumbai", "lat": 19.076, "lng": 72.877,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["assessment"]["band"] in ("LOW", "MEDIUM")
    assert body["case_id"] is None


def test_full_investigation_workflow(client):
    cases = client.get("/api/cases").json()
    assert cases, "seed should create at least one case"
    case_id = cases[0]["id"]

    detail = client.get(f"/api/cases/{case_id}").json()
    assert detail["assessment"]["band"] == "CRITICAL"
    shares = sum(s["contribution_pct"] for s in detail["assessment"]["signals"])
    assert 95 <= shares <= 105  # leave-one-out attribution sums to ~100%

    run = client.post(f"/api/cases/{case_id}/investigate", json={}).json()
    assert run["status"] in ("COMPLETED", "FALLBACK")
    assert run["grounding_verdict"] in ("PASS", "PARTIAL")
    assert run["claims_total"] >= 1
    assert run["claims_verified"] == run["claims_total"]  # nothing fabricated
    for f in run["findings"]:
        assert f["evidence_refs"], "every finding must cite evidence"

    # analyst overrides -> reason required
    bad = client.post(f"/api/cases/{case_id}/decision", json={
        "decision": "OVERRIDE", "final_action": "BLOCK", "override_reason": None,
    })
    assert bad.status_code == 422

    ok = client.post(f"/api/cases/{case_id}/decision", json={
        "decision": "OVERRIDE", "final_action": "BLOCK",
        "override_reason": "Confirmed fraud ring via shared device.",
    })
    assert ok.status_code == 200

    audit = client.get(f"/api/cases/{case_id}/audit").json()
    actions = {a["action"] for a in audit}
    assert {"case_opened", "investigation_started", "ai_recommendation_generated",
            "analyst_decision"} <= actions


def test_simulate_scenario_creates_critical_case(client):
    r = client.post("/api/simulate/scenario", json={"scenario": "account_takeover"})
    assert r.status_code == 200
    body = r.json()
    assert body["band"] == "CRITICAL"
    assert body["case_id"] is not None
