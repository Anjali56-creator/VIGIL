# Vigil — AI Risk Investigator

Razorpay AI Buildathon · **Track 2: AI Risk Manager**

Vigil turns a suspicious payment into an explainable, evidence-backed investigation
and an auditable decision. It is built on three layers that never bleed into each other:

| Layer | Owns | Implementation |
|---|---|---|
| **Deterministic + statistical** | the 0–100 risk **score** | `backend/risk/` — 6 signal detectors, grouped noisy-OR fusion, exact leave-one-out attribution, deterministic rule floors, action policy matrix |
| **AI investigation agent** | the **narrative + relationships** | `backend/agent/` — `claude-opus-5`, 5 read-only tools, evidence ledger, structured Pydantic output, 4-gate grounding validation with numeric re-verification |
| **Human analyst** | the **decision** | approve / step-up / hold / block / override, with a mandatory reason on any override, and an append-only audit trail |

The LLM never computes the score and never executes an action. Every quantitative
claim it makes is re-computed against the database before it is persisted — findings
that fail are shown with a warning, never dropped.

## Run it

```bash
python -m venv .venv
.venv/Scripts/activate           # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
cp .env.example .env             # optional: add ANTHROPIC_API_KEY for the live agent

python -m backend.seed           # build the synthetic demo database (deterministic)
uvicorn backend.main:app --port 8000
```

Open <http://localhost:8000>.

Without an `ANTHROPIC_API_KEY` the agent runs a deterministic **engine-only**
investigation (clearly badged) so the full workflow still demos.

## Demo flow

1. Dashboard — a normal night: ~97% of transactions score LOW.
2. Click **⚡ Simulate attack** — injects a scripted account-takeover sequence
   (home-city purchase → new device → OTP failures → far-city high-value transaction,
   plus two ring transactions on the same device).
3. The engine opens a CRITICAL case automatically. Open it.
4. **Why this score** — six signals, each with the observed value, the customer's
   baseline, and its exact contribution %. Rule floor `R_IMPOSSIBLE_TRAVEL` shown.
5. Click **Investigate** — the agent queries the database tool-by-tool, produces
   findings that cite evidence ids, and the backend verifies every number
   (`✓ N/N claims verified`).
6. Engine vs agent recommendation, with a **⚡ dissents** marker when they differ.
7. Analyst overrides with a reason → case status flips → audit trail updated.
8. Dashboard shows median analyst time-to-decision.

**Reset demo** returns everything to the seeded state.

## Data

All data is **synthetic** and generated locally by `backend/seed.py`. It does not
represent real Razorpay production data. No real customer PII is used. `is_attack` /
`scenario_label` are ground-truth tags used only for detection metrics — they are
never shown to the risk engine or the agent.

## Layout

```
backend/
  main.py            FastAPI app (serves the API and the single-page UI)
  config.py          settings; secrets from env only
  models.py          SQLAlchemy ORM (SQLite + WAL)
  risk/              signals, fusion, rules, policy, engine
  agent/             tools, schema, prompts, runner, validator, fallback
  api/               transactions, cases, investigations, decisions, dashboard, admin
  seed.py            deterministic synthetic population + scripted attacks
  static/index.html  the whole frontend (Tailwind + Alpine via CDN, no build step)
tests/test_smoke.py  end-to-end: ingest → case → investigate → grounding → decision → audit
```
