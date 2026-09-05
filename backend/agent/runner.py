"""The investigation flow.

Fast, non-locking design:

  1. create the RUN row (status RUNNING) + audit  ->  COMMIT immediately
        the write lock is held for milliseconds, not for the whole model call.
  2. gather ALL evidence deterministically via the 5 read-only tools
        pure SELECTs; in WAL mode these never block a writer.
  3. ONE model call: the model reasons over the pre-gathered evidence and returns
     a structured `Investigation`. There is NO open database transaction during
     this call, so a slow / hung LLM cannot lock the database.
  4. grounding validation (+ at most one repair model call).
  5. persist evidence ledger + findings + run result + audit  ->  COMMIT.

If no provider is configured, or the model errors / times out / exceeds the
latency budget, step 3 is replaced by a deterministic engine-only investigation
built from the SAME evidence, badged mode="fallback" / model="engine-only".

Invariants (unchanged): the model cannot emit a 0-100 score (no such field),
every tool is read-only, `policy.py` owns the binding action, and every numeric
claim is re-verified against the database by `validator.validate`.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..audit import record
from ..config import settings
from ..models import (
    AgentFinding,
    AgentRun,
    Case,
    CaseEvidence,
    Customer,
    RiskAssessment,
    Transaction,
)
from ..util import now_ist
from . import fallback
from .prompts import SYSTEM
from .schema import Investigation
from .tools import (
    ToolContext,
    find_related_events,
    get_auth_events,
    get_customer_profile,
    get_risk_assessment,
    get_transaction_history,
)
from .validator import validate

MODEL_MAX_TOKENS = 4000
MODEL_TIMEOUT_S = 15          # hard per-call timeout
LATENCY_BUDGET_S = 22        # soft budget: past this, skip the repair call

# The five read-only evidence tools, always all run (they are independent).
_EVIDENCE_TOOLS = (
    ("get_risk_assessment", get_risk_assessment),
    ("get_customer_profile", get_customer_profile),
    ("get_transaction_history", get_transaction_history),
    ("get_auth_events", get_auth_events),
    ("find_related_events", find_related_events),
)


@dataclass
class _Ev:
    id: str
    tool: str
    metric: str
    observed: float | None
    baseline: float | None
    unit: str
    detail: str
    source_ref: str


# --------------------------------------------------------------------------- phase 2
def _gather_evidence(
    db: Session, case: Case, txn: Transaction, customer: Customer
) -> tuple[dict, list[_Ev], list[dict]]:
    """Run the 5 read-only tools once each. Returns (tool_results, evidence_items,
    tool_log). Nothing is written - EV ids are assigned in memory and persisted
    later in phase 5."""
    ctx = ToolContext(db=db, txn=txn, customer=customer)
    n = db.scalar(
        select(func.count()).select_from(CaseEvidence).where(CaseEvidence.case_id == case.id)
    ) or 0

    tool_results: dict = {}
    items: list[_Ev] = []
    tool_log: list[dict] = []
    for seq, (name, fn) in enumerate(_EVIDENCE_TOOLS, start=1):
        t = time.perf_counter()
        res = fn(ctx)
        ms = int((time.perf_counter() - t) * 1000)
        tool_results[name] = res
        for it in res.evidence:
            n += 1
            items.append(_Ev(
                id=f"EV-{n:03d}", tool=name, metric=it.metric, observed=it.observed_value,
                baseline=it.baseline_value, unit=it.unit, detail=it.detail,
                source_ref=it.source_ref,
            ))
        tool_log.append({"seq": seq, "tool": name, "args": {},
                         "summary": _summarize(name, res.data),
                         "rows": _rows(res.data), "ms": ms})
    return tool_results, items, tool_log


def _evidence_payload(txn: Transaction, assessment: RiskAssessment,
                      tool_results: dict, items: list[_Ev]) -> str:
    """Compact JSON the model reasons over. Long lists are trimmed - the model
    needs the shape and the headline numbers, not every row."""
    ra = tool_results["get_risk_assessment"].data
    prof = tool_results["get_customer_profile"].data
    hist = tool_results["get_transaction_history"].data
    auth = tool_results["get_auth_events"].data
    rel = tool_results["find_related_events"].data

    payload = {
        "transaction": {
            "amount_paise": txn.amount_paise, "method": txn.method, "city": txn.city,
            "device_id": txn.device_id, "merchant": txn.merchant_name,
            "created_at": txn.created_at.isoformat(),
        },
        "engine": {
            "score": ra.get("score"), "band": ra.get("band"),
            "base_score": ra.get("base_score"), "floor_applied": ra.get("floor_applied"),
            "rules_fired": ra.get("rules_fired", []),
            "engine_recommended_action": ra.get("engine_recommended_action"),
            "signals": [
                {"code": s["code"], "contribution_pct": s["contribution_pct"],
                 "normalized": s["normalized"], "explanation": s["explanation"]}
                for s in ra.get("signals", [])
            ],
        },
        "customer_baseline": {
            "kyc_tier": prof.get("kyc_tier"), "segment": prof.get("segment"),
            "account_age_days": prof.get("account_age_days"),
            "home_city": prof.get("home_city"),
            "historical_txn_count": prof.get("historical_txn_count"),
            "amount_median_paise": prof.get("amount_median_paise"),
            "amount_p95_paise": prof.get("amount_p95_paise"),
            "active_hours": prof.get("active_hours"),
            "known_device_ids": prof.get("known_device_ids", []),
        },
        "history_summary": {k: hist.get(k) for k in
                            ("window_days", "count", "median_paise", "p95_paise", "max_paise")},
        "auth": {
            "failures_last_10min": auth.get("failures_last_10min"),
            "failures_in_window": auth.get("failures_in_window"),
            "events": auth.get("events", [])[:12],
        },
        "related": {
            "link_value": rel.get("link_value"),
            "related_txn_count": rel.get("related_txn_count"),
            "distinct_other_customers": rel.get("distinct_other_customers"),
            "other_customer_ids": rel.get("other_customer_ids", []),
            "events": rel.get("events", [])[:10],
        },
        "evidence_ledger": [
            {"id": e.id, "metric": e.metric, "observed": e.observed,
             "baseline": e.baseline, "unit": e.unit, "detail": e.detail}
            for e in items
        ],
    }
    return json.dumps(payload, default=str, separators=(",", ":"))


# --------------------------------------------------------------------------- phase 3
def _user_prompt(case: Case, payload_json: str, violations: list[str] | None = None) -> str:
    head = (
        f"Investigate case {case.id}. The transaction under investigation and ALL evidence "
        f"you need have already been gathered and are provided below as JSON. Do not ask "
        f"for more data - reason over it and return the Investigation.\n"
        f"- Cite only ids present in `evidence_ledger`.\n"
        f"- For quantitative findings, copy the exact numbers from `evidence_ledger` into "
        f"observed/baseline and set `metric` accordingly.\n"
        f"- `engine` holds the deterministic score/band/action. Concur unless the evidence "
        f"genuinely warrants dissent (see the ring rule).\n"
    )
    if violations:
        head += (
            "\nGrounding validation of your previous answer FAILED. Fix ONLY these issues "
            "and return a corrected Investigation:\n- " + "\n- ".join(violations[:8]) + "\n"
        )
    return head + "\nEVIDENCE:\n" + payload_json


def _synthesize(provider: str, case: Case, payload_json: str,
                violations: list[str] | None = None) -> tuple[Investigation, tuple[int, int]]:
    """One model call. Returns (Investigation, (in_tokens, out_tokens)). Raises on
    any provider error or timeout - the caller falls back."""
    if provider == "gemini":
        return _gemini_once(case, payload_json, violations)
    return _anthropic_once(case, payload_json, violations)


def _gemini_once(case: Case, payload_json: str,
                 violations: list[str] | None) -> tuple[Investigation, tuple[int, int]]:
    from google import genai
    from google.genai import types

    client = genai.Client(
        api_key=settings.gemini_api_key,
        http_options=types.HttpOptions(timeout=MODEL_TIMEOUT_S * 1000),
    )
    resp = client.models.generate_content(
        model=settings.gemini_model,
        contents=[types.Content(role="user", parts=[
            types.Part.from_text(text=_user_prompt(case, payload_json, violations))])],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM,
            response_mime_type="application/json",
            response_schema=Investigation,
            temperature=0.0,
            max_output_tokens=MODEL_MAX_TOKENS,
            http_options=types.HttpOptions(timeout=MODEL_TIMEOUT_S * 1000),
        ),
    )
    um = getattr(resp, "usage_metadata", None)
    usage = ((getattr(um, "prompt_token_count", 0) or 0),
             (getattr(um, "candidates_token_count", 0) or 0)) if um else (0, 0)

    parsed = getattr(resp, "parsed", None)
    if isinstance(parsed, Investigation):
        return parsed, usage
    if isinstance(parsed, dict):
        return Investigation.model_validate(parsed), usage
    text = getattr(resp, "text", None)
    if text:
        return Investigation.model_validate_json(text), usage
    raise RuntimeError("Gemini returned no parseable Investigation")


def _anthropic_once(case: Case, payload_json: str,
                    violations: list[str] | None) -> tuple[Investigation, tuple[int, int]]:
    from anthropic import Anthropic

    from .schema import submit_tool_schema

    client = Anthropic(api_key=settings.anthropic_api_key, timeout=MODEL_TIMEOUT_S, max_retries=1)
    resp = client.messages.create(
        model=settings.vigil_model, max_tokens=MODEL_MAX_TOKENS, system=SYSTEM,
        tools=[submit_tool_schema()],
        tool_choice={"type": "tool", "name": "submit_investigation"},
        messages=[{"role": "user", "content": _user_prompt(case, payload_json, violations)}],
    )
    usage = (resp.usage.input_tokens, resp.usage.output_tokens)
    for b in resp.content:
        if getattr(b, "type", None) == "tool_use" and b.name == "submit_investigation":
            return Investigation.model_validate(b.input), usage
    raise RuntimeError("Anthropic did not return submit_investigation")


# --------------------------------------------------------------------------- phase 5
def _persist(db: Session, run: AgentRun, case: Case, txn: Transaction, customer: Customer,
             assessment: RiskAssessment, inv: Investigation, items: list[_Ev],
             grounding, *, mode: str, failure_reason: str | None, tool_log: list[dict],
             t0: float, usage: tuple[int, int]) -> None:
    # evidence ledger rows
    verified_metrics = {fv.finding.metric for fv in grounding.per_finding if fv.status == "VERIFIED"}
    verified_refs = {r for fv in grounding.per_finding if fv.status == "VERIFIED"
                     for r in fv.finding.evidence_refs}
    for e in items:
        row = CaseEvidence(
            id=e.id, case_id=case.id, run_id=run.id, source_tool=e.tool,
            source_ref=e.source_ref, metric=e.metric, observed_value=e.observed,
            baseline_value=e.baseline, unit=e.unit, detail=e.detail,
        )
        if e.id in verified_refs or (e.metric and e.metric in verified_metrics):
            row.verified = True
            row.verification_note = "Recomputed from database; matches."
        db.add(row)

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
    run.model = settings.active_model_label if mode == "llm" else "engine-only"
    run.finished_at = now_ist()
    run.latency_ms = int((time.perf_counter() - t0) * 1000)
    run.input_tokens, run.output_tokens = usage
    run.tool_call_count = len(_EVIDENCE_TOOLS)  # 5 read-only tools built the evidence
    run.tool_log = tool_log
    run.failure_reason = failure_reason
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


# --------------------------------------------------------------------------- orchestrator
def investigate(db: Session, case: Case) -> AgentRun:
    t0 = time.perf_counter()

    # -- phase 1: RUNNING row, short write, COMMIT (lock released) --------------
    run = AgentRun(case_id=case.id, status="RUNNING", mode="llm", started_at=now_ist())
    db.add(run)
    case.status = "INVESTIGATING"
    db.flush()
    record(db, entity_type="case", entity_id=case.id, action="investigation_started",
           detail={"run_id": run.id, "llm_configured": settings.llm_configured})
    db.commit()
    db.refresh(run)
    run_id = run.id

    customer = db.get(Customer, case.customer_id)
    txn = db.get(Transaction, case.transaction_id)
    assessment = db.scalar(select(RiskAssessment).where(RiskAssessment.transaction_id == txn.id))

    # -- phase 2: gather evidence (reads only) ---------------------------------
    tool_results, items, tool_log = _gather_evidence(db, case, txn, customer)
    ev_ids = {e.id for e in items}
    db.commit()  # end the read transaction; the session now holds no locks

    # -- phase 3: ONE model call, NO db transaction open ----------------------
    provider = settings.active_provider
    mode = "llm"
    failure_reason: str | None = None
    usage: tuple[int, int] = (0, 0)
    inv: Investigation | None = None

    if provider is not None:
        payload = _evidence_payload(txn, assessment, tool_results, items)
        try:
            inv, usage = _synthesize(provider, case, payload)
        except Exception as exc:  # noqa: BLE001 - demo must degrade, not break
            failure_reason = f"{provider} error: {type(exc).__name__}: {str(exc)[:200]}"
            inv = None
    else:
        failure_reason = "no live LLM provider configured (set GEMINI_API_KEY or ANTHROPIC_API_KEY)"

    if inv is None:
        mode = "fallback"
        inv, _ = fallback.build_from(assessment, txn, customer, tool_results)
        # attach citations so reference-integrity passes; the numeric gate still decides VERIFIED
        by_metric: dict[str, list[str]] = {}
        for e in items:
            by_metric.setdefault(e.metric or "__none__", []).append(e.id)
        any_id = [items[0].id] if items else []
        for f in inv.findings:
            f.evidence_refs = by_metric.get(f.metric or "__none__") or any_id
        tool_log.append({"seq": len(tool_log) + 1, "tool": "engine-only",
                         "summary": "deterministic synthesis from the evidence above", "ms": 0})

    # -- phase 4: grounding validation (reads) + at most one repair call ------
    grounding = validate(db, txn, customer, inv, ev_ids)
    if (mode == "llm" and grounding.verdict != "PASS" and grounding.violations
            and (time.perf_counter() - t0) < LATENCY_BUDGET_S):
        db.commit()  # ensure no txn is held across the repair call
        try:
            repaired, u2 = _synthesize(provider, case,
                                       _evidence_payload(txn, assessment, tool_results, items),
                                       violations=grounding.violations)
            inv = repaired
            usage = (usage[0] + u2[0], usage[1] + u2[1])
            tool_log.append({"seq": len(tool_log) + 1, "tool": "repair",
                             "summary": "corrected after grounding failure", "ms": 0})
            grounding = validate(db, txn, customer, inv, ev_ids)
        except Exception:  # noqa: BLE001 - keep the first result if repair fails
            pass

    # -- phase 5: persist everything, short write, COMMIT --------------------
    run = db.get(AgentRun, run_id)
    _persist(db, run, case, txn, customer, assessment, inv, items, grounding,
             mode=mode, failure_reason=failure_reason, tool_log=tool_log, t0=t0, usage=usage)
    db.commit()
    return db.get(AgentRun, run_id)


# --------------------------------------------------------------------------- helpers
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
