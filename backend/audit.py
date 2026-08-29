"""Append-only audit logging. This module is the only writer of audit_log, and it
only ever INSERTs."""
from __future__ import annotations

from sqlalchemy.orm import Session

from .models import AuditLog


def record(db: Session, *, entity_type: str, entity_id: str, action: str,
           actor: str = "system", detail: dict | None = None) -> AuditLog:
    entry = AuditLog(
        entity_type=entity_type,
        entity_id=str(entity_id),
        actor=actor,
        action=action,
        detail=detail or {},
    )
    db.add(entry)
    db.flush()
    return entry
