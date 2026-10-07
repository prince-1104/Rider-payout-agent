"""Ops queue: approvals and escalations waiting for a human."""

from fastapi import APIRouter

from app.schemas.ops import PendingItem

router = APIRouter(prefix="/ops", tags=["ops"])


@router.get("/pending", response_model=list[PendingItem])
def list_pending() -> list[PendingItem]:
    # Phase 1: no approval/escalation storage exists yet.
    return []
