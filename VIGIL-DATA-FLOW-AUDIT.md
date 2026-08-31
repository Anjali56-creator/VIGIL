# VIGIL — DATA-FLOW AUDIT OF ONE TRANSACTION

Every statement below was verified against the code and against a **live run** of the
application. The files audited (`backend/risk/*`, `backend/agent/*`,
`backend/services.py`, `backend/api/*`, `backend/models.py`) are **byte-identical to
git HEAD** — `git diff HEAD` on them is empty. Only `backend/seed.py` (added
`_normal` / `_suspicious` generators, pinned the card-testing hour),
`backend/api/transactions.py` (added a `customer` field to one response) and
`backend/static/index.html` (UI) were changed in earlier tasks; none of those affect
the trace below.

---

## THE TRANSACTION USED FOR THIS AUDIT

Live run, captured now:

```
transaction_id : txn_ato_cust-1002_429699d8
amount_paise   : 5176246        ->  Rs 51,762.46
method         : upi
merchant_name  : Gift Card Store
merchant_mcc   : 5947
device_id      : DEV-9312
ip_addr        : 185.66.151.201
ip_is_proxy    : 1 (true)
city           : Kolkata
lat, lng       : 22.5784, 88.3382
created_at     : 2026-08-31 19:52:36.631044
status         : captured
is_attack      : 1 (true)        <- ground-truth tag, hidden from engine & agent
scenario_label : account_takeover
```

Row exists in SQLite table **`transactions`**. Its risk assessment
(`base_score 95 -> score 95 CRITICAL -> BLOCK`) is one row in **`risk_assessments`**,
with six rows in **`risk_signals`**.

### Why the number isn't exactly "Rs 90,476.62 / 97"

The screenshot you quoted (`Rs 90,476.62`, `97/100`) is `~6.8x` the customer median.
That came from the **old** amount formula `median * uniform(6.0, 8.5)`. In the previous
"explainable score" task that multiplier was lowered to `uniform(3.2, 4.2)` (see
`backend/seed.py::_account_takeover`), so a current run shows `Rs 51,762.46` = `3.9x`
median and a score of `94-97` (the last point or two float because the amount is
randomised). **The structure, sources and logic are identical** — only the randomised
rupee amount and ~2 points of score differ. Everything below is the real mechanism.

---

## 1. WHERE DOES THIS TRANSACTION COME FROM?

**THE TRANSACTION COMES FROM: `backend/seed.py`, function `_account_takeover()`
(lines ~164–230), invoked by `inject_scenario()` (line ~329), inserted as a row in
the SQLite table `transactions`, then scored by `backend/risk/engine.py::assess()`.**

It is **not**: hard-coded mock data, a JSON file, a frontend constant, an external
API, or an AI model. It **is**: backend-generated synthetic data from a scripted
template with a few randomised numbers, written to the database, read back through
the API.

Chain:

| Stage | Exact location |
|---|---|
| Button | `backend/static/index.html` — `<button @click="runScenario()">▶ Run scenario</button>` (before the UI redesign this was `@click="simulate()"` labelled "⚡ Simulate attack"; the endpoint is unchanged) |
| Frontend fn | `runScenario()` in `index.html` → `POST /api/simulate/scenario` with `{scenario:"account_takeover"}` |
| Route | `backend/api/admin.py:24` `@router.post("/simulate/scenario")` → `simulate()` → `inject_scenario(db, body.scenario, body.customer_id)` |
| Victim pick | `backend/seed.py` `_DEFAULT_VICTIM = {"account_takeover": "CUST-1002", ...}` → `victim = db.get(Customer, "CUST-1002")` |
| Builder pick | `backend/seed.py` `_BUILDERS = {"account_takeover": _account_takeover, ...}` |
| Data generation | `backend/seed.py::_account_takeover(db, victim, when=now_ist(), historical=False, rng=random.Random())` — builds a "legit prior" txn, 3 `OTP_FAIL` auth events, 2 ring txns, and the focus txn |
| Scoring | `backend/risk/engine.py::assess(db, txn)` (called inside `_account_takeover` for every txn it makes) |
| Case open | `backend/seed.py::inject_scenario` → `if assessment.score >= 60: open_case(db, txn, assessment)` → `backend/services.py::open_case()` inserts a `cases` row + `audit_log` `case_opened` |
| DB write | `db.commit()` at the end of `inject_scenario` |
| Display | `runScenario()` → `openCase(case_id)` → `GET /api/cases/{id}` → `backend/api/cases.py:61 case_detail()` aggregates from the DB |

The 12 customers and their 90-day histories (which give `CUST-1002` its `median
Rs 13,301`, `home_city Pune`, `known device DEV-1007`) come from
`backend/seed.py::seed()` — `random.Random(42)`, run once on an empty DB
(`backend/main.py:36`) and again by `POST /api/admin/reset`.

---

## 2. WHAT HAPPENS WHEN YOU CLICK "RUN SCENARIO" / "SIMULATE ATTACK"?

```
Click "▶ Run scenario"  (scenario = "account_takeover")
        |   backend/static/index.html
        v
runScenario()                          async; this.api('/api/simulate/scenario', POST, {scenario})
        |
        v
POST /api/simulate/scenario            HTTP
        |
        v
backend/api/admin.py::simulate()       validates scenario in SCENARIOS; calls inject_scenario()
        |
        v
backend/seed.py::inject_scenario(db, "account_takeover", None)
        |   cid = _DEFAULT_VICTIM["account_takeover"] = "CUST-1002"
        |   victim = db.get(Customer, "CUST-1002")
        |   txn = _BUILDERS["account_takeover"](db, victim, when=now_ist(), historical=False, rng=random.Random())
        v
backend/seed.py::_account_takeover(...)
        |   far  = max(CITY_NAMES, key=lambda x: haversine_km(Pune, CITIES[x]))   -> "Kolkata"
        |   bad_device = "DEV-9" + rng.randint(100,999)                           -> e.g. DEV-9312
        |   bad_ip     = "185." + 3x rng.randint(1,250)
        |   1) insert legit_prior txn: Pune, amount = median*uniform(0.4,1.1), device=DEV-1007, t-35min ; assess()
        |   2) insert AuthEvent LOGIN ok (t-11m); 3x AuthEvent OTP_FAIL (t-7m,t-5m,t-3m) on bad_device ; LOGIN ok (t-1m)
        |   3) for peer in rng.sample(other_customers, 2): insert ring txn on bad_device in Kolkata, amount rng.randint(4000,25000) ; assess()
        |   4) insert FOCUS txn: amount = median * uniform(3.2,4.2), upi, "Gift Card Store"/5947,
        |      device=bad_device, ip_is_proxy=True, city="Kolkata", is_attack=True, scenario_label="account_takeover"
        |   5) assessment = assess(db, focus_txn)
        v
backend/risk/engine.py::assess()       signals -> fusion (noisy-OR) -> leave-one-out -> rules -> policy
        |   writes RiskAssessment + 6x RiskSignal rows + audit_log "risk_assessed"
        v
back in inject_scenario: assessment.score (95) >= 60  -> open_case(db, txn, assessment)
        |   backend/services.py::open_case()  -> insert cases row (id "CASE-2026-0004") + audit_log "case_opened"
        v
db.commit()
        |
        v
return {scenario, transaction_id, customer_id, score:95, band:"CRITICAL",
        recommended_action:"BLOCK", requires_human_review:true, case_id:"CASE-2026-0004"}
        |
        v
runScenario(): r.case_id present -> openCase("CASE-2026-0004")
        |   GET /api/cases/CASE-2026-0004
        v
backend/api/cases.py::case_detail()    reads cases + transactions + customers + risk_assessments
                                       + risk_signals + related_events() + timeline + evidence + runs + decisions + audit
        |
        v
Case screen renders (4 numbered sections)
```

`GET /api/cases/{id}` calls `backend/services.py::related_events()` and
`build_timeline()`; it does **not** call any AI. The AI runs only later, if the
analyst clicks "Run AI investigation".

---

## 3. IS THE TRANSACTION RANDOM?

Answering your A–F exactly:

- **A. Randomly generated?** — *Partly.* A fixed template with a handful of
  `rng` draws (see the list below). `rng = random.Random()` (unseeded) in
  `inject_scenario`, so ids and the exact amount differ every click.
- **B. Selected from predefined synthetic scenarios?** — *Yes, the template is
  predefined.* `SCENARIOS = ("normal","suspicious","account_takeover","card_testing")`
  in `backend/seed.py`; the builder is `_account_takeover`.
- **C. Generated deterministically?** — *The population is* (`random.Random(42)` in
  `seed()`). *The injected scenario is not* — it uses fresh randomness.
- **D. Fetched from a database?** — *After creation, yes.* The screen reads it from
  SQLite. It is written to the DB the moment it is generated.
- **E. Generated by an AI model?** — **No.** No LLM is involved in creating,
  selecting or scoring the transaction.
- **F. Something else?** — No.

**What is randomised in `_account_takeover` (the only randomness):**

| Value | Code | Range |
|---|---|---|
| focus amount | `int(victim.amount_median_paise * rng.uniform(3.2, 4.2))` | 3.2–4.2× the customer's median |
| device id | `f"DEV-9{rng.randint(100, 999)}"` | `DEV-9100`–`DEV-9999` |
| IP | `f"185.{rng.randint(1,250)}.{rng.randint(1,250)}.{rng.randint(1,250)}"` | a `185.x.x.x` address |
| legit-prior amount | `int(victim.amount_median_paise * rng.uniform(0.4, 1.1))` | 0.4–1.1× median |
| ring peers | `rng.sample(other_customers, 2)` | 2 of the other 11 customers |
| ring amounts | `rng.randint(4000, 25000)` paise | Rs 40–250 |
| ring timing | `t - timedelta(minutes=rng.randint(15, 38))` | 15–38 min before |
| jittered coords | `_jitter(*CITIES[far], rng, 6)` | ±~6 km around Kolkata |
| id hex suffix | `rng.randrange(16**8)` | cosmetic |

**What is fixed (not random):**

- victim `CUST-1002`; far city = deterministically the farthest CITY from Pune = **Kolkata**;
- merchant "Gift Card Store", MCC `5947`, method `upi`, `ip_is_proxy=True`;
- exactly 3 `OTP_FAIL` events at `t-7/5/3` min; a LOGIN ok at `t-11` and `t-1`;
- the legit prior transaction sits in Pune 35 min earlier (this is what makes the
  jump to Kolkata "impossible travel");
- `is_attack=True`, `scenario_label="account_takeover"`.

**The template is `backend/seed.py::_account_takeover` (lines ~164–230).** There is no
JSON file and no stored transaction record for it — it is constructed in Python each
time.

---

## 4. WHY DOES VIGIL SHOW THIS PARTICULAR TRANSACTION?

Two separate reasons:

1. **Why this transaction is created:** you asked for it. Clicking Run scenario with
   `scenario="account_takeover"` calls `_account_takeover` on the hard-coded default
   victim `CUST-1002` (`_DEFAULT_VICTIM["account_takeover"]` in `backend/seed.py`).
   "Kolkata" is not chosen at random — `_account_takeover` picks
   `far = max(CITY_NAMES, key=lambda x: haversine_km(victim.home, CITIES[x]))`, and
   Kolkata is the farthest of the 12 seeded cities from Pune. "Gift Card Store /
   5947 / UPI" are literals in that function (a high-risk cash-out merchant category).

2. **Why the screen jumps straight to it:** `inject_scenario` returns `case_id`, and
   `runScenario()` immediately calls `openCase(case_id)`. When a scenario scores below
   60 (Normal, Suspicious) there is no `case_id`, and `runScenario()` calls
   `openTxn(transaction_id)` instead (a read-only result page).

3. **Why it sits at the top of the dashboard queue:** `backend/api/cases.py::list_cases`
   sorts by `_PRIORITY_ORDER = {"CRITICAL":0,"HIGH":1,...}` then newest first, so a
   fresh CRITICAL case is first.

---

## 5. HOW IS THE RISK SCORE CALCULATED? (for this exact transaction)

Source: `backend/risk/engine.py::assess()` → `backend/risk/signals.py` →
`backend/risk/fusion.py` → `backend/risk/rules.py` → `backend/risk/policy.py`.
**Deterministic. No AI.**

### Step A — the 6 signal detectors (`signals.py`, live values from this run)

| Signal | Weight | `normalized` | Why (from `explanation`, verbatim) | `contribution_pct` |
|---|---|---|---|---|
| `AUTH_FAIL` | 0.85 | 0.750 | "3 failed authentication events (OTP_FAIL) in the 10 minutes before this transaction." `norm = clamp(3/4)` | **38.4 %** |
| `DEV_NEW` | 0.60 | 1.000 | "Device DEV-9312 has never been seen on this account (known devices: ['DEV-1007']). First use is also a high-value transaction." `norm = 0.6`, doubled to `1.0` because amount > p95 | **32.8 %** |
| `GEO_DIST` | 0.50 | 1.000 | "Transaction originated in Kolkata, 1573 km from the customer's usual area around Pune (typical radius 30 km)." `norm = clamp((1573/30 − 2)/18)` → clamps to 1.0 | **21.8 %** |
| `AMT_DEV` | 0.80 | 0.302 | "Rs 51,762.46 is 3.9x the customer's median of Rs 13,301.21 (robust z-score 6.2)." `norm = 1 − exp(−max(0,6.2−4)/6)` ≈ 0.302 | **7.0 %** |
| `VEL_1H` | 0.70 | 0.000 | "2 transactions in the hour before this one; the customer averages 0.06/hour." below threshold | 0 % |
| `TIME_ODD` | 0.35 | 0.000 | "19:00 is within the customer's active window 07:00-23:00." | 0 % |

`contribution_pct` is stored per row in `risk_signals`; it is the exact **leave-one-out**
attribution from `fusion.leave_one_out()`.

### Step B — fusion (`fusion.py::fuse_score`, grouped noisy-OR)

Per-signal `c = weight × normalized`:
`AUTH_FAIL 0.6375 · DEV_NEW 0.60 · GEO_DIST 0.50 · AMT_DEV 0.2416 · VEL_1H 0 · TIME_ODD 0`

Groups (from `fusion.GROUPS`): within group `g = 1 − Π(1 − c)`; across groups
`p = 1 − Π(1 − g)`:

```
amount   : 1 − (1−0.2416)                     = 0.2416
velocity : 1 − (1−0)(1−0.6375)                = 0.6375
access   : 1 − (1−0.60)(1−0.50)               = 0.80
temporal : 0

p = 1 − (1−0.2416)(1−0.6375)(1−0.80)(1−0)
  = 1 − 0.7584 × 0.3625 × 0.20
  = 1 − 0.05498
  = 0.94502

base_score = round(100 × 0.94502) = 95      (matches risk_assessments.base_score)
```

### Step C — deterministic rules (`rules.py`, live values)

| Rule | Floor | Fired? | Detail (verbatim) |
|---|---|---|---|
| `R_IMPOSSIBLE_TRAVEL` | 85 | **yes** | "1573 km from the previous transaction in Pune in 35 min implies 2696 km/h - physically impossible." |
| `R_MULTI_CUSTOMER_DEVICE` | 75 | **yes** | "Device DEV-9312 was used by 2 other customers in the last 24h - consistent with a fraud ring." |
| `R_AUTH_STORM` | 65 | no (65 < 85, would not govern anyway) | — |

`governing = max(fired, key=floor)` → `R_IMPOSSIBLE_TRAVEL`, floor 85.

### Step D — final score, band, action

```
score  = max(base_score, governing floor) = max(95, 85) = 95     (risk_assessments.score)
band   = band_for(95): 95 >= 82  -> "CRITICAL"                    (policy.band_for)
action = decide("CRITICAL", rules_fired, amount, p95, kyc):
         CRITICAL + a HARD rule fired ("R_IMPOSSIBLE_TRAVEL")  -> "BLOCK"   (policy.decide)
```

So `95 / 100 → CRITICAL → BLOCK`. In this run the statistical `base_score` (95) is
already above both rule floors, so the rules **confirm** the score rather than raise
it. In runs where the randomised amount is lower, `base_score` lands in the 60s–80s
and `R_IMPOSSIBLE_TRAVEL` visibly lifts it to 85. The card-testing scenario always
shows the rule doing the lifting (base ~50 → floor 75).

**The "97/100" from your screenshot** was the same calculation with a larger
randomised amount (old `6.0–8.5×` multiplier) pushing `AMT_DEV.normalized` higher,
so `base_score` reached 97.

---

## 6. WHERE DO THE SIGNAL VALUES COME FROM?

| On-screen value | Source (table / field) | Logic that uses it | Effect on score |
|---|---|---|---|
| **Impossible travel** ("1573 km … 2696 km/h") | `transactions.lat/lng` of the focus txn (Kolkata) and of `txn_pre_cust-1002_...` (Pune, `created_at = t − 35 min`, inserted by `_account_takeover`). Distance via `util.haversine_km`. | `rules.py::_impossible_travel` — scans prior txns ≤ 8 h; needs `Δt ≤ 3 h`, `dist ≥ 300 km`, `speed > 900 km/h` | Rule floor **85** (`R_IMPOSSIBLE_TRAVEL`), hard rule → forces `BLOCK` at CRITICAL |
| **New device** ("DEV-9312") | `transactions.device_id` vs `customers.known_device_ids` (`["DEV-1007"]`, computed in `seed._build_customers`) | `signals.py::device_novelty` — not in the set → `norm 0.6`, doubled to `1.0` because `amount > customers.amount_p95_paise` | Signal `DEV_NEW`, `w 0.60`, **32.8 %** of base |
| **Failed / declined attempts** ("3 OTP_FAIL") | 3 rows in `auth_events` (`type="OTP_FAIL"`, `success=0`, `created_at = t−7/−5/−3 min`), inserted by `_account_takeover` | `signals.py::auth_failures` counts fails in the 10-min window → `norm = clamp(3/4) = 0.75`. Also `rules.py::_auth_storm` (≥3 in 5 min) fires floor 65 | Signal `AUTH_FAIL`, `w 0.85`, **38.4 %** of base; plus rule `R_AUTH_STORM` (not governing) |
| **Shared device** ("used by 2 other customers") | `transactions` rows with the same `device_id` = `DEV-9312`, different `customer_id` — the 2 ring txns for `rng.sample(others, 2)` (here CUST-1010, CUST-1005), in `related_events` on screen | `rules.py::_multi_customer_device` — ≥ 2 distinct other customers on that device in 24 h → floor 75. Also `agent/tools.py::find_related_events` surfaces it to the AI. On screen: `services.py::related_events()` (48-h window) | Rule floor **75** (`R_MULTI_CUSTOMER_DEVICE`), hard rule |
| **Unusual amount** ("Rs 51,762.46 … 3.9x median, z 6.2") | `transactions.amount_paise` (5,176,246) vs `customers.amount_median_paise` (1,330,121) and `amount_mad_paise` — both computed by `seed._history_for` from 90 days of synthetic history | `signals.py::amount_deviation` — robust z `mz = 0.6745·(x−median)/MAD ≈ 6.2`; `norm = 1 − exp(−(6.2−4)/6) ≈ 0.302` | Signal `AMT_DEV`, `w 0.80`, **7.0 %** of base |
| **Location** ("Kolkata", 1573 km) | `transactions.city/lat/lng` (Kolkata, jittered) vs `customers.home_lat/lng` (Pune) and `geo_radius_km` (30) | `signals.py::geo_distance` — `ratio = 1573/30`; `norm = clamp((ratio−2)/18)` → 1.0 (deliberately weak alone; corroborated here) | Signal `GEO_DIST`, `w 0.50`, **21.8 %** of base |
| **Customer history** (median Rs 13,301, home Pune, active 07–23, 1 known device) | `customers` row for CUST-1002; the numeric baselines were computed by `seed._history_for` over ~`n` synthetic transactions using `numpy.median` / `percentile` / MAD | Read by every signal detector and by the agent tools as the "normal" to compare against | Sets the thresholds every signal is measured against |
| **Device ID** ("DEV-9312") | `transactions.device_id`, literally `f"DEV-9{rng.randint(100,999)}"` in `_account_takeover` | see "New device" and "Shared device" above | via `DEV_NEW` + `R_MULTI_CUSTOMER_DEVICE` |
| **IP address** ("185.66.151.201", proxy) | `transactions.ip_addr` = `f"185.{rng…}.{rng…}.{rng…}"`; `ip_is_proxy=True` (literal) | `services.py::related_events` also links on shared IP (shown as `shared_ip` rows). **`ip_is_proxy` is stored but not currently read by any signal or rule.** | IP contributes only via the shared-IP related-events list; no direct score effect |

---

## 7. WHAT IS ACTUALLY AI?

- **Which file calls Gemini:** `backend/agent/runner.py` — function `_run_gemini()`
  (and its helpers `_gemini_client()`, `_gemini_submit()`), reached from
  `investigate()` when `settings.active_provider == "gemini"`
  (`backend/config.py`). The SDK is `google.genai`.
- **Which function / trigger:** only `POST /api/cases/{id}/investigate`
  (`backend/api/investigations.py::start_investigation` → `runner.investigate`),
  i.e. the **"Run AI investigation"** button in case step 4. Nowhere else — not on
  Run scenario, not on scoring, not on the dashboard, not on the decision.
- **What data is sent to Gemini:** a system prompt (`backend/agent/prompts.py`), a
  short user message naming the case/transaction/customer id and the engine's score,
  and the results the model pulls itself via 5 **read-only** tools
  (`backend/agent/tools.py`): `get_risk_assessment`, `get_customer_profile`,
  `get_transaction_history`, `get_auth_events`, `find_related_events`. Then a final
  structured-output call with `response_schema=Investigation`.
- **What Gemini returns:** an `Investigation` object (`backend/agent/schema.py`) —
  `investigation_summary`, `findings[]` (each may name a verifiable `metric` +
  `observed`/`baseline` + `evidence_refs`), `behavioral_deviation`,
  `related_activity`, `agent_risk_view` (LOW/MEDIUM/HIGH/CRITICAL — **a word, not a
  number**), `concurs_with_engine` + `dissent_reason`, an advisory
  `recommended_action`, `confidence`, `requires_human_review`.
- **Does Gemini calculate the risk score?** **No.** The `Investigation` schema has
  **no numeric-score field**. The score in `risk_assessments` is written by
  `engine.py::assess()` before any investigation can run.
- **Does Gemini decide Block/Hold/Allow?** **No.** Its `recommended_action` is
  advisory. The binding recommendation is `policy.decide()`. The final action is the
  analyst's (`services.apply_decision`).
- **Does Gemini only investigate / explain?** **Yes** — investigate, explain, and
  offer a second opinion (agree or dissent). It has no tool that writes anything.
- **What happens when Gemini fails?** `runner.investigate()` wraps the call in
  `try/except Exception` (line ~152). On **any** error — `429 RESOURCE_EXHAUSTED`,
  timeout, JSON parse failure, iteration-budget exhaustion — it deletes the partial
  `CaseEvidence`/`AgentFinding` rows, keeps the `AgentRun` row, stores the raw error
  text in `AgentRun.failure_reason`, and calls `_run_fallback()`.
- **`_run_fallback` → `backend/agent/fallback.py::build()`** — a deterministic
  "engine-only" investigation: it calls the same 5 tool functions directly (no LLM),
  builds findings from the engine's triggered signals, and applies one fixed
  escalation rule (`ring ≥ 2 customers` and engine action in
  `{MONITOR, STEP_UP_AUTH, HOLD_FOR_REVIEW}` → dissent, recommend `BLOCK`).
  `run.mode="fallback"`, `run.model="engine-only"`, `run.status="FALLBACK"`.
- **Why the UI can show "engine-only":** either no `GEMINI_API_KEY` /
  `ANTHROPIC_API_KEY` is set (`settings.active_provider is None` → straight to
  fallback), or a Gemini call raised and the `except` path ran. The header chip and
  the investigation panel label it explicitly. On this machine a key **is** set, so
  live runs use `Gemini gemini-3.6-flash`.

---

## 8. WHAT IS THE RISK ENGINE (separate from Gemini)?

`backend/risk/` — pure Python, deterministic, no network, no model.

```
Transaction (+ this customer's prior txns and auth events)
      |
      v
backend/risk/signals.py  — 6 detectors, each -> Signal(normalized 0..1, weight)
      |    AMT_DEV .80 | VEL_1H .70 | AUTH_FAIL .85 | DEV_NEW .60 | GEO_DIST .50 | TIME_ODD .35
      v
backend/risk/fusion.py   — grouped noisy-OR: p = 1 - PROD(1 - group_val);  base_score = round(100p)
      |                     leave_one_out() -> each signal's contribution_pct
      v
backend/risk/rules.py    — 3 rules, each imposes a score FLOOR:
      |    R_IMPOSSIBLE_TRAVEL 85 (>900 km/h implied)   [HARD]
      |    R_MULTI_CUSTOMER_DEVICE 75 (device on >=2 other customers / 24h)  [HARD]
      |    R_AUTH_STORM 65 (>=3 failed OTP/PWD/CVV in 5 min)
      v
score = max(base_score, highest fired floor)   [clamped 0..100]
      v
backend/risk/policy.py
      band_for(score): <35 LOW | 35-61 MEDIUM | 62-81 HIGH | >=82 CRITICAL
      decide(band, rules, amount, p95, kyc):
        LOW      -> ALLOW
        MEDIUM   -> MONITOR
        HIGH     -> HOLD_FOR_REVIEW if (amount>p95 or any rule) else STEP_UP_AUTH
        CRITICAL -> BLOCK if any HARD rule else HOLD_FOR_REVIEW
      v
engine.py::assess() persists: risk_assessments (1 row) + risk_signals (6 rows)
                    + audit_log "risk_assessed"
```

`kyc_tier` is passed to `decide()` but not used in the current body.

---

## 9. WHAT HAPPENS AFTER YOU CLICK "BLOCK TRANSACTION"?

```
Click an action button  ->  chooseAction("BLOCK")  sets pendingAction   [index.html]
Click "Confirm decision & close case"  ->  submitDecision()
        |   decision = "OVERRIDE" if BLOCK != engine action
        |            else "REJECT" (BLOCK/HOLD) | "APPROVE" (others)
        v
POST /api/cases/{id}/decision  { decision, final_action:"BLOCK", override_reason }
        v
backend/api/decisions.py::decide()  ->  backend/services.py::apply_decision()
        |   if is_override and blank reason -> raise ValueError -> HTTP 422
        |   INSERT decisions row (actor "analyst@demo", engine_recommended_action,
        |          ai_recommended_action, final_action "BLOCK", override_reason)
        |   case.status = "BLOCKED"        (BLOCK and HOLD_FOR_REVIEW both -> BLOCKED;
        |                                   STEP_UP_AUTH / MONITOR / ALLOW -> APPROVED)
        |   case.closed_at = now_ist()
        |   INSERT audit_log "analyst_decision" {decision, final_action, engine_recommended,
        |          ai_recommended, override, override_reason, new_status}
        v
db.commit()
        v
frontend reloads GET /api/cases/{id} -> "Case resolved" panel; decision buttons hidden;
        dashboard reloads (open_cases, median_time_to_decision update)
        POST .../investigate now returns 409 for this case
```

**Is it a meaningful backend operation or just UI state?**
It is a real, persisted backend operation: three writes to SQLite — a new
`decisions` row, `cases.status` + `cases.closed_at`, and an append-only `audit_log`
row. It is **not** a payment action: **no code path sets `transactions.status`** (it
stays `"captured"`), there is no payment gateway, nothing is actually blocked or
refunded. "Block Transaction" records the analyst's decision and closes the case.
That is the honest scope: decision support + audit trail, not enforcement.

---

## 10. SIMPLE ANSWERS FOR THE DEMO

**"Where did this transaction come from?"**
> "It's synthetic data my app generated. When I click Run scenario, a Python
> function called `_account_takeover` in `backend/seed.py` builds a scripted
> account-takeover: a real-looking payment 35 minutes ago in the customer's home
> city, then three failed OTPs, then a big payment from a brand-new device in a
> far-away city, plus two other accounts using that same device. It's written to a
> SQLite database and the screen reads it back. No real customer data, no external
> feed."

**"How did Vigil get 97/100?"**
> "A deterministic formula, not AI. Six checks compare this payment to this
> customer's own history and each produces a 0–1 severity. Here: three failed OTPs,
> a new device, 1,500 km from home, and about 4x the normal amount. They're combined
> with a noisy-OR model — that gives roughly 95. Then two hard rules fire: impossible
> travel and a device shared across accounts. Impossible travel sets a floor of 85.
> The final score is the higher of the two, so about 95–97, which is CRITICAL, and
> because a hard rule fired the recommended action is Block. The 'Why this score?'
> panel on screen shows the exact per-signal points."

**"Is Vigil actually predicting fraud using AI?"**
> "No. The fraud score is deterministic rules plus statistics with fixed weights —
> there's no trained model anywhere. The only AI is Google's Gemini, and it runs
> only when I press 'Run AI investigation'. It reads the case and writes an
> evidence-backed explanation and a second opinion, and every number it states is
> re-checked against the database. It never produces the score and it can't take an
> action."

**"What happens when I click Simulate Attack / Run scenario?"**
> "The browser calls `POST /api/simulate/scenario`. The backend picks the scenario
> template, generates the transactions and auth events for a fixed demo customer
> (CUST-1002), runs them through the deterministic risk engine, saves everything to
> the database, opens a case because the score is over 60, and the UI jumps straight
> into that case. No AI is involved in that step."

---

## 11. VISUAL DATA-FLOW DIAGRAM (real names)

```
  [ USER ]  clicks "▶ Run scenario"  (scenario = "account_takeover")
      |
      v
  runScenario()                                   backend/static/index.html
      |   fetch POST /api/simulate/scenario
      v
  simulate()                                       backend/api/admin.py
      |
      v
  inject_scenario(db, "account_takeover", None)    backend/seed.py
      |   victim = CUST-1002        (_DEFAULT_VICTIM)
      |   builder = _account_takeover   (_BUILDERS)
      v
  _account_takeover(db, victim, when=now, rng=random.Random())   backend/seed.py
      |   far city  = max(CITIES, key=haversine from Pune) = "Kolkata"
      |   inserts: 1 legit-prior txn (Pune, t-35m) + 3 OTP_FAIL auth_events
      |            + 2 ring txns (same device, Kolkata) + 1 FOCUS txn
      |   FOCUS txn -> table `transactions`
      |      amount = median * uniform(3.2,4.2) ; merchant "Gift Card Store"/5947 ; device "DEV-9xxx"
      v
  assess(db, focus_txn)                            backend/risk/engine.py
      |
      +-- signals.run_all()          backend/risk/signals.py   -> 6 Signals
      |       AUTH_FAIL .750  DEV_NEW 1.0  GEO_DIST 1.0  AMT_DEV .302  VEL_1H 0  TIME_ODD 0
      |
      +-- fusion.fuse_score()        backend/risk/fusion.py    -> base_score = 95  (grouped noisy-OR)
      +-- fusion.leave_one_out()     backend/risk/fusion.py    -> contribution_pct per signal
      |
      +-- rules.evaluate()           backend/risk/rules.py
      |       R_IMPOSSIBLE_TRAVEL floor 85  (HARD)   <- from Pune->Kolkata in 35 min
      |       R_MULTI_CUSTOMER_DEVICE floor 75 (HARD) <- ring txns on DEV-9xxx
      |
      +-- score = max(95, 85) = 95
      +-- policy.band_for(95)  = "CRITICAL"          backend/risk/policy.py
      +-- policy.decide(...)   = "BLOCK"             (CRITICAL + HARD rule)
      |
      +-- writes: risk_assessments (1 row) + risk_signals (6 rows) + audit_log "risk_assessed"
      v
  open_case(db, txn, assessment)                   backend/services.py
      |   score 95 >= 60  ->  INSERT cases row "CASE-2026-0004" + audit_log "case_opened"
      v
  db.commit()   ->  returns {score:95, band:"CRITICAL", recommended_action:"BLOCK", case_id:"CASE-2026-0004"}
      |
      v
  openCase("CASE-2026-0004")  ->  GET /api/cases/CASE-2026-0004
      |
      v
  case_detail()                                    backend/api/cases.py
      reads: cases + transactions + customers + risk_assessments + risk_signals
             + services.related_events() + services.build_timeline()
             + case_evidence + agent_runs + decisions + audit_log
      |
      v
  CASE SCREEN  (4 numbered sections)               backend/static/index.html
      |
      |   [ OPTIONAL ]  analyst clicks "Run AI investigation"
      |        POST /api/cases/{id}/investigate  ->  runner.investigate()   backend/agent/runner.py
      |            provider = Gemini  ->  _run_gemini()  (5 read-only tools, structured Investigation)
      |            on error / no key ->  _run_fallback() -> fallback.build()  (deterministic, badged)
      |            validator.validate()  re-checks every number  ->  agent_runs + agent_findings rows
      v
  ANALYST DECISION   analyst picks an action, adds a reason, "Confirm"
      |   POST /api/cases/{id}/decision
      v
  apply_decision()                                 backend/services.py
      |   INSERT decisions row
      |   cases.status -> "BLOCKED" | "APPROVED" ;  cases.closed_at = now
      |   INSERT audit_log "analyst_decision"
      v
  FINAL STATUS   cases.status in SQLite  (BLOCKED / APPROVED / REVIEW_REQUIRED)
                 transactions.status is UNCHANGED ("captured") — no gateway
```

---

## 12. EXPLICIT STATEMENTS (verified, not guessed)

**THE TRANSACTION COMES FROM** `backend/seed.py::_account_takeover()` (a scripted
Python template with ~9 randomised numbers), called by `inject_scenario()` from the
route `POST /api/simulate/scenario` (`backend/api/admin.py`), and stored as a row in
the **SQLite `transactions` table**. It is synthetic; it is not mock JSON, not an
external API, not AI-generated. The customer baselines it is compared against come
from `backend/seed.py::seed()` (`random.Random(42)`, 90 days of synthetic history
per customer).

**THE RISK SCORE COMES FROM** `backend/risk/engine.py::assess()` — a deterministic
pipeline: `signals.py` (6 weighted detectors) → `fusion.py` (grouped noisy-OR →
`base_score`, plus leave-one-out attribution) → `rules.py` (3 rules that impose score
floors) → `score = max(base_score, floor)` → `policy.py` (band + recommended action).
For the audited transaction: `base_score 95`, rules `R_IMPOSSIBLE_TRAVEL` (85) +
`R_MULTI_CUSTOMER_DEVICE` (75) fired, `score = max(95, 85) = 95 → CRITICAL → BLOCK`.
Stored in **`risk_assessments`** (+ 6 rows in **`risk_signals`**). **No AI is
involved in the score.**

**THE AI DOES** — only when the analyst clicks "Run AI investigation" — read the case
with 5 read-only tools and produce a written investigation: a summary, findings whose
numbers the backend re-verifies against the database (`backend/agent/validator.py`),
a qualitative risk view (a word, not a 0–100 number), and an advisory recommendation
/ second opinion. It is `backend/agent/runner.py::_run_gemini()` calling
`google.genai` with model `gemini-3.6-flash`. It **does not** compute the score,
**does not** decide the action, and has **no tool that writes anything**. On any
failure or missing key it falls back to `backend/agent/fallback.py` (deterministic,
labelled "engine-only" in the UI). Output stored in **`agent_runs`** +
**`agent_findings`** + **`case_evidence`**.

**THE ANALYST DOES** choose the final action (Allow / Monitor / Step-up / Hold /
Block) in case step 4. A choice that differs from the engine's recommendation
requires a typed reason (enforced: `HTTP 422` otherwise, in
`backend/services.py::apply_decision`). The analyst is the only actor who can resolve
a case.

**THE FINAL DECISION IS STORED IN** SQLite: a new row in the **`decisions`** table
(actor, `decision`, `engine_recommended_action`, `ai_recommended_action`,
`final_action`, `override_reason`), an update to **`cases.status`** (→ `BLOCKED` for
BLOCK/HOLD, `APPROVED` for the rest) and **`cases.closed_at`**, and an append-only
row in the **`audit_log`** table (`action = "analyst_decision"`). It is **NOT**
stored on the transaction — `transactions.status` is never modified by a decision,
and no payment system is contacted.
