"""Calendrier métier (XG-15) : les jours comptables sont ceux de Dakar.

Dakar est en UTC+0 sans heure d'été ; on passe néanmoins par ``zoneinfo`` plutôt que de
supposer « jour Dakar = jour UTC » : la règle reste juste si la zone changeait, et elle
est explicite dans le code. Les horodatages, eux, sont toujours stockés en UTC.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo

DAKAR: Final = ZoneInfo("Africa/Dakar")


def business_day_bounds(day: date) -> tuple[datetime, datetime]:
    """Bornes UTC ``[début, fin[`` du jour ``day`` à Dakar."""
    start = datetime.combine(day, time.min, tzinfo=DAKAR)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def business_date(moment: datetime) -> date:
    """Jour comptable (Dakar) d'un horodatage UTC."""
    return moment.astimezone(DAKAR).date()


def today_in_dakar(now: datetime | None = None) -> date:
    return business_date(now or datetime.now(UTC))
