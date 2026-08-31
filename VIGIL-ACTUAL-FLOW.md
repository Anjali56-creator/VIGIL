# VIGIL — ACTUAL FLOW (code-level audit)

This document describes **only what the code does**, file by file. No aspirations, no
marketing. Line references are to the repository at the time of writing.

---

## WHAT VIGIL ACTUALLY IS

Vigil is a **hybrid deterministic risk engine + LLM-assisted investigation + human decision log**.

| Layer | Implementation | Produces |
|---|---|---|
| Statistical anomaly scoring | `backend/risk/signals.py`, `backend/risk/fusion.py` — robust z-score, Poisson tail, grouped **noisy-OR** fusion, exact **leave-one-out** attribution. Hand-set constant weights. | `base_score` (0–100) + per-signal contribution % |
| Deterministic hard rules | `backend/risk/rules.py` — 3 rules, each imposes a **score floor** | `rules_fired`, `floor_applied` |
| Action policy | `backend/risk/policy.py::decide()` — a fixed `if/elif` matrix over (band, rules, amount, kyc) | `recommended_action` (binding, engine-owned) |
| AI investigation agent | `backend/agent/runner.py` — Gemini (`gemini-3.6-flash`) agentic tool-loop, or a deterministic fallback (`backend/agent/fallback.py`) | narrative, findings, `agent_risk_view`, advisory `recommended_action`, dissent |
| Grounding validation | `backend/agent/validator.py` — re-computes every numeric claim from the DB (±1% tolerance) | `grounding_verdict`, `claims_verified/claims_total` |
| Human analyst | `backend/services.py::apply_decision()` | `Decision` row, `Case.status`, append-only `AuditLog` |

**It is NOT:**

- a machine-learning fraud predictor — there is **no trained/loaded ML model anywhere**.
  `numpy` is used only for `median` / `percentile` / MAD on customer history. No
  sklearn / torch / tensorflow / joblib in `requirements.txt` or the code.
- a real-time payment interceptor — there is **no payment gateway**. "Block", "Hold",
  etc. write a `Decision` row and change `Case.status`. `Transaction.status` stays
  `"captured"`. Nothing is actually blocked.
- operating on real data — **100% synthetic**, generated locally by `backend/seed.py`.
  `is_attack` / `scenario_label` are ground-truth tags used only for the detection-rate
  metric; they are never shown to the engine or the AI.

The risk **score is fully deterministic and reproducible**. The **AI never computes the
score and never executes an action** — this is enforced by the schema
(`backend/agent/schema.py` has no numeric-score field) and the tool set
(`backend/agent/tools.py` — every tool is read-only).

---

## END-TO-END FLOW

```
USER ACTION            Frontend (backend/static/index.html, Alpine.js)
      |                    fetch()
      v
API                    FastAPI routers (backend/api/*.py)
      |
      v
BACKEND                services.py / seed.py
      |
      v
RISK ENGINE            backend/risk/engine.py::assess()
      |  signals -> fusion (noisy-OR) -> leave-one-out -> rules (floor) -> policy
      v
AI / LLM  (only on "Run AI investigation")
      |  backend/agent/runner.py::investigate()
      |  provider = Gemini | Anthropic | None -> deterministic fallback
      |  read-only tools -> evidence ledger -> structured Investigation -> grounding
      v
RECOMMENDATION         engine: RiskAssessment.recommended_action  (BINDING)
      |                 AI:     AgentRun.recommended_action        (ADVISORY)
      v
ANALYST ACTION         POST /api/cases/{id}/decision -> services.apply_decision()
      |
      v
FINAL STATE            Decision row + Case.status + AuditLog  (all persisted to SQLite)
```

---

## 1. Where synthetic/demo transactions are created

`backend/seed.py`. Three entry points:

1. **`seed(db)`** (`seed.py:286`) — called on startup if the DB is empty
   (`backend/main.py:36`) and by `POST /api/admin/reset`.
   - `_wipe(db)` deletes every table in reverse-FK order.
   - `_build_customers` creates **12 customers** `CUST-1001..CUST-1012`, each with a
     "personality": `amount_median/mad/p95`, `active_hour_start/end`, `home_city`,
     `known_device_ids`, `mean_hourly_txns`, `geo_radius_km`.
   - `_history_for` generates **~90 days of history per customer** (deterministic walk,
     occasional multi-day "trips" to another city so history has no teleport artefacts).
   - Every historical transaction is scored with `assess()`.
   - Injects **3 scripted historical attacks**: 2× `_account_takeover`, 1× `_card_testing`.
   - Deterministic: `rng = random.Random(42)` (`SEED = 42`).

2. **`inject_scenario(db, scenario, customer_id)`** (`seed.py:329`) — the live scenario
   launcher, called by `POST /api/simulate/scenario`. Uses `random.Random()` (unseeded,
   only for id suffixes). Builds one scenario "now", then opens a case **iff
   `assessment.score >= 60`**.

3. **`POST /api/transactions`** (`backend/api/transactions.py:48`) — generic single
   transaction ingest. Scores it; opens a case iff `score >= settings.case_open_threshold`
   (60). Used by the smoke test, not by the UI.

**Nature of the data:** predefined scenario *templates* with randomised numeric
parameters inside fixed ranges. No ML generation, no LLM generation.

---

## 2. What "Simulate Attack" / scenario launch actually does

Frontend → `POST /api/simulate/scenario {scenario: "..."}` →
`backend/api/admin.py::simulate()` → `seed.inject_scenario()`.

Scenario builders (all deterministic in structure, random only in exact numbers/ids):

### `_account_takeover` (`seed.py:164`) — victim `CUST-1002`
1. `far` = the city in `CITIES` **farthest** from the victim's home (`max(..., key=haversine)`).
2. `bad_device = "DEV-9xxx"`, `bad_ip = "185.x.x.x"`.
3. A **legit prior** transaction in the victim's **home city** at `t − 35 min`
   (amount ≈ median, victim's real device). Scored — this is what makes the later
   far-city transaction "impossible travel".
4. Auth events: LOGIN ok at `t − 11 min`; **3× `OTP_FAIL`** at `t − 7/5/3 min` on
   `bad_device`; LOGIN ok at `t − 1 min` on `bad_device`.
5. If `with_ring` (default): 2 random other customers each get a ring transaction on
   `bad_device`/`bad_ip` in the `far` city at `t − 15..38 min` (`is_attack=True`,
   `scenario_label="account_takeover_ring"`). Scored.
6. **Focus transaction**: `amount = victim.amount_median_paise × uniform(6.0, 8.5)`,
   UPI, merchant "Gift Card Store", `device=bad_device`, `ip_is_proxy=True`, city=`far`,
   `is_attack=True`, `scenario_label="account_takeover"`. Scored → this assessment is shown.
7. `inject_scenario` opens a case (score always ≥ 60 here; observed 97–98, CRITICAL).

### `_card_testing` (`seed.py:233`) — primary `CUST-1006`
- `bad_device = "DEV-8xxx"`, `bad_ip = "196.x.x.x"`.
- **8 small authorisations** (₹40–160) across the primary + 3 random other customers,
  each in that customer's home city, spread over ~1 hour, all on `bad_device`. Scored.
- 1 `PAYMENT_DECLINE` auth event (mild, not enough to trip the auth-storm rule).
- **Focus transaction**: small (₹60–130), primary's home city, `bad_device`.
- Each individual transaction is statistically *mild* (MEDIUM). The
  **`R_MULTI_CUSTOMER_DEVICE` rule floors it to HIGH** — a visible, explainable rule bump.

### `_normal` (`seed.py`) — victim `CUST-1001` *(added for the demo)*
- Known device, home city, active hour, `amount ≈ median × uniform(0.85, 1.2)`.
- No auth failures, no device sharing, no travel. Expected: **LOW → ALLOW**, no case.

### `_suspicious` (`seed.py`) — victim `CUST-1005` *(added for the demo)*
- **New device**, home city, active hour, `amount ≈ median × ~2.4` (below p95, so
  `DEV_NEW` stays at 0.6 and no `high_value` flag), no auth failures, no ring, no travel.
- Expected: **MEDIUM → MONITOR** (or HIGH → STEP_UP), no rule floor, no case.

---

## 3. Random / predefined / dynamic?

**Predefined scenario templates** with **parameterised randomness** inside fixed ranges.
Historical population is **seeded-deterministic** (`random.Random(42)`). No ML, no LLM
involvement in data creation.

---

## 4. How the risk score (e.g. 97/100) is calculated

`backend/risk/engine.py::assess()`:

1. **Context** — customer + prior transactions (≤200, `created_at ≤ txn time`) + prior
   auth events (≤100).
2. **6 signal detectors** (`signals.run_all`) — each returns
   `Signal(code, raw_value, baseline_value, unit, normalized∈[0,1], explanation)` with a
   constant `weight`. `triggered` ⇔ `normalized ≥ 0.15`.
3. **Fusion** (`fusion.fuse_score`) — grouped **noisy-OR**:
   - per-signal contribution `c = weight × normalized`
   - groups: `amount=(AMT_DEV)`, `velocity=(VEL_1H, AUTH_FAIL)`,
     `access=(DEV_NEW, GEO_DIST)`, `temporal=(TIME_ODD)`
   - within a group: `g = 1 − Π(1 − c)`; across groups: `p = 1 − Π(1 − g)`
   - `base_score = round(100 × p)`
4. **Leave-one-out attribution** (`fusion.leave_one_out`):
   `delta_i = fuse(all) − fuse(all without i)`; `share_i = 100 × delta_i / Σ delta`.
   Stored as `RiskSignal.contribution_pct`. Shares sum to ~100 % across triggered signals.
5. **Rules** (`rules.evaluate`) — `governing = max(fired rules, key=floor)`;
   `floor = governing.floor`, `floor_rule = governing.code`.
6. **Final score** `= max(base_score, floor)`, clamped `0..100`.
7. **Band** (`policy.band_for`): `<35 LOW`, `35–61 MEDIUM`, `62–81 HIGH`, `≥82 CRITICAL`.
8. **Action** (`policy.decide`) — see §12.
9. Persist `RiskAssessment` + one `RiskSignal` row per detector + `AuditLog(risk_assessed)`.

**Deterministic.** Same inputs → same score, every time. Weights are constants in
`signals.py::WEIGHTS`, not learned.

Worked example (a real Simulate-Attack run): `base_score = 47` from
`AMT_DEV 33% · AUTH_FAIL 28% · DEV_NEW 24% · GEO_DIST 16%` (of `base_score`), then
`R_IMPOSSIBLE_TRAVEL` (floor 85) and `R_MULTI_CUSTOMER_DEVICE` (floor 75) fire →
`score = max(47, 85) = 97` → band `CRITICAL` → `R_IMPOSSIBLE_TRAVEL` is a hard rule →
action `BLOCK`.

---

## 5. Every risk rule currently implemented

### Signals (`backend/risk/signals.py`) — `WEIGHTS`

| Code | Weight | Fires on | `normalized` formula |
|---|---|---|---|
| `AMT_DEV` | 0.80 | amount vs customer median | robust z `mz = 0.6745·(x−median)/MAD`; `1 − exp(−max(0,|mz|−4)/6)` for `mz>0`; near-constant spenders fall back to `(ratio−2)/6` |
| `VEL_1H` | 0.70 | txn count in the 1 h before this one | `clamp((k − (mean_hourly + 2)) / 5)` |
| `AUTH_FAIL` | 0.85 | failed `OTP_FAIL/PWD_FAIL/CVV_FAIL/PAYMENT_DECLINE` in the **10 min** before txn | `clamp(k / 4)` |
| `DEV_NEW` | 0.60 | `device_id ∉ customer.known_device_ids` | `0.6`; bumped to `1.0` if amount also `> amount_p95` |
| `GEO_DIST` | 0.50 | haversine km from home vs `geo_radius_km` | `clamp((dist/radius − 2) / 18)` — deliberately weak |
| `TIME_ODD` | 0.35 | hour outside `[active_hour_start, active_hour_end]` | `clamp(gap_hours / 4)` |

### Rules (`backend/risk/rules.py`) — impose a score **floor**

| Code | Floor | Hard? | Fires on |
|---|---|---|---|
| `R_IMPOSSIBLE_TRAVEL` | **85** | yes | a prior txn within 8 h where `0 < Δt ≤ 3 h`, distance `≥ 300 km`, implied speed `> 900 km/h` |
| `R_MULTI_CUSTOMER_DEVICE` | **75** | yes | same `device_id` used by `≥ 2` **other** customers in the last **24 h** |
| `R_AUTH_STORM` | **65** | no | `≥ 3` failed `OTP_FAIL/PWD_FAIL/CVV_FAIL` in the **5 min** before a successful txn |

"Hard" (`HARD_RULES`) ⇒ the recommended action escalates to `BLOCK` when the band is
CRITICAL. A rule can only **raise** the score to its floor — never lower it, never be
averaged away by the statistical layer.

---

## 6. What triggers the named rules

- **`R_IMPOSSIBLE_TRAVEL`** — `rules.py::_impossible_travel`. In `_account_takeover` the
  legit prior transaction (home city, `t − 35 min`) and the far-city focus transaction
  are ~1500–2000 km apart in 35 min ⇒ ~2600–3400 km/h ⇒ fires, floor 85.
- **`R_MULTI_CUSTOMER_DEVICE`** — `rules.py::_multi_customer_device`. The ring
  transactions (account-takeover) or the 8 cross-account authorisations (card-testing)
  put `bad_device` on ≥ 2 other customers within 24 h ⇒ fires, floor 75.
- **Abnormal amount** — signal `AMT_DEV`, not a rule. Driven by robust z-score vs the
  customer's own median/MAD.
- **Failed / declined attempts** — signal `AUTH_FAIL` (10-min window) and, at ≥ 3 in
  5 min, also rule `R_AUTH_STORM` (floor 65).
- **New device** — signal `DEV_NEW`.
- **`R_AUTH_STORM`** — 3 `OTP_FAIL` in 5 min; present in `_account_takeover` (t−7/5/3),
  so it fires too, but 85 > 65 so `R_IMPOSSIBLE_TRAVEL` governs.

---

## 7. Deterministic or ML/AI?

**Fully deterministic.** No trained model, no inference library, no learned parameters.
The score is arithmetic over hand-set constants. Reproducible bit-for-bit given the same
DB state.

---

## 8. Exactly where Gemini is used

- **Provider selection** — `backend/config.py::Settings.active_provider`:
  `"gemini"` if `GEMINI_API_KEY` set, else `"anthropic"` if `ANTHROPIC_API_KEY`, else
  `None`. This is the only place the provider is chosen.
- **`backend/agent/runner.py::investigate()`** → `_run_gemini()` when provider is
  `"gemini"`. Model id from `settings.gemini_model` (`GEMINI_MODEL` env, default
  `gemini-3.6-flash`).
- `_run_gemini` runs an **agentic tool loop** (≤ 8 iterations): Gemini is given the 5
  read-only tools as function declarations with `automatic_function_calling` **disabled**
  (the app drives the loop so every call is logged and every returned value is written to
  the evidence ledger *before* it can be cited). Then one **structured-output call**
  (`response_schema=Investigation`, `temperature=0.0`) produces the final JSON. Then
  grounding validation + **one optional repair turn**.
- Gemini is called **only** from `POST /api/cases/{id}/investigate` (the "Run AI
  investigation" button). Not on ingest, not on scoring, not on the dashboard, not on
  the analyst decision.

---

## 9. What Gemini contributes

From `backend/agent/prompts.py` (SYSTEM prompt) and `backend/agent/schema.py`
(`Investigation` model — **note: no numeric score field exists**):

- `investigation_summary` — 3–5 sentence narrative
- `behavioral_deviation` — how current behaviour compares with the customer's history
- `related_activity` — cross-account / shared-device / ring observations
- `findings[]` — each a claim; quantitative ones must set `metric` + `observed` +
  `baseline` + `evidence_refs` (re-verified by the backend)
- `agent_risk_view` — qualitative LOW/MEDIUM/HIGH/CRITICAL **only** (prompt hard-rule #1:
  "You do NOT produce a numeric 0-100 risk score")
- `concurs_with_engine` + `dissent_reason` — a **second opinion**
- `recommended_action` — **advisory only** (prompt hard-rule #2: "A human decides")
- `confidence`, `requires_human_review`

So Gemini's role is: **investigation + explanation + second opinion**. Not prediction,
not scoring, not the binding recommendation, not any action.

---

## 10. What happens when Gemini fails or hits quota

`runner.investigate()` wraps the provider call in `try/except Exception`
(`runner.py:152`). On **any** failure — `429 RESOURCE_EXHAUSTED`, timeout, JSON parse
error, iteration-budget exhaustion — it:

1. deletes any partial `CaseEvidence` / `AgentFinding` rows for this run,
2. keeps the `AgentRun` row,
3. calls `_run_fallback(reason="gemini error: <type>: <message>")`.

The raw provider error text is stored in `AgentRun.failure_reason` (surfaced only in the
UI's Technical Details). If no provider key is configured at all, it goes straight to
fallback with `reason="no live LLM provider configured"`.

`_run_fallback` → `backend/agent/fallback.py::build()`: a **deterministic engine-only
investigation**. It calls the same 5 tool functions directly (no LLM), builds findings
from the engine's triggered signals, adds a ring finding if a shared device is seen, and
applies one **fixed escalation rule**: if `ring_customers ≥ 2` and the engine action is
`MONITOR/STEP_UP_AUTH/HOLD_FOR_REVIEW` → `concurs=False`, `recommended_action="BLOCK"`,
`agent_risk_view` raised to CRITICAL, canned `dissent_reason`. `run.mode = "fallback"`,
`run.model = "engine-only"`, `run.status = "FALLBACK"`.

---

## 11. Does the engine-only fallback change the result?

| Changes | Does **not** change |
|---|---|
| the investigation narrative (deterministic template text, not model prose) | the risk **score / band / base_score / rules** (already computed by the engine before any investigation) |
| the dissent is a **fixed rule**, not a model judgement | the engine's **binding `recommended_action`** |
| `mode="fallback"`, `model="engine-only"`, badged in the UI | the analyst's available actions or the decision flow |

Grounding still runs and passes (`claims_verified == claims_total`) because the fallback
cites real tool numbers. The seeded card-testing case is tuned so the fallback dissents
reproducibly (asserted by `tests/test_smoke.py`).

---

## 12. Where the final recommendation comes from

`backend/risk/policy.py::decide(band, rules_fired, amount_paise, amount_p95_paise, kyc_tier)`
→ `(recommended_action, requires_human_review)`. **Binding**, stored on
`RiskAssessment.recommended_action`.

```
LOW       -> ALLOW           , review = False
MEDIUM    -> MONITOR         , review = False
HIGH      -> HOLD_FOR_REVIEW if (amount > p95 or any rule fired) else STEP_UP_AUTH , review = True
CRITICAL  -> BLOCK if any HARD rule fired else HOLD_FOR_REVIEW                      , review = True
```

`kyc_tier` is passed but currently unused in the body. The AI's `recommended_action` is a
separate, advisory value on `AgentRun`.

---

## 13. What happens technically on each analyst action

Frontend `chooseAction(a)` sets `pendingAction`; **Confirm** → `submitDecision()`:

- derives `decision`: `OVERRIDE` if `final_action ≠ engine action`; else `REJECT` for
  `BLOCK`/`HOLD_FOR_REVIEW`; else `APPROVE`.
- `POST /api/cases/{id}/decision {decision, final_action, override_reason}`.

`backend/api/decisions.py::decide` → `backend/services.py::apply_decision`:

1. `is_override = (decision == "OVERRIDE") or (final_action != engine_action)`.
2. If `is_override` and `override_reason` is blank → `ValueError` → **HTTP 422**
   (the frontend requires a reason in this case).
3. Insert a `Decision` row: `actor="analyst@demo"`, `engine_recommended_action`,
   `ai_recommended_action`, `final_action`, `override_reason`.
4. Set `Case.status`:
   - override / diverges → `BLOCKED` if `final_action ∈ {BLOCK, HOLD_FOR_REVIEW}` else `APPROVED`
   - otherwise → `APPROVE→APPROVED`, `REJECT→BLOCKED`, `ESCALATE→REVIEW_REQUIRED`
   - `APPROVED`/`BLOCKED` also set `closed_at = now`
5. `AuditLog(action="analyst_decision", detail={...})` — append-only.
6. `db.commit()`.

**Honest limitation:** the five buttons differ in the **recorded** `final_action`,
`decision`, `override_reason` and audit entry, but the **status** collapses to two
outcomes — `BLOCK`/`HOLD_FOR_REVIEW` → `BLOCKED`; `STEP_UP_AUTH`/`MONITOR`/`ALLOW` →
`APPROVED`. **No payment is actually blocked, held or stepped-up.**
`Transaction.status` never changes. There is no gateway. The product is a
decision-support and audit tool, not an enforcement point.

---

## 14. Persisted or just frontend state?

**Persisted.** Every mutation is a SQLAlchemy write to SQLite (`vigil.db`, or
`DATABASE_URL`). Tables: `customers`, `transactions`, `auth_events`, `risk_assessments`,
`risk_signals`, `cases`, `case_evidence`, `agent_runs`, `agent_findings`, `decisions`,
`audit_log`. The frontend re-fetches `/api/cases/{id}` after every action, so the screen
reflects server state. Nothing material is faked in JavaScript.

(On Render the SQLite file is on ephemeral disk, so it resets on redeploy; within a
running session it is real persistence. `POST /api/admin/reset` re-seeds deterministically.)

---

## 15. What happens after the analyst decides

- `Decision` row committed; `Case.status` → `BLOCKED` / `APPROVED` / `REVIEW_REQUIRED`;
  `closed_at` set if closed.
- Audit gains `analyst_decision`.
- Frontend reloads the case (now shows the resolved state, decision buttons hidden) and
  the dashboard (`open_cases`, `median_time_to_decision_s` update).
- `POST /api/cases/{id}/investigate` now returns **409** for `APPROVED`/`BLOCKED`/`RESOLVED`.
- No notification, no gateway call, no transaction mutation, no further automation.

---

## API surface (for reference)

| Method + path | Handler | Effect |
|---|---|---|
| `GET /api/health` | `main.py` | db ok, `llm_configured`, active model label |
| `GET /api/dashboard/metrics` | `api/dashboard.py` | counts, detection rate, median TTD, risk mix |
| `GET /api/cases` | `api/cases.py` | case queue, priority-ordered |
| `GET /api/cases/{id}` | `api/cases.py` | full aggregate (txn, customer, assessment, timeline, related, evidence, runs, decisions, audit) |
| `POST /api/cases/{id}/investigate` | `api/investigations.py` | run the agent (Gemini or fallback); 409 if case closed |
| `POST /api/cases/{id}/decision` | `api/decisions.py` | record analyst decision; 422 if override without reason |
| `GET /api/transactions?limit=` | `api/transactions.py` | recent transactions with scores |
| `GET /api/transactions/{id}` | `api/transactions.py` | one transaction + assessment (+ customer, added for the demo) |
| `POST /api/transactions` | `api/transactions.py` | ingest + score one transaction |
| `POST /api/simulate/scenario` | `api/admin.py` | build a scenario now (`normal` / `suspicious` / `account_takeover` / `card_testing`) |
| `POST /api/admin/reset` | `api/admin.py` | wipe + re-seed deterministically |
