from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from dateutil.rrule import DAILY, MO, WEEKLY, rrule

from .models import CheckSchedule


def sale_windows(year: int) -> dict[str, tuple[date, date]]:
    november = date(year, 11, 1)
    black_friday = november + timedelta(days=(3 - november.weekday()) % 7 + 22)
    return {
        "black_friday": (black_friday - timedelta(days=7), black_friday + timedelta(days=3)),
        "christmas": (date(year, 12, 15), date(year, 12, 26)),
        "new_year": (date(year, 12, 27), date(year + 1, 1, 7)),
        "winter_sales": (date(year, 1, 8), date(year, 1, 31)),
    }


def next_check(watch: dict, moment: datetime, *, include_today: bool = False) -> datetime | None:
    if not watch.get("schedule"):
        hours = watch.get("interval_hours")
        return (moment if include_today else moment + timedelta(hours=hours)) if hours else None
    schedule = CheckSchedule.model_validate(watch["schedule"])
    zone = ZoneInfo(schedule.timezone)
    today = moment.astimezone(zone).date()
    reference = datetime.combine(today, time.min, zone)
    recurrence = rrule(
        DAILY if schedule.repeat == "daily" else WEEKLY,
        interval=schedule.interval,
        dtstart=datetime.combine(schedule.start_date, time.min, zone),
        byweekday=schedule.weekdays if schedule.repeat == "weekly" else None,
        wkst=MO,
    )
    upcoming = recurrence.after(reference, inc=include_today)
    candidates = [upcoming] if upcoming else []
    first_day = max(schedule.start_date, today + timedelta(days=not include_today))
    for year in range(first_day.year - 1, first_day.year + 2):
        for period in schedule.sale_periods:
            start, end = sale_windows(year)[period]
            if end >= first_day:
                candidates.append(datetime.combine(max(start, first_day), time.min, zone))
    return min(candidates).astimezone(UTC) if candidates else None