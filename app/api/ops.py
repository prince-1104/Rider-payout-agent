"""Ops queue: approvals and escalations waiting for a human."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.database import get_session
from app.services.ops_service import OpsService

from app.schemas.ops import PendingItem

router = APIRouter(prefix="/ops", tags=["ops"])


@router.get("/pending", response_model=list[PendingItem])
def list_pending(session: Annotated[Session, Depends(get_session)]) -> list[PendingItem]:
    return OpsService(session).list_pending()
