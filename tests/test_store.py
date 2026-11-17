from datetime import datetime, timezone

from moonbot import logic as L
from moonbot.store import Store

UTC = timezone.utc


def ev(t="MoonminingLaserFired", minute=0):
    return L.MoonEvent(t, datetime(2026, 11, 17, 9, minute, tzinfo=UTC), 42, moon_id=7,
                       ready_time=datetime(2026, 11, 17, 6, tzinfo=UTC), volumes=[1.5, 2.5])


def test_event_roundtrip_and_dedup():
    s = Store(":memory:")
    assert s.add_event(ev(), announced=False)
    assert not s.add_event(ev(), announced=False)  # same key from another char
    assert s.add_event(ev(minute=5), announced=True)
    back = s.events_for(42)
    assert len(back) == 2 and back[0].volumes == [1.5, 2.5] and back[0].ready_time.tzinfo


def test_settings_messages_names():
    s = Store(":memory:")
    s.set("a", "1"); assert s.get("a") == "1"
    s.set("a", None); assert s.get("a", "x") == "x"
    s.set_status_message(42, 1, 2); assert s.status_messages() == {42: (1, 2)}
    s.drop_status_message(42); assert s.status_messages() == {}
    s.save_names({7: "Moon"}); assert s.names([7, 8]) == {7: "Moon"}
    assert s.moon_ores(7) is None
    s.set_moon_ores(7, [45501, 45491]); assert s.moon_ores(7) == [45491, 45501]


def test_parse_structure_filters_athanor():
    raw = {"structure_id": 42, "fuel_expires": "2026-12-10 12:00:00",
           "info": {"name": "XX-TEST - Test Refinery"}, "type": {"typeID": 35835},
           "solar_system": {"name": "XX-TEST"},
           "services": [{"name": "Moon Drilling", "state": "offline"}]}
    s = L.parse_structure(raw)
    assert s.name.startswith("XX-TEST") and s.fuel_expires.day == 10 and not s.drill_online
    assert L.parse_structure({**raw, "type": {"typeID": 81826}}) is None  # Metenox
    assert L.parse_structure({**raw, "fuel_expires": None}).fuel_expires is None
