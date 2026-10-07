"""Rider-bound structured tools with durable traces, including invalid invocations."""

import asyncio
from dataclasses import dataclass
from datetime import date as Date, datetime
from typing import Callable

from langchain_core.tools import StructuredTool
from langsmith.run_helpers import tracing_context
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, StrictInt
from sqlalchemy import select

from app.db.models import Trip
from app.domain.dispute import DisputeRequest
from app.domain.policy import is_dispute_within_window, normalize_rider_id, to_ist_date
from app.integrations.payswift import PaySwiftClient
from app.services.dispute_service import DisputeService
from app.services.trace import TraceRecorder


class ToolAccessError(ValueError):
    """The requested resource is not available to the context's rider."""


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OptionalDateInput(EmptyInput):
    date: Date | None = None


class DateInput(EmptyInput):
    date: Date


class TripInput(EmptyInput):
    trip_id: str = Field(min_length=1, max_length=32)


class DisputeInput(EmptyInput):
    dispute_id: str = Field(min_length=1, max_length=128)


class AmountInput(EmptyInput):
    amount: StrictInt


@dataclass(frozen=True)
class ToolContext:
    """Construct only in trusted application code, once per authenticated request.

    Never build this object from model output/config. Each invocation gets its own
    session, so synchronous and asynchronous tools do not share ORM sessions.
    """

    authenticated_rider_id: str
    session_factory: Callable
    now: datetime
    payswift_factory: Callable = PaySwiftClient

    def __post_init__(self):
        if not isinstance(self.authenticated_rider_id, str) or not self.authenticated_rider_id.strip():
            raise ValueError("A trusted rider ID is required")
        rider_id = normalize_rider_id(self.authenticated_rider_id)
        if len(rider_id) > 32 or not isinstance(self.now, datetime) or self.now.tzinfo is None or self.now.utcoffset() is None:
            raise ValueError("Trusted rider ID and timezone-aware clock are required")
        object.__setattr__(self, "authenticated_rider_id", rider_id)

    @property
    def rider_id(self):
        return self.authenticated_rider_id

    @property
    def today(self):
        return to_ist_date(self.now)

    def own_trip(self, session, trip_id):
        trip = session.scalar(select(Trip).where(Trip.trip_id == trip_id, Trip.rider_id == self.rider_id))
        if trip is None:
            raise ToolAccessError("Trip not found for this rider")
        return trip

    def recent_trips(self, session, target=None):
        if target is not None and not is_dispute_within_window(target, self.today):
            raise ValueError("Date outside the dispute window")
        trips = session.scalars(select(Trip).where(Trip.rider_id == self.rider_id).order_by(Trip.started_at, Trip.trip_id)).all()
        return [t for t in trips if (to_ist_date(t.started_at) == target if target else
                                    is_dispute_within_window(to_ist_date(t.started_at), self.today))]

    def reconcile(self, kind, *, trip_id=None, date=None):
        with self.session_factory() as session:
            if trip_id:
                self.own_trip(session, trip_id)
            return DisputeService(session).evaluate(self.rider_id, DisputeRequest(
                dispute_type=kind, trip_id=trip_id, dispute_date=date,
            ), reference_date=self.now).model_dump(mode="json")


class TracedTool(StructuredTool):
    _recorder: TraceRecorder = PrivateAttr()

    @property
    def tool_call_schema(self):
        # No injected/model-controlled context fields exist. Preserve the full
        # strict schema instead of LangChain's config-losing subset projection.
        return self.args_schema

    def run(self, tool_input, *args, **kwargs):
        # All auditing stays in our database, even if a host enables LangSmith.
        with tracing_context(enabled=False):
            inputs = {"arguments": tool_input}
            if kwargs.get("tool_call_id") is not None:
                inputs["tool_call_id"] = kwargs["tool_call_id"]
            identifier = self._recorder.start(self.name, inputs)
            try:
                if not isinstance(tool_input, dict):
                    raise ValueError("Tool inputs must be structured JSON objects")
                # LangChain skips parsing on zero-field tools; enforce strict
                # input validation for those tools as well as ordinary tools.
                self.args_schema.model_validate(tool_input)
                output = super().run(tool_input, *args, **kwargs)
            except Exception as exc:
                self._recorder.finish(identifier, error=exc)
                raise
            self._recorder.finish(identifier, output=output)
            return output

    async def arun(self, tool_input, *args, **kwargs):
        return await asyncio.to_thread(self.run, tool_input, *args, **kwargs)


def read_tool(context, name, description, schema, function):
    tool = TracedTool.from_function(function, name=name, description=description,
                                   args_schema=schema, infer_schema=False,
                                   metadata={"access": "read", "rider_bound": True})
    tool._recorder = TraceRecorder(context.session_factory, context.rider_id)
    return tool


def trip_json(trip):
    return {"trip_id": trip.trip_id, "rider_id": trip.rider_id,
            "started_at": trip.started_at.isoformat(), "ist_date": to_ist_date(trip.started_at).isoformat(),
            "status": trip.status, "distance_km": str(trip.distance_km),
            "surge_multiplier": str(trip.surge_multiplier)}
