"""End-to-end smoke test for the Vigil backend.

Runs against a file-backed test database, seeds it, and exercises the full
workflow: ingest/score -> case -> rule floor -> investigate (fallback) ->
grounding -> seeded dissent -> analyst override -> audit.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///./_test_vigil.db")
# This end-to-end suite exercises the deterministic engine-only path. Force it
# regardless of any provider key in a local .env so it never makes a live call
# and its mode=="fallback" assertions stay valid.
os.environ["GEMINI_API_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""

import pytest
from fastapi.testclient import TestClient

_DB_FILES = ("_test_vigil.db", "_test_vigil.db-wal", "_test_vigil.db-shm")
SEED_INFO: dict = {}


@pytest.fixture(scope="module")
def client():
    for f in _DB_FILES:
        try:
            os.remove(f)
        except OSError:
            pass
    from backend.main import app

    with TestClient(app) as c:
        SEED_INFO.update(c.post("/api/admin/reset").json())
        yield c
    for f in _DB_FILES:
        try:
            os.remove(f)
        except OSError:
            pass


def _case_for_txn(client, txn_id: str) -> dict:
    """Find the case whose transaction is txn_id, return its full detail."""
    for c in client.get("/api/cases").json():
        d = client.get(f"/api/cases/{c['id']}").json()
        if d["transaction"]["id"] == txn_id:
            return d
    raise AssertionError(f"no case found for transaction {txn_id}")


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


# ---------------------------------------------------------------- B3: rule floor
HARD_RULE_FLOORS = {"R_IMPOSSIBLE_TRAVEL": 85, "R_MULTI_CUSTOMER_DEVICE": 75}


def test_rule_floor_visibly_bumps_the_score(client):
    """The card-testing case: statistical score is MEDIUM, a deterministic rule
    raises it to HIGH, and the bump is reported transparently."""
    d = _case_for_txn(client, SEED_INFO["dissent_case_txn"])
    a = d["assessment"]
    assert a["floor_rule"] == "R_MULTI_CUSTOMER_DEVICE"
    assert a["floor_applied"] == 75
    assert a["base_score"] < a["floor_applied"], "statistical score should be below the floor"
    assert a["score"] == a["floor_applied"], "final score should equal the governing floor"
    assert a["band"] == "HIGH"
    assert a["base_score"] < 62, "pre-rule score should sit in the MEDIUM band"


def test_final_score_never_contradicts_a_critical_rule(client):
    """Across every scored transaction, if a hard rule fired the final score is
    never below that rule's floor."""
    checked = 0
    for t in client.get("/api/transactions?limit=500").json():
        det = client.get(f"/api/transactions/{t['id']}").json()
        a = det.get("assessment")
        if not a:
            continue
        for r in a["rules_fired"]:
            if r["code"] in HARD_RULE_FLOORS:
                assert a["score"] >= r["floor"], (t["id"], r["code"], a["score"], r["floor"])
                checked += 1
    assert checked >= 1, "expected at least one hard-rule transaction to verify"


def test_score_layers_are_distinguishable(client):
    """The four layers - statistical, deterministic rules, engine score, AI - are
    each independently reported so the UI can show them separately."""
    d = _case_for_txn(client, SEED_INFO["dissent_case_txn"])
    a = d["assessment"]
    assert "base_score" in a                       # 1. statistical
    assert a["rules_fired"] and "floor" in a["rules_fired"][0]  # 2. deterministic
    assert a["score"] >= a["base_score"]           # 3. engine score = max(...)
    assert a["recommended_action"]                 # engine's action
    # 4. AI layer is a separate object on the case, not mixed into the score
    assert "runs" in d


# ---------------------------------------------------------------- B4: seeded dissent
def test_seeded_dissent_is_reproducible_and_labelled(client):
    d = _case_for_txn(client, SEED_INFO["dissent_case_txn"])
    case_id = d["case"]["id"]
    engine_action = d["assessment"]["recommended_action"]
    assert engine_action == "HOLD_FOR_REVIEW"

    run = client.post(f"/api/cases/{case_id}/investigate", json={}).json()
    assert run["mode"] == "fallback", "no API key in tests -> deterministic fallback"
    assert run["status"] == "FALLBACK"
    assert run["concurs_with_engine"] is False, "the investigator must dissent here"
    assert run["recommended_action"] == "BLOCK"
    assert run["recommended_action"] != engine_action
    assert run["agent_risk_view"] in ("HIGH", "CRITICAL")
    assert run["dissent_reason"] and "ring" in run["dissent_reason"].lower()
    # dissent must not fabricate evidence
    assert run["grounding_verdict"] in ("PASS", "PARTIAL")
    assert run["claims_verified"] == run["claims_total"]


def test_disagreement_is_visible_in_case_detail(client):
    d = _case_for_txn(client, SEED_INFO["dissent_case_txn"])
    run = d["runs"][-1]
    assert run["concurs_with_engine"] is False
    assert run["dissent_reason"]
    # both recommendations are exposed so the UI can render the disagreement
    assert run["recommended_action"] == "BLOCK"
    assert d["assessment"]["recommended_action"] == "HOLD_FOR_REVIEW"
    assert run["mode"] == "fallback"  # UI labels this as demo/fallback behaviour


def test_analyst_is_final_decision_maker_after_dissent(client):
    d = _case_for_txn(client, SEED_INFO["dissent_case_txn"])
    case_id = d["case"]["id"]

    # choosing the agent's BLOCK diverges from the engine's HOLD_FOR_REVIEW -> reason required
    no_reason = client.post(f"/api/cases/{case_id}/decision", json={
        "decision": "OVERRIDE", "final_action": "BLOCK", "override_reason": "   ",
    })
    assert no_reason.status_code == 422

    ok = client.post(f"/api/cases/{case_id}/decision", json={
        "decision": "OVERRIDE", "final_action": "BLOCK",
        "override_reason": "Agreeing with the investigator: shared-device ring, block the device.",
    })
    assert ok.status_code == 200
    assert client.get(f"/api/cases/{case_id}").json()["case"]["status"] == "BLOCKED"


def test_audit_trail_covers_the_dissent_flow(client):
    d = _case_for_txn(client, SEED_INFO["dissent_case_txn"])
    audit = client.get(f"/api/cases/{d['case']['id']}/audit").json()
    by_action = {a["action"]: a for a in audit}
    assert {"risk_assessed", "case_opened", "investigation_started",
            "ai_recommendation_generated", "analyst_decision"} <= set(by_action)
    assert by_action["ai_recommendation_generated"]["detail"]["concurs_with_engine"] is False
    dec = by_action["analyst_decision"]["detail"]
    assert dec["override"] is True
    assert dec["override_reason"]
    assert dec["engine_recommended"] == "HOLD_FOR_REVIEW"
    assert dec["final_action"] == "BLOCK"
