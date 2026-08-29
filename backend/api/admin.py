"""Demo control: reset to seeded state, inject a scenario."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..seed import SCENARIOS, inject_scenario, seed

router = APIRouter(prefix="/api", tags=["admin"])


class SimulateBody(BaseModel):
    scenario: str = "account_takeover"
    customer_id: str | None = None


@router.post("/admin/reset")
def reset(db: Session = Depends(get_db)) -> dict:
    return seed(db)


@router.post("/simulate/scenario")
def simulate(body: SimulateBody, db: Session = Depends(get_db)) -> dict:
    if body.scenario not in SCENARIOS:
        raise HTTPException(422, f"unknown scenario; choose from {list(SCENARIOS)}")
    try:
        return inject_scenario(db, body.scenario, body.customer_id)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
