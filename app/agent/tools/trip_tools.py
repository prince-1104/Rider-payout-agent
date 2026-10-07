"""Read only trips owned by the rider bound to this tool collection."""

from app.agent.tools.base import OptionalDateInput, TripInput, read_tool, trip_json


def build_tools(context):
    def find_recent_trips(date=None):
        with context.session_factory() as session:
            return [trip_json(t) for t in context.recent_trips(session, date)]

    def get_trip(trip_id):
        with context.session_factory() as session:
            return trip_json(context.own_trip(session, trip_id))
    return [read_tool(context, "find_recent_trips", "List owned trips in the last 7 days, optionally on an IST date.", OptionalDateInput, find_recent_trips),
            read_tool(context, "get_trip", "Read one owned trip's authoritative dispatch record.", TripInput, get_trip)]
