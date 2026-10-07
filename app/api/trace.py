"""Per-rider audit trail."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.database import get_session
from app.schemas.trace import TraceStep
from app.services.trace import get_trace

router = APIRouter(tags=["trace"])


@router.get("/trace/{rider_id}", response_model=list[TraceStep])
def read_trace(
    rider_id: str,
    session: Annotated[Session, Depends(get_session)],
) -> list[TraceStep]:
    return get_trace(session, rider_id)
