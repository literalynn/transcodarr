"""Périodes des statistiques : alignement, granularité et créneaux (purs)."""

from __future__ import annotations

import datetime

from transcoder.api.routers.stats import bucket_starts, period_start, timeline_unit, truncate

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 9, 15, 14, 37, 12, tzinfo=UTC)  # un mardi


def test_truncate_matches_date_trunc() -> None:
    assert truncate(NOW, "hour") == datetime.datetime(2026, 9, 15, 14, tzinfo=UTC)
    assert truncate(NOW, "day") == datetime.datetime(2026, 9, 15, tzinfo=UTC)
    # Semaine ISO (date_trunc('week')) : lundi
    assert truncate(NOW, "week") == datetime.datetime(2026, 9, 14, tzinfo=UTC)


def test_truncate_converts_to_utc() -> None:
    paris = datetime.timezone(datetime.timedelta(hours=2))
    local_midnight = datetime.datetime(2026, 9, 15, 0, 30, tzinfo=paris)
    assert truncate(local_midnight, "day") == datetime.datetime(2026, 9, 14, tzinfo=UTC)


def test_last_24h_is_24_hourly_buckets_including_current_hour() -> None:
    start = period_start(1, NOW)
    assert start == datetime.datetime(2026, 9, 14, 15, tzinfo=UTC)
    buckets = bucket_starts(start, NOW, timeline_unit(1, start, NOW))
    assert len(buckets) == 24
    assert buckets[-1] == truncate(NOW, "hour")


def test_days_periods_are_calendar_days_including_today() -> None:
    for days in (7, 30):
        start = period_start(days, NOW)
        assert start is not None
        unit = timeline_unit(days, start, NOW)
        assert unit == "day"
        buckets = bucket_starts(start, NOW, unit)
        assert len(buckets) == days
        assert buckets[-1] == truncate(NOW, "day")


def test_all_time_granularity_follows_history_span() -> None:
    assert period_start(None, NOW) is None
    assert timeline_unit(None, None, NOW) == "hour"
    assert timeline_unit(None, NOW - datetime.timedelta(hours=30), NOW) == "hour"
    assert timeline_unit(None, NOW - datetime.timedelta(days=60), NOW) == "day"
    assert timeline_unit(None, NOW - datetime.timedelta(days=400), NOW) == "week"


def test_weekly_buckets_cover_first_job() -> None:
    first = datetime.datetime(2025, 7, 16, 9, tzinfo=UTC)  # un mercredi
    buckets = bucket_starts(first, NOW, "week")
    assert buckets[0] == datetime.datetime(2025, 7, 14, tzinfo=UTC)
    assert buckets[-1] == truncate(NOW, "week")
    assert all(b.weekday() == 0 for b in buckets)
