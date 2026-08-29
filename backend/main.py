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
        model=settings.vigil_model,
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
