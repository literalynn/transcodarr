"""Parsing de la réponse Jellyfin /Items (DateCreated par chemin)."""

from __future__ import annotations

import datetime

from transcoder.core.jellyfin import parse_items_dates


def test_parse_items_dates_nominal() -> None:
    payload = {
        "Items": [
            {
                "Path": "/srv/storage/Films/A.mkv",
                "DateCreated": "2024-03-01T12:34:56.7891234Z",
            },
            {"Path": "/srv/storage/Films/B.mkv", "DateCreated": "2024-04-02T00:00:00Z"},
            {"Path": "/srv/storage/Films/C.mkv"},  # sans date : ignoré
            {"DateCreated": "2024-04-02T00:00:00Z"},  # sans chemin : ignoré
            "junk",
        ]
    }
    dates = parse_items_dates(payload)
    assert len(dates) == 2
    a = dates["/srv/storage/Films/A.mkv"]
    assert a.year == 2024 and a.month == 3
    assert a.tzinfo is datetime.timezone.utc
    assert a.microsecond == 789123  # nanosecondes tronquées


def test_parse_items_dates_garbage() -> None:
    assert parse_items_dates({}) == {}
    assert parse_items_dates({"Items": "no"}) == {}
    assert (
        parse_items_dates({"Items": [{"Path": "/x", "DateCreated": "pas-une-date"}]}) == {}
    )
