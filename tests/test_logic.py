from datetime import datetime, timedelta, timezone

from moonbot import logic as L
from moonbot.messages import build_pop

UTC = timezone.utc


def ft(dt: datetime) -> int:
    return int((dt.timestamp() + 11644473600) * 10_000_000)


START = datetime(2026, 10, 20, 6, 0, tzinfo=UTC)
READY = datetime(2026, 11, 17, 6, 0, 59, tzinfo=UTC)
AUTO = READY + timedelta(hours=3)
SID = 1035000000001

STARTED_YAML = f"""autoTime: {ft(AUTO)}
moonID: 40349232
moonLink: <a href="showinfo:14//40349232">XX-TEST VII - Moon 3</a>
oreVolumeByType:
  45490: 2078121.0
  45491: 773702.5
  46312: 1790476.0
  45492: 2301335.0
readyTime: {ft(READY)}
solarSystemID: 30005498
solarSystemLink: <a href="showinfo:5//30005498">XX-TEST</a>
startedBy: 90000001
startedByLink: <a href="showinfo:1383//90000001">Test Pilot</a>
structureID: {SID}
structureLink: <a href="showinfo:35835//{SID}">XX-TEST - Test Refinery</a>
structureName: XX-TEST - Test Refinery
structureTypeID: 35835
"""


def notif(ntype, ts, body):
    return {"notification_id": 1, "type": ntype, "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"), "object": body}


def started():
    return L.parse_notification(notif(L.EV_STARTED, START, STARTED_YAML))


def test_filetime_roundtrip():
    assert L.filetime_to_dt(ft(READY)) == READY


def test_parse_started():
    ev = started()
    assert ev.structure_id == SID
    assert ev.moon_id == 40349232
    assert ev.ready_time == READY and ev.auto_time == AUTO
    assert ev.ore_types == [45490, 45491, 45492, 46312]
    assert ev.volumes[2] == 2301335.0
    assert ev.actor_id == 90000001


def test_parse_accepts_dict_body_and_ignores_other_types():
    ev = L.parse_notification(notif(L.EV_CANCELLED, START, {"structureID": SID, "moonID": 1}))
    assert ev.type == L.EV_CANCELLED
    assert L.parse_notification(notif("StructureUnderAttack", START, "structureID: 1")) is None


def test_parse_iso_timestamp_with_z():
    n = notif(L.EV_STARTED, START, STARTED_YAML)
    n["timestamp"] = "2026-10-20T06:00:00Z"
    assert L.parse_notification(n).timestamp == START


def test_drilling_progress():
    now = START + (READY - START) / 2
    st = L.compute_state([started()], now)
    assert st.phase == L.PHASE_DRILLING
    assert abs(st.progress(now) - 0.5) < 0.01
    assert not st.ore_is_last


def test_ready_by_clock_then_fractured_by_clock_then_idle():
    assert L.compute_state([started()], READY + timedelta(minutes=1)).phase == L.PHASE_READY
    st = L.compute_state([started()], AUTO + timedelta(minutes=1))
    assert st.phase == L.PHASE_FRACTURED and st.fractured == AUTO
    st = L.compute_state([started()], AUTO + timedelta(hours=49))
    assert st.phase == L.PHASE_IDLE and st.ore_is_last


def test_laser_fired_and_cancel():
    fired = L.MoonEvent(L.EV_LASER_FIRED, READY + timedelta(hours=1), SID, volumes=[5.0])
    st = L.compute_state([started(), fired], READY + timedelta(hours=2))
    assert st.phase == L.PHASE_FRACTURED and st.fractured == fired.timestamp
    assert st.volumes == [5.0]
    cancel = L.MoonEvent(L.EV_CANCELLED, START + timedelta(days=1), SID)
    st = L.compute_state([started(), cancel], START + timedelta(days=2))
    assert st.phase == L.PHASE_IDLE and st.ore_is_last


def test_unknown_without_events():
    assert L.compute_state([], START).phase == L.PHASE_UNKNOWN


def test_event_key_dedups_across_characters():
    a, b = started(), started()
    assert a.key == b.key


def test_progress_bar():
    assert L.progress_bar(0.5, 10) == "`━━━━━─────|` 50%"
    assert L.progress_bar(1.0, 4) == "`━━━━|` 100%"


def test_fuel_line():
    now = START
    assert L.fuel_line(None, now, 7).startswith("🔴")
    assert L.fuel_line(now + timedelta(days=3), now, 7).startswith("🔴")
    assert L.fuel_line(now + timedelta(days=10), now, 7).startswith("🟡")
    assert L.fuel_line(now + timedelta(days=30), now, 7).startswith("🟢")


def test_render_status():
    s = L.StructureInfo(SID, "XX-TEST - Test Refinery", "XX-TEST", START + timedelta(days=30))
    names = {40349232: "XX-TEST VII - Moon 3", 45492: "Sylvite"}
    now = START + timedelta(days=1)
    e = L.render_status(s, L.compute_state([started()], now), names, now, 7)
    assert e["title"] == "🌕 XX-TEST VII - Moon 3 · ⚪ R4"
    assert "Drilling" in e["description"] and "━" in e["description"]
    assert "Sylvite" in e["fields"][1]["value"]


def test_build_pop():
    title, embed = build_pop("XX-TEST VII - Moon 3", "Test Refinery", "XX-TEST",
                             ["Sylvite", "Chromite"], "6.94M m³")
    assert "XX-TEST VII - Moon 3" in embed["description"]
    assert "Sylvite · Chromite" in embed["description"]


# Real SeAT API payload (text is already a dict, ore volumes as a bare list)
SEAT_REAL = {
    "notification_id": 1, "type": "MoonminingAutomaticFracture",
    "timestamp": "2026-11-17T09:02:00.000000Z",
    "text": {"moonID": 40000001, "oreVolumeByType": [2301335.3, 773702.4, 1790476.1, 2078121.2],
             "solarSystemID": 30000001, "structureID": 1035000000002,
             "structureName": "XX-TEST - Test Refinery", "structureTypeID": 35835},
}


def test_parse_db_row():
    """SeAT DB row: naive datetime timestamp, raw YAML text with type ids."""
    row = {"type": L.EV_STARTED, "timestamp": START.replace(tzinfo=None), "text": STARTED_YAML}
    ev = L.parse_notification(row)
    assert ev.timestamp == START and ev.ore_types == [45490, 45491, 45492, 46312]


def test_parse_real_seat_payload():
    ev = L.parse_notification(SEAT_REAL)
    assert ev.timestamp == datetime(2026, 11, 17, 9, 2, tzinfo=UTC)
    assert ev.ore_types == [] and len(ev.volumes) == 4


def test_ore_breakdown_maps_by_ascending_type_id():
    ev = L.parse_notification(SEAT_REAL)
    # configured in any order: Chromite, Sylvite, Sperrylite, Scheelite
    pairs = L.ore_breakdown(ev.volumes, ev.ore_types, [45501, 45491, 45499, 45497])
    assert dict(pairs) == {45491: 2301335.3, 45497: 773702.4, 45499: 1790476.1, 45501: 2078121.2}
    assert pairs[0][0] == 45491  # largest first


def test_ore_breakdown_unknown_or_mismatch():
    assert L.ore_breakdown([1.0, 2.0], [], None) == [(None, 2.0), (None, 1.0)]
    assert L.ore_breakdown([1.0, 2.0], [], [45491]) == [(None, 2.0), (None, 1.0)]
    text = L.ore_lines([(None, 2.0)], {})
    assert "Ore #1" in text and "/moons ores" in text


def test_render_rented_without_fuel():
    s = L.StructureInfo(SID, "Rented - Refinery", "XX-TEST", None, fuel_known=False,
                        rented_from="Landlord Corp")
    now = START + timedelta(days=1)
    e = L.render_status(s, L.compute_state([started()], now), {}, now, 7)
    assert "Rented from Landlord Corp" in e["description"]
    assert "Unknown" in e["fields"][0]["value"] and "NO FUEL" not in e["fields"][0]["value"]


def extraction(start=START, ready=READY, auto=AUTO, sid=SID):
    return L.Extraction(98000001, sid, 40349232, start, ready, auto)


def test_extraction_fills_missing_start_notification():
    # Only a pop from the previous cycle is known; the current drill only from the DB table
    old_pop = L.MoonEvent(L.EV_LASER_FIRED, START - timedelta(days=3), SID, volumes=[5.0, 6.0])
    events = L.merge_extractions([old_pop], [extraction()])
    st = L.compute_state(events, START + timedelta(days=1))
    assert st.phase == L.PHASE_DRILLING and st.ready == READY and st.auto == AUTO
    assert st.volumes == [5.0, 6.0] and st.ore_is_last  # ore is from the last cycle


def test_notification_wins_over_extraction_row():
    events = L.merge_extractions([started()], [extraction(start=START + timedelta(seconds=40))])
    assert len(events) == 1
    st = L.compute_state(events, START + timedelta(days=1))
    assert st.volumes and not st.ore_is_last


def test_cancel_notification_beats_extraction_row():
    cancel = L.MoonEvent(L.EV_CANCELLED, START + timedelta(days=1), SID)
    st = L.compute_state(L.merge_extractions([cancel], [extraction()]), START + timedelta(days=2))
    assert st.phase == L.PHASE_IDLE


def test_ore_class_and_value():
    names = {45491: "Sylvite", 45511: "Monazite", 46312: "Brimful Zeolites"}
    assert L.ore_class("Monazite") == 64 and L.ore_class("Brimful Zeolites") == 4
    assert L.ore_class("Veldspar") is None and L.ore_class(None) is None
    pairs = [(45491, 1_000_000.0), (45511, 500_000.0)]
    assert L.moon_class(pairs, names) == 64 and L.class_tag(64) == "🟠 R64"
    prices = {45491: 100.0, 45511: 1000.0}
    assert L.total_value(pairs, prices) == 100_000 * 100 + 50_000 * 1000
    text = L.ore_lines(pairs, names, prices)
    assert "⚪ R4 Sylvite" in text and "🟠 R64 Monazite" in text and "60M ISK" in text


def test_render_status_shows_moon_class():
    s = L.StructureInfo(SID, "XX-TEST - Test Refinery", "XX-TEST", START + timedelta(days=30))
    names = {40349232: "XX-TEST VII - Moon 3", 45490: "Zeolites", 45491: "Sylvite",
             45492: "Bitumens", 46312: "Monazite"}
    now = START + timedelta(days=1)
    e = L.render_status(s, L.compute_state([started()], now), names, now, 7, prices={45492: 50.0})
    assert e["title"].endswith("🟠 R64")
    assert "ISK" in e["fields"][1]["value"] and "ESI" in e["footer"]["text"]


def test_refined_value_beats_raw_market_price():
    market = {45497: 20.0, 36: 100.0, 16639: 1000.0}   # raw Scheelite noise; materials
    yields = {45497: (100, [(36, 100), (16639, 65)])}
    prices = L.ore_unit_prices(market, yields)
    assert prices[45497] == (100 * 100 + 65 * 1000) / 100
    assert prices[36] == 100.0
