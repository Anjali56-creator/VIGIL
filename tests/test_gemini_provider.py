"""Provider-swap tests for the Gemini live investigation agent.

These NEVER make a real Gemini API call - `backend.agent.runner._gemini_client`
is replaced with a scripted fake. They verify:

  1. Gemini configuration detection
  2. provider selection / precedence
  3. tool-call handling (read-only tools, evidence ledger)
  4. structured Investigation parsing
  5. evidence-id grounding (bogus EV ids are refuted, never silently accepted)
  6. numeric re-verification against the database (+-1% tolerance)
  7. Gemini failure -> deterministic engine-only fallback
  8. no API key -> deterministic engine-only fallback
  9. the analyst remains the final decision-maker (investigate() decides nothing)
"""
from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite:///./_test_gemini.db")
os.environ["GEMINI_API_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""

import pytest
from google.genai import types

from backend.config import settings
from backend.agent import runner
from backend.agent.schema import Finding, Investigation
from backend.agent.validator import _truth

_DB_FILES = ("_test_gemini.db", "_test_gemini.db-wal", "_test_gemini.db-shm")


# --------------------------------------------------------------------------- fakes
class _FakeModels:
    def __init__(self, outer: "FakeGeminiClient"):
        self._outer = outer

    def generate_content(self, *, model, contents, config):  # noqa: D401 - mimics SDK
        self._outer.calls.append(model)
        if self._outer.raise_exc is not None:
            raise self._outer.raise_exc
        usage = SimpleNamespace(prompt_token_count=11, candidates_token_count=7)
        # A request that carries a response_schema is the "submit" turn.
        if getattr(config, "response_schema", None) is not None:
            self._outer.submit_turns += 1
            inv = (self._outer.repair_investigation
                   if self._outer.submit_turns > 1 and self._outer.repair_investigation
                   else self._outer.investigation)
            return SimpleNamespace(parsed=inv, text=inv.model_dump_json(),
                                   candidates=[], usage_metadata=usage)
        # Otherwise it is a tool-loop turn: emit one batch of function calls, then
        # (once the batch is spent) emit a plain-text part so the loop breaks.
        if self._outer.tool_batches:
            names = self._outer.tool_batches.pop(0)
            parts = [types.Part(function_call=types.FunctionCall(name=n, args={}))
                     for n in names]
        else:
            parts = [types.Part(text="done gathering evidence")]
        content = types.Content(role="model", parts=parts)
        return SimpleNamespace(candidates=[SimpleNamespace(content=content)],
                               usage_metadata=usage)


class FakeGeminiClient:
    def __init__(self, *, investigation: Investigation,
                 tool_batches=None, repair_investigation: Investigation | None = None,
                 raise_exc: Exception | None = None):
        self.investigation = investigation
        self.repair_investigation = repair_investigation
        self.tool_batches = list(tool_batches or [["get_risk_assessment",
                                                   "get_transaction_history",
                                                   "get_auth_events",
                                                   "find_related_events"]])
        self.raise_exc = raise_exc
        self.calls: list[str] = []
        self.submit_turns = 0
        self.models = _FakeModels(self)


# ------------------------------------------------------------------------ fixtures
@pytest.fixture(scope="module")
def dbsetup():
    for f in _DB_FILES:
        try:
            os.remove(f)
        except OSError:
            pass
    from backend.db import SessionLocal, init_db
    from backend.seed import seed

    init_db()
    db = SessionLocal()
    info = seed(db)
    yield db, info
    db.close()
    for f in _DB_FILES:
        try:
            os.remove(f)
        except OSError:
            pass


@pytest.fixture
def dissent_case(dbsetup):
    """The seeded card-testing case (engine HOLD_FOR_REVIEW, ring present)."""
    from backend.models import AgentFinding, AgentRun, Case, CaseEvidence, Transaction

    db, info = dbsetup
    db.rollback()  # clear any failed-flush state left by a prior test
    txn_id = info["dissent_case_txn"]
    case = db.query(Case).join(Transaction, Case.transaction_id == Transaction.id) \
        .filter(Transaction.id == txn_id).one()

    # reset the case so each test investigates it fresh - delete in FK-safe order
    run_ids = [r.id for r in db.query(AgentRun).filter(AgentRun.case_id == case.id)]
    if run_ids:
        db.query(AgentFinding).filter(AgentFinding.run_id.in_(run_ids)).delete(
            synchronize_session=False)
        db.query(CaseEvidence).filter(CaseEvidence.run_id.in_(run_ids)).delete(
            synchronize_session=False)
        db.query(AgentRun).filter(AgentRun.id.in_(run_ids)).delete(
            synchronize_session=False)
    db.query(CaseEvidence).filter(CaseEvidence.case_id == case.id).delete(
        synchronize_session=False)
    case.status = "NEW"
    db.flush()
    db.expire_all()
    case = db.get(Case, case.id)
    return db, case


@pytest.fixture
def as_gemini(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "AIza-FAKE-not-a-real-key")
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    monkeypatch.setattr(settings, "gemini_model", "gemini-3.6-flash")
    return settings


def _truthy_investigation(db, case, *, extra_findings=()) -> Investigation:
    """Build an Investigation whose one numeric finding matches the database."""
    from backend.models import Customer, Transaction

    txn = db.get(Transaction, case.transaction_id)
    cust = db.get(Customer, case.customer_id)
    dev_truth = _truth(db, txn, cust, "new_device")
    findings = [Finding(
        title="Device never seen on this account",
        detail="The transaction device is not among the customer's known devices.",
        evidence_refs=["EV-001"], metric="new_device",
        observed=dev_truth, baseline=0.0, unit="bool", supports_risk=True, confidence=0.9,
    ), *extra_findings]
    return Investigation(
        investigation_summary="Shared-device ring across several customers; the engine "
                              "scored one transaction and under-recommended.",
        findings=findings,
        behavioral_deviation="Many small authorisations on a new device in under an hour.",
        related_activity="Device seen on 3 other customers in 48h.",
        agent_risk_view="CRITICAL",
        concurs_with_engine=False,
        dissent_reason="A ring warrants blocking the device, not holding one transaction.",
        recommended_action="BLOCK",
        confidence=0.7,
        requires_human_review=True,
    )


# ---------------------------------------------------------------- 1. config detect
def test_gemini_configuration_detection(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "AIza-FAKE")
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-FAKE")
    assert settings.active_provider == "gemini"           # gemini wins precedence
    assert settings.active_model == "gemini-3.6-flash"
    assert settings.active_model_label == "Gemini gemini-3.6-flash"
    assert settings.llm_configured is True

    monkeypatch.setattr(settings, "gemini_api_key", "   ")  # blank -> not configured
    assert settings.active_provider == "anthropic"
    assert settings.active_model_label == "Claude claude-opus-5"

    monkeypatch.setattr(settings, "anthropic_api_key", None)
    assert settings.active_provider is None
    assert settings.active_model_label == "engine-only fallback"
    assert settings.llm_configured is False


# --------------------------------------------------------------- 2. provider route
def test_provider_selection_routes_by_config(dissent_case, monkeypatch):
    db, case = dissent_case
    seen = {}
    monkeypatch.setattr(runner, "_run_gemini", lambda *a: seen.setdefault("who", "gemini"))
    monkeypatch.setattr(runner, "_run_anthropic", lambda *a: seen.setdefault("who", "anthropic"))
    monkeypatch.setattr(runner, "_run_fallback",
                        lambda *a, **k: seen.setdefault("who", "fallback"))

    monkeypatch.setattr(settings, "gemini_api_key", "AIza-FAKE")
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-FAKE")
    runner.investigate(db, case)
    assert seen["who"] == "gemini"

    seen.clear()
    monkeypatch.setattr(settings, "gemini_api_key", None)
    runner.investigate(db, case)
    assert seen["who"] == "anthropic"

    seen.clear()
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    runner.investigate(db, case)
    assert seen["who"] == "fallback"


# ------------------------------------------------ 3+4. tool handling + parsing
def test_gemini_tool_calls_and_structured_parse(dissent_case, as_gemini, monkeypatch):
    db, case = dissent_case
    inv = _truthy_investigation(db, case)
    fake = FakeGeminiClient(investigation=inv,
                            tool_batches=[["get_risk_assessment", "find_related_events"]])
    monkeypatch.setattr(runner, "_gemini_client", lambda: fake)

    run = runner.investigate(db, case)

    assert run.mode == "llm" and run.status == "COMPLETED"
    assert run.model == "Gemini gemini-3.6-flash"          # never "Claude"
    assert fake.calls and all(m == "gemini-3.6-flash" for m in fake.calls)
    # tools actually ran and were logged
    tools_used = {t["tool"] for t in run.tool_log}
    assert {"get_risk_assessment", "find_related_events"} <= tools_used
    assert run.tool_call_count == len(run.tool_log) >= 2
    # evidence ledger was populated for this run
    from backend.models import CaseEvidence
    evs = db.query(CaseEvidence).filter(CaseEvidence.run_id == run.id).all()
    assert evs, "tool output must be written to the evidence ledger"
    # structured Investigation fields were parsed and persisted
    assert run.recommended_action == "BLOCK"
    assert run.agent_risk_view == "CRITICAL"
    assert run.confidence == pytest.approx(0.7)
    assert run.concurs_with_engine is False and run.dissent_reason


# --------------------------------------------------- 5. evidence-id grounding
def test_bogus_evidence_id_is_refuted_not_accepted(dissent_case, as_gemini, monkeypatch):
    db, case = dissent_case
    fabricated = Finding(
        title="Fabricated claim citing a non-existent evidence id",
        detail="This finding cites EV-999 which no tool ever returned.",
        evidence_refs=["EV-999"], metric="amount_multiple",
        observed=42.0, baseline=1.0, unit="x", supports_risk=True, confidence=0.9,
    )
    inv = _truthy_investigation(db, case, extra_findings=(fabricated,))
    monkeypatch.setattr(runner, "_gemini_client",
                        lambda: FakeGeminiClient(investigation=inv))

    run = runner.investigate(db, case)

    from backend.models import AgentFinding
    rows = db.query(AgentFinding).filter(AgentFinding.run_id == run.id).all()
    by_title = {r.title: r for r in rows}
    # the fabricated finding is persisted (never silently dropped) and marked REFUTED
    assert len(rows) == 2
    assert by_title["Fabricated claim citing a non-existent evidence id"].validation_status == "REFUTED"
    # grounding did not pass, so the run is flagged for human review
    assert run.grounding_verdict != "PASS"
    assert run.requires_human_review is True
    # the fabricated numeric claim contributed nothing to the verified count
    assert run.claims_verified == 1  # only the genuine new_device finding


# --------------------------------------------------- 6. numeric re-verification
def test_wrong_number_is_refuted(dissent_case, as_gemini, monkeypatch):
    db, case = dissent_case
    from backend.models import Customer, Transaction
    txn = db.get(Transaction, case.transaction_id)
    cust = db.get(Customer, case.customer_id)
    true_dev = _truth(db, txn, cust, "new_device")

    good = Finding(title="Correct device claim", detail="matches db",
                   evidence_refs=["EV-001"], metric="new_device",
                   observed=true_dev, baseline=0.0, unit="bool", confidence=0.9)
    wrong = Finding(title="Wrong median claim",
                    detail="claims a median that the database does not support",
                    evidence_refs=["EV-001"], metric="amount_median_paise",
                    observed=1.0, baseline=0.0, unit="paise", confidence=0.9)
    inv = _truthy_investigation(db, case)
    inv.findings = [good, wrong]
    monkeypatch.setattr(runner, "_gemini_client",
                        lambda: FakeGeminiClient(investigation=inv))

    run = runner.investigate(db, case)

    from backend.models import AgentFinding
    by_title = {r.title: r.validation_status
                for r in db.query(AgentFinding).filter(AgentFinding.run_id == run.id)}
    assert by_title["Correct device claim"] == "VERIFIED"
    assert by_title["Wrong median claim"] == "REFUTED"
    assert run.claims_verified == 1 and run.claims_total == 2


# --------------------------------------------------- 7. failure -> fallback
def test_gemini_failure_falls_back(dissent_case, as_gemini, monkeypatch):
    db, case = dissent_case
    monkeypatch.setattr(
        runner, "_gemini_client",
        lambda: FakeGeminiClient(investigation=_truthy_investigation(db, case),
                                 raise_exc=RuntimeError("simulated Gemini outage")),
    )

    run = runner.investigate(db, case)

    assert run.mode == "fallback" and run.status == "FALLBACK"
    assert run.model == "engine-only"                       # never "Gemini ..."
    assert "gemini error" in (run.failure_reason or "")
    assert "simulated Gemini outage" in (run.failure_reason or "")
    # the deterministic investigation still produced findings
    from backend.models import AgentFinding
    assert db.query(AgentFinding).filter(AgentFinding.run_id == run.id).count() >= 1


# --------------------------------------------------- 8. no key -> fallback
def test_no_provider_key_falls_back(dissent_case, monkeypatch):
    db, case = dissent_case
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(settings, "anthropic_api_key", None)

    run = runner.investigate(db, case)

    assert run.mode == "fallback"
    assert run.model == "engine-only"
    assert "no live LLM provider configured" in (run.failure_reason or "")


# --------------------------------------------------- 9. analyst is final
def test_investigate_never_decides_the_case(dissent_case, as_gemini, monkeypatch):
    db, case = dissent_case
    monkeypatch.setattr(runner, "_gemini_client",
                        lambda: FakeGeminiClient(investigation=_truthy_investigation(db, case)))

    run = runner.investigate(db, case)

    # investigate() advises but never approves/blocks and writes no analyst record
    assert case.status in ("REVIEW_REQUIRED", "INVESTIGATING")
    assert case.status not in ("APPROVED", "BLOCKED", "RESOLVED")
    from backend.models import AuditLog, Decision
    assert db.query(Decision).filter(Decision.case_id == case.id).count() == 0
    actions = {a.action for a in db.query(AuditLog).filter(AuditLog.entity_id == case.id)}
    assert "analyst_decision" not in actions
    assert "ai_recommendation_generated" in actions

    # the analyst can then decide, and only that changes the outcome
    from backend.services import apply_decision
    apply_decision(db, case, decision="OVERRIDE", final_action="BLOCK",
                   override_reason="Confirmed ring; block the device.")
    assert case.status == "BLOCKED"
    assert db.query(Decision).filter(Decision.case_id == case.id).count() == 1
