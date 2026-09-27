"""The person's time zone, and the moment the writer is told a message was said.

A message that says "tomorrow morning" or "Monday" names a moment only to a
reader who knows which day it was said and in which zone. The record keeps
the moment in UTC and holds no zone for the person, so the zone is a setting.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from _writer import FakeStore, reading, replying
from pydantic import ValidationError

from agentic_memory.settings import Settings
from agentic_memory.synthesize import said_line, write


@pytest.mark.parametrize(
    "zone, expected",
    [
        ("UTC", "2026-09-26 01:44 UTC, a Saturday"),
        ("America/New_York", "2026-09-25 21:44 EDT (America/New_York), a Friday"),
        ("Asia/Tokyo", "2026-09-26 10:44 JST (Asia/Tokyo), a Saturday"),
    ],
)
def test_the_moment_is_given_in_the_persons_zone(zone, expected):
    # Late on Friday evening in New York is already Saturday in UTC, and
    # "tomorrow morning" said then is Saturday morning.
    said = datetime(2026, 9, 26, 1, 44, tzinfo=UTC)
    assert said_line(said, ZoneInfo(zone)) == expected


def test_a_moment_in_winter_is_given_in_the_zones_winter_time():
    said = datetime(2026, 12, 26, 1, 44, tzinfo=UTC)
    assert said_line(said, ZoneInfo("America/New_York")).startswith("2026-12-25 20:44 EST")


def test_the_persons_zone_is_utc_unless_it_is_set(monkeypatch):
    monkeypatch.delenv("AGENTIC_MEMORY_TIME_ZONE", raising=False)
    assert Settings().time_zone == ZoneInfo("UTC")


def test_the_persons_zone_is_read_by_its_name(monkeypatch):
    monkeypatch.setenv("AGENTIC_MEMORY_TIME_ZONE", "America/New_York")
    assert Settings().time_zone == ZoneInfo("America/New_York")


def test_a_zone_that_does_not_exist_is_refused():
    with pytest.raises(ValidationError):
        Settings(time_zone="Mars/Olympus_Mons")


@pytest.mark.parametrize(
    "zone, expected",
    [
        ("UTC", "The message was said at 2026-09-20 12:00 UTC, a Sunday."),
        (
            "America/New_York",
            "The message was said at 2026-09-20 08:00 EDT (America/New_York), a Sunday.",
        ),
    ],
)
async def test_the_ask_names_the_moment_in_the_persons_zone(zone, expected):
    model = replying(("prospective", "The widget release is held until Monday."))
    await write(
        "s1",
        "e1",
        settings=Settings(time_zone=zone),
        pool=FakeStore(reading(prospective=0.95)),
        client=model,
    )
    assert expected in model.asked


async def test_the_ask_says_when_the_moment_is_unknown():
    model = replying(("prospective", "The widget release is held until Monday."))
    await write(
        "s1",
        "e1",
        settings=Settings(),
        pool=FakeStore(reading(prospective=0.95, said_at=None)),
        client=model,
    )
    assert "The message was said at an unknown moment." in model.asked
