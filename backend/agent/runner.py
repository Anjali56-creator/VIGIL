"""The investigation agent loop.

Flow:  create run -> (LLM agentic loop with read tools | deterministic fallback)
       -> persist evidence ledger -> grounding validation (+ 1 repair turn)
       -> persist findings + conclusion -> audit.
"""
from __future__ import annotations

import json
import time
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..audit import record
from ..config import settings
from ..models import AgentFinding, AgentRun, Case, CaseEvidence, Customer, RiskAssessment, Transaction
from ..util import now_ist
from . import fallback
from .prompts import SYSTEM
from .schema import Investigation, submit_tool_schema
from .tools import REGISTRY, EvidenceItem, ToolContext, tool_schemas
from .validator import validate

MAX_TOOL_ITERS = 8
MODEL_MAX_TOKENS = 8000


class _Ledger:
    """Assigns stable EV-nnn ids per case and persists evidence rows."""

    def __init__(self, db: Session, case: Case, run: AgentRun):
        self.db, self.case, self.run = db, case, run
        self._n = db.scalar(
            select(func.count()).select_from(CaseEvidence).where(CaseEvidence.case_id == case.id)
        ) or 0
        self.by_metric: dict[str, list[str]] = {}
        self.ids: set[str] = set()

    def add(self, items: list[EvidenceItem], tool_name: str) -> list[dict]:
        out = []
        for it in items:
            self._n += 1
            ev_id = f"EV-{self._n:03d}"
            self.db.add(CaseEvidence(
                id=ev_id, case_id=self.case.id, run_id=self.run.id,
                source_tool=tool_name, source_ref=it.source_ref, metric=it.metric,
                observed_value=it.observed_value, baseline_value=it.baseline_value,
                unit=it.unit, detail=it.detail,
            ))
            self.ids.add(ev_id)
            self.by_metric.setdefault(it.metric, []).append(ev_id)
            out.append({"id": ev_id, "metric": it.metric, "observed": it.observed_value,
                        "baseline": it.baseline_value, "unit": it.unit, "detail": it.detail})
        self.db.flush()
        return out


def _finish(db: Session, run: AgentRun, case: Case, inv: Investigation, ledger: _Ledger,
            *, mode: str, t0: float, usage: tuple[int, int] = (0, 0)) -> AgentRun:
    customer = db.get(Customer, case.customer_id)
    txn = db.get(Transaction, case.transaction_id)
    assessment = db.scalar(select(RiskAssessment).where(RiskAssessment.transaction_id == txn.id))

    grounding = validate(db, txn, customer, inv, ledger.ids)

    # Mark evidence rows that a VERIFIED finding relied on.
    verified_refs = {r for fv in grounding.per_finding if fv.status == "VERIFIED"
                     for r in fv.finding.evidence_refs}
    for ev in db.scalars(select(CaseEvidence).where(CaseEvidence.run_id == run.id)):
        if ev.id in verified_refs or ev.metric in {fv.finding.metric for fv in grounding.per_finding
                                                   if fv.status == "VERIFIED"}:
            ev.verified = True
            ev.verification_note = "Recomputed from database; matches."

    for fv in grounding.per_finding:
        f = fv.finding
        db.add(AgentFinding(
            run_id=run.id, title=f.title, detail=f.detail, evidence_refs=f.evidence_refs,
            metric=f.metric, observed=f.observed, baseline=f.baseline, unit=f.unit,
            supports_risk=f.supports_risk, confidence=f.confidence, validation_status=fv.status,
        ))

    engine_band = assessment.band if assessment else ""
    concurs = inv.concurs_with_engine and inv.agent_risk_view == engine_band
    run.status = "COMPLETED" if mode == "llm" else "FALLBACK"
    run.mode = mode
    run.model = settings.vigil_model if mode == "llm" else "engine-only"
    run.finished_at = now_ist()
    run.latency_ms = int((time.perf_counter() - t0) * 1000)
    run.input_tokens, run.output_tokens = usage
    run.summary = inv.investigation_summary
    run.behavioral_deviation = inv.behavioral_deviation
    run.related_activity = inv.related_activity
    run.agent_risk_view = inv.agent_risk_view
    run.concurs_with_engine = concurs
    run.dissent_reason = None if concurs else (inv.dissent_reason or "Agent's view differs from engine band.")
    run.recommended_action = inv.recommended_action
    run.confidence = inv.confidence
    run.requires_human_review = inv.requires_human_review or grounding.verdict != "PASS"
    run.grounding_verdict = grounding.verdict
    run.claims_verified = grounding.claims_verified
    run.claims_total = grounding.claims_total

    if case.status in ("NEW", "INVESTIGATING"):
        case.status = "REVIEW_REQUIRED" if run.requires_human_review else "INVESTIGATING"

    record(db, entity_type="case", entity_id=case.id, action="ai_recommendation_generated",
           detail={"run_id": run.id, "mode": mode, "agent_risk_view": inv.agent_risk_view,
                   "recommended_action": inv.recommended_action, "concurs_with_engine": concurs,
                   "grounding_verdict": grounding.verdict,
                   "claims": f"{grounding.claims_verified}/{grounding.claims_total}"})
    db.flush()
    return run


def _run_fallback(db: Session, case: Case, run: AgentRun, t0: float, reason: str | None) -> AgentRun:
    customer = db.get(Customer, case.customer_id)
    txn = db.get(Transaction, case.transaction_id)
    inv, evidence = fallback.build(db, txn, customer)
    ledger = _Ledger(db, case, run)
    ledger.add(evidence, "engine-only")
    # attach citations so reference-integrity passes; numeric gate still decides VERIFIED
    for f in inv.findings:
        refs = ledger.by_metric.get(f.metric or "__none__") or (list(ledger.ids)[:1] if ledger.ids else [])
        f.evidence_refs = refs
    run.failure_reason = reason
    run.tool_log = [{"seq": 1, "tool": "engine-only", "summary": "deterministic investigation", "ms": 0}]
    run.tool_call_count = 0
    return _finish(db, run, case, inv, ledger, mode="fallback", t0=t0)


def investigate(db: Session, case: Case) -> AgentRun:
    t0 = time.perf_counter()
    run = AgentRun(case_id=case.id, status="RUNNING", mode="llm", started_at=now_ist())
    db.add(run)
    db.flush()
    case.status = "INVESTIGATING"
    record(db, entity_type="case", entity_id=case.id, action="investigation_started",
           detail={"run_id": run.id, "llm_configured": settings.llm_configured})
    db.flush()

    if not settings.llm_configured:
        return _run_fallback(db, case, run, t0, reason="no ANTHROPIC_API_KEY configured")

    try:
        return _run_llm(db, case, run, t0)
    except Exception as exc:  # noqa: BLE001 - demo must degrade, not break
        # Drop any partial state from the failed LLM attempt, keep the run row, fall back.
        for ev in db.scalars(select(CaseEvidence).where(CaseEvidence.run_id == run.id)):
            db.delete(ev)
        for f in db.scalars(select(AgentFinding).where(AgentFinding.run_id == run.id)):
            db.delete(f)
        db.flush()
        return _run_fallback(db, case, run, t0, reason=f"LLM error: {type(exc).__name__}: {exc}")


def _run_llm(db: Session, case: Case, run: AgentRun, t0: float) -> AgentRun:
    from anthropic import Anthropic

    customer = db.get(Customer, case.customer_id)
    txn = db.get(Transaction, case.transaction_id)
    tctx = ToolContext(db=db, txn=txn, customer=customer)
    ledger = _Ledger(db, case, run)

    client = Anthropic(api_key=settings.anthropic_api_key, timeout=60.0, max_retries=1)
    tools = [*tool_schemas(), submit_tool_schema()]
    user0 = (
        f"Investigate case {case.id}. Transaction id: {txn.id}. "
        f"Customer id: {customer.id}. The engine opened this case at score "
        f"{case.score_at_open} ({case.band}). Begin."
    )
    messages: list[dict] = [{"role": "user", "content": user0}]
    tool_log: list[dict] = []
    in_tok = out_tok = 0
    seq = 0
    investigation: Investigation | None = None

    for _ in range(MAX_TOOL_ITERS):
        resp = client.messages.create(
            model=settings.vigil_model, max_tokens=MODEL_MAX_TOKENS,
            system=SYSTEM, tools=tools, messages=messages,
        )
        in_tok += resp.usage.input_tokens
        out_tok += resp.usage.output_tokens
        messages.append({"role": "assistant", "content": resp.content})

        tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        if not tool_uses:
            messages.append({"role": "user", "content": "Call submit_investigation now with your result."})
            continue

        results = []
        done = False
        for b in tool_uses:
            if b.name == "submit_investigation":
                investigation = Investigation.model_validate(b.input)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": "received"})
                done = True
                continue
            seq += 1
            fn = REGISTRY.get(b.name)
            if not fn:
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": f"unknown tool {b.name}", "is_error": True})
                continue
            tstart = time.perf_counter()
            try:
                tr = fn(tctx, **(b.input or {}))
            except Exception as e:  # noqa: BLE001
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": f"tool error: {e}", "is_error": True})
                continue
            ms = int((time.perf_counter() - tstart) * 1000)
            ev_refs = ledger.add(tr.evidence, b.name)
            payload = {"data": tr.data, "evidence": ev_refs}
            results.append({"type": "tool_result", "tool_use_id": b.id,
                            "content": json.dumps(payload, default=str)})
            tool_log.append({"seq": seq, "tool": b.name, "args": b.input or {},
                             "summary": _summarize(b.name, tr.data), "rows": _rows(tr.data), "ms": ms})
        messages.append({"role": "user", "content": results})
        if done:
            break

    run.tool_log = tool_log
    run.tool_call_count = len(tool_log)

    if investigation is None:
        raise RuntimeError("agent did not submit an investigation within the iteration budget")

    # ---- grounding + one repair turn ----
    grounding = validate(db, txn, customer, investigation, ledger.ids)
    if grounding.verdict != "PASS" and grounding.violations:
        repair = (
            "Grounding validation failed. Fix ONLY these issues and call submit_investigation "
            "again with a corrected result:\n- " + "\n- ".join(grounding.violations[:8])
            + "\nUse only evidence ids that tools returned. Put exact tool numbers in observed/baseline."
        )
        messages.append({"role": "user", "content": repair})
        try:
            resp = client.messages.create(
                model=settings.vigil_model, max_tokens=MODEL_MAX_TOKENS,
                system=SYSTEM, tools=tools, messages=messages,
            )
            in_tok += resp.usage.input_tokens
            out_tok += resp.usage.output_tokens
            for b in resp.content:
                if getattr(b, "type", None) == "tool_use" and b.name == "submit_investigation":
                    investigation = Investigation.model_validate(b.input)
                    run.tool_log = [*tool_log, {"seq": seq + 1, "tool": "repair",
                                               "summary": "corrected after grounding failure", "ms": 0}]
                    break
        except Exception:  # noqa: BLE001 - keep the first result if repair fails
            pass

    return _finish(db, run, case, investigation, ledger, mode="llm", t0=t0, usage=(in_tok, out_tok))


def _summarize(tool: str, data: dict) -> str:
    if tool == "get_risk_assessment":
        return f"score {data.get('score')} {data.get('band')}, {len(data.get('signals', []))} signals"
    if tool == "get_transaction_history":
        return f"{data.get('count')} txns, median {data.get('median_paise')} paise"
    if tool == "get_auth_events":
        return f"{data.get('total_events')} events, {data.get('failures_last_10min')} fails/10min"
    if tool == "find_related_events":
        return (f"{data.get('related_txn_count')} related txns, "
                f"{data.get('distinct_other_customers')} other customers")
    if tool == "get_customer_profile":
        return f"tier {data.get('kyc_tier')}, {data.get('historical_txn_count')} txns"
    return "ok"


def _rows(data: dict) -> int:
    for k in ("transactions", "events", "signals"):
        if isinstance(data.get(k), list):
            return len(data[k])
    return 0
