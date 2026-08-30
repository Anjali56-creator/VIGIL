"""Vigil FastAPI application entry point.

Run:  uvicorn backend.main:app --reload --port 8000
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from .config import settings
from .db import engine, init_db
from .schemas import HealthOut

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    # On a fresh deployment the tables exist but are empty, which makes the
    # dashboard look broken until someone clicks "Reset demo". Seed once if the
    # database is empty; an already-seeded DB is left untouched. "Reset demo"
    # still re-seeds deterministically at any time.
    from sqlalchemy import func, select

    from .db import SessionLocal
    from .models import Customer
    from .seed import seed

    with SessionLocal() as db:
        if not db.scalar(select(func.count()).select_from(Customer)):
            seed(db)
    yield


app = FastAPI(title="Vigil - AI Risk Investigator", version="1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def _unhandled(_request: Request, exc: Exception):  # pragma: no cover
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error", "message": str(exc)}},
    )


@app.get("/api/health", response_model=HealthOut)
def health() -> HealthOut:
    db_ok = True
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    return HealthOut(
        status="ok" if db_ok else "degraded",
        db=db_ok,
        llm_configured=settings.llm_configured,
        # Provider-qualified label - "Gemini <model>" / "Claude <model>" only when
        # that provider is actually active, else "engine-only fallback". Never
        # implies a live model when none is configured.
        model=settings.active_model_label,
    )


# ---- routers (registered as phases land) ----
from .api import admin, cases, dashboard, decisions, investigations, transactions  # noqa: E402

for _r in (transactions.router, cases.router, investigations.router, decisions.router, dashboard.router, admin.router):
    app.include_router(_r)


# ---- static single-page frontend ----
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
