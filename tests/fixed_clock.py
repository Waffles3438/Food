"""Keep dated event fixtures independent of the machine's current date."""

from datetime import datetime
from zoneinfo import ZoneInfo


class EventFixtureDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        fixed = datetime(2026, 9, 24, 12, tzinfo=ZoneInfo("America/Toronto"))
        return fixed.astimezone(tz) if tz is not None else fixed.replace(tzinfo=None)
