"""XG-15 : jours comptables à l'heure de Dakar, horodatages en UTC."""

from datetime import UTC, date, datetime

from xaalis.core.clock import DAKAR, business_date, business_day_bounds, today_in_dakar


def test_xg15_business_day_bounds_are_dakar_midnight_in_utc() -> None:
    start, end = business_day_bounds(date(2026, 11, 26))
    assert start == datetime(2026, 11, 26, tzinfo=UTC)
    assert end == datetime(2026, 11, 27, tzinfo=UTC)
    assert start.tzinfo is not None


def test_xg15_dakar_has_no_offset_and_no_daylight_saving() -> None:
    for month in (1, 7):  # hiver et été
        moment = datetime(2026, month, 15, 12, tzinfo=UTC).astimezone(DAKAR)
        assert moment.utcoffset() is not None
        assert moment.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


def test_xg15_last_second_of_the_day_belongs_to_that_day() -> None:
    assert business_date(datetime(2026, 11, 26, 23, 59, 59, tzinfo=UTC)) == date(2026, 11, 26)
    assert business_date(datetime(2026, 11, 27, 0, 0, 0, tzinfo=UTC)) == date(2026, 11, 27)


def test_today_in_dakar_uses_the_given_instant() -> None:
    assert today_in_dakar(datetime(2026, 11, 28, 21, 59, tzinfo=UTC)) == date(2026, 11, 28)
