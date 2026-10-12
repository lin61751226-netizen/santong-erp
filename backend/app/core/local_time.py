"""Local business dates over the existing UTC timestamp storage."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.core.config import settings


def local_now() -> datetime:
    return datetime.now(ZoneInfo(settings.timezone))


def local_today() -> date:
    return local_now().date()


def as_local(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo(settings.timezone))


def utc_day_bounds(day: date) -> tuple[datetime, datetime]:
    zone = ZoneInfo(settings.timezone)
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return (
        start.astimezone(timezone.utc).replace(tzinfo=None),
        end.astimezone(timezone.utc).replace(tzinfo=None),
    )
