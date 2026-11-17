"""Pure logic: parse moon notifications, derive extraction state, render embeds.

No I/O in here, so everything is unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import yaml

ATHANOR_TYPE_ID = 35835

EV_STARTED = "MoonminingExtractionStarted"
EV_CANCELLED = "MoonminingExtractionCancelled"
EV_FINISHED = "MoonminingExtractionFinished"
EV_AUTO_FRACTURE = "MoonminingAutomaticFracture"
EV_LASER_FIRED = "MoonminingLaserFired"

MOON_EVENT_TYPES = (EV_STARTED, EV_CANCELLED, EV_FINISHED, EV_AUTO_FRACTURE, EV_LASER_FIRED)
POP_EVENT_TYPES = (EV_AUTO_FRACTURE, EV_LASER_FIRED)

# A fractured chunk despawns roughly 48h after fracturing.
BELT_LIFETIME = timedelta(hours=48)

_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def filetime_to_dt(value: int) -> datetime:
    """EVE notifications store times as Windows FILETIME (100ns ticks since 1601)."""
    return _FILETIME_EPOCH + timedelta(microseconds=int(value) // 10)


def parse_ts(value: str) -> datetime:
    """Parse an ISO-ish timestamp from SeAT ('2026-11-17 09:02:00' or ISO 8601)."""
    value = value.strip().replace(" ", "T")
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class MoonEvent:
    type: str
    timestamp: datetime
    structure_id: int
    moon_id: int | None = None
    solar_system_id: int | None = None
    structure_name: str | None = None
    ready_time: datetime | None = None
    auto_time: datetime | None = None
    # SeAT's API turns oreVolumeByType into a bare list (type ids lost). CCP orders it by
    # ascending type id, so volumes[i] belongs to the i-th smallest ore type id of the moon.
    volumes: list[float] = field(default_factory=list)
    ore_types: list[int] = field(default_factory=list)  # only if the source kept the ids
    actor_id: int | None = None  # startedBy / firedBy / cancelledBy

    @property
    def key(self) -> str:
        """Dedup key. Several characters receive the same event with different
        notification ids, so key on structure + type + minute."""
        return f"{self.type}:{self.structure_id}:{self.timestamp.strftime('%Y%m%d%H%M')}"


def parse_notification(notif: dict) -> MoonEvent | None:
    """Turn a SeAT CharacterNotification into a MoonEvent (None if not a moon event)."""
    ntype = notif.get("type")
    if ntype not in MOON_EVENT_TYPES:
        return None
    body = notif.get("object") or notif.get("text") or {}
    if isinstance(body, str):
        try:
            body = yaml.safe_load(body) or {}
        except yaml.YAMLError:
            return None
    if not isinstance(body, dict) or "structureID" not in body:
        return None

    raw_ore = body.get("oreVolumeByType") or []
    if isinstance(raw_ore, dict):
        pairs = sorted((int(k), float(v)) for k, v in raw_ore.items())
        ore_types, volumes = [k for k, _ in pairs], [v for _, v in pairs]
    else:
        ore_types, volumes = [], [float(v) for v in raw_ore]
    actor = body.get("startedBy") or body.get("firedBy") or body.get("cancelledBy")
    return MoonEvent(
        type=ntype,
        timestamp=parse_ts(str(notif["timestamp"])),
        structure_id=int(body["structureID"]),
        moon_id=int(body["moonID"]) if body.get("moonID") else None,
        solar_system_id=int(body["solarSystemID"]) if body.get("solarSystemID") else None,
        structure_name=body.get("structureName"),
        ready_time=filetime_to_dt(body["readyTime"]) if body.get("readyTime") else None,
        auto_time=filetime_to_dt(body["autoTime"]) if body.get("autoTime") else None,
        volumes=volumes,
        ore_types=ore_types,
        actor_id=int(actor) if isinstance(actor, int) else None,
    )


@dataclass
class Extraction:
    """One row of SeAT's corporation_industry_mining_extractions (from ESI)."""
    corp_id: int
    structure_id: int
    moon_id: int | None
    start: datetime
    ready: datetime
    auto: datetime | None


def merge_extractions(events: list[MoonEvent], extractions: list[Extraction]) -> list[MoonEvent]:
    """Add a synthetic ExtractionStarted for every extraction that has no matching
    notification (e.g. the character joined SeAT after the drill was started).
    Notifications win when both exist, because they also carry the ore volumes."""
    started = [e for e in events if e.type == EV_STARTED]
    out = list(events)
    for x in extractions:
        if any(e.ready_time and abs(e.ready_time - x.ready) < timedelta(minutes=10)
               or abs(e.timestamp - x.start) < timedelta(hours=2) for e in started):
            continue
        out.append(MoonEvent(EV_STARTED, x.start, x.structure_id, moon_id=x.moon_id,
                             ready_time=x.ready, auto_time=x.auto))
    return out


# --------------------------------------------------------------------------- state

PHASE_UNKNOWN = "unknown"
PHASE_IDLE = "idle"
PHASE_DRILLING = "drilling"
PHASE_READY = "ready"
PHASE_FRACTURED = "fractured"


@dataclass
class MoonState:
    phase: str = PHASE_UNKNOWN
    moon_id: int | None = None
    started: datetime | None = None
    ready: datetime | None = None
    auto: datetime | None = None
    fractured: datetime | None = None
    volumes: list[float] = field(default_factory=list)  # current, or last known
    ore_types: list[int] = field(default_factory=list)
    ore_is_last: bool = False  # True when ore is from a previous, finished extraction

    def progress(self, now: datetime) -> float:
        if self.phase in (PHASE_READY, PHASE_FRACTURED):
            return 1.0
        if self.phase != PHASE_DRILLING or not (self.started and self.ready):
            return 0.0
        total = (self.ready - self.started).total_seconds()
        if total <= 0:
            return 1.0
        return max(0.0, min(1.0, (now - self.started).total_seconds() / total))


def compute_state(events: list[MoonEvent], now: datetime) -> MoonState:
    """Replay the moon events of ONE structure and return its current state."""
    st = MoonState()
    stale = False  # volumes belong to an earlier extraction than the current one
    for ev in sorted(events, key=lambda e: e.timestamp):
        st.moon_id = ev.moon_id or st.moon_id
        if ev.volumes:
            st.volumes, st.ore_types = list(ev.volumes), list(ev.ore_types)
            stale = False
        elif ev.type == EV_STARTED:  # extraction without ore data (from the extraction table)
            stale = True
        if ev.type == EV_STARTED:
            st.phase, st.started, st.ready, st.auto, st.fractured = (
                PHASE_DRILLING, ev.timestamp, ev.ready_time, ev.auto_time, None)
        elif ev.type == EV_FINISHED:
            st.phase, st.fractured = PHASE_READY, None
            st.ready = st.ready or ev.timestamp
            st.auto = ev.auto_time or st.auto
        elif ev.type in POP_EVENT_TYPES:
            st.phase, st.fractured = PHASE_FRACTURED, ev.timestamp
        elif ev.type == EV_CANCELLED:
            st.phase = PHASE_IDLE

    # Advance by clock when a follow-up notification has not arrived (yet).
    if st.phase == PHASE_DRILLING and st.ready and now >= st.ready:
        st.phase = PHASE_READY
    if st.phase == PHASE_READY and st.auto and now >= st.auto:
        st.phase, st.fractured = PHASE_FRACTURED, st.auto
    if st.phase == PHASE_FRACTURED and st.fractured and now >= st.fractured + BELT_LIFETIME:
        st.phase = PHASE_IDLE

    st.ore_is_last = bool(st.volumes) and (stale or st.phase in (PHASE_IDLE, PHASE_UNKNOWN))
    return st


# --------------------------------------------------------------------------- rendering

BAR_WIDTH = 20

COLOR = {
    PHASE_DRILLING: 0x3B82F6,
    PHASE_READY: 0x22C55E,
    PHASE_FRACTURED: 0xF97316,
    PHASE_IDLE: 0x6B7280,
    PHASE_UNKNOWN: 0x6B7280,
}


def progress_bar(fraction: float, width: int = BAR_WIDTH) -> str:
    filled = round(max(0.0, min(1.0, fraction)) * width)
    return f"`{'━' * filled}{'─' * (width - filled)}|` {int(fraction * 100)}%"


def dts(dt: datetime, style: str = "f") -> str:
    """Discord timestamp: renders in every viewer's local time, and <t:..:R> counts live."""
    return f"<t:{int(dt.timestamp())}:{style}>"


def eve_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%a %d %b %H:%M") + " EVE"


def fmt_volume(m3: float) -> str:
    if m3 >= 1_000_000:
        return f"{m3 / 1_000_000:.2f}M m³"
    if m3 >= 1_000:
        return f"{m3 / 1_000:.0f}k m³"
    return f"{m3:.0f} m³"


def ore_breakdown(volumes: list[float], known_types: list[int],
                  configured: list[int] | None) -> list[tuple[int | None, float]]:
    """Pair volumes with type ids (from the event, else the /moons ores config),
    largest first. Unknown types come back as None."""
    types = known_types or sorted(configured or [])
    if types and len(types) == len(volumes):
        pairs = list(zip(types, volumes))
    else:
        pairs = [(None, v) for v in volumes]
    return sorted(pairs, key=lambda p: p[1], reverse=True)


def ore_name(tid: int | None, idx: int, names: dict[int, str]) -> str:
    return names.get(tid, f"Type {tid}") if tid else f"Ore #{idx}"


def ore_lines(pairs: list[tuple[int | None, float]], names: dict[int, str]) -> str:
    lines = [f"• {ore_name(t, i + 1, names)} — {fmt_volume(v)}" for i, (t, v) in enumerate(pairs)]
    lines.append(f"**Total:** {fmt_volume(sum(v for _, v in pairs))}")
    if any(t is None for t, _ in pairs):
        lines.append("_Ore names unknown — set them once with `/moons ores`_")
    return "\n".join(lines)


def fuel_line(fuel_expires: datetime | None, now: datetime, warn_days: int) -> str:
    if fuel_expires is None:
        return "🔴 **NO FUEL** — structure is in low power!"
    left = fuel_expires - now
    days = left.total_seconds() / 86400
    if days < warn_days:
        icon = "🔴"
    elif days < warn_days * 2:
        icon = "🟡"
    else:
        icon = "🟢"
    return f"{icon} **{days:.1f} days** left (runs dry {dts(fuel_expires, 'D')})"


@dataclass
class StructureInfo:
    structure_id: int
    name: str
    system: str
    fuel_expires: datetime | None
    drill_online: bool = True
    fuel_known: bool = True        # False for rented moons whose structure SeAT can't see
    rented_from: str | None = None  # landlord corp name for rented moons


def parse_structure(raw: dict) -> StructureInfo | None:
    """SeAT CorporationStructure -> StructureInfo, or None if it is not an Athanor."""
    if int((raw.get("type") or {}).get("typeID") or 0) != ATHANOR_TYPE_ID:
        return None
    services = raw.get("services") or []
    drill = [s for s in services if "moon" in (s.get("name") or "").lower()]
    fuel = raw.get("fuel_expires")
    return StructureInfo(
        structure_id=int(raw["structure_id"]),
        name=(raw.get("info") or {}).get("name") or str(raw["structure_id"]),
        system=(raw.get("solar_system") or {}).get("name") or "?",
        fuel_expires=parse_ts(fuel) if fuel else None,
        drill_online=all(s.get("state") == "online" for s in drill) if drill else True,
    )


def render_status(s: StructureInfo, st: MoonState, names: dict[int, str],
                  now: datetime, warn_days: int,
                  configured_ores: list[int] | None = None) -> dict:
    """Return a discord embed as a plain dict (discord.Embed.from_dict)."""
    moon = names.get(st.moon_id, s.system) if st.moon_id else s.system
    lines: list[str] = []

    if st.phase == PHASE_DRILLING:
        lines.append(f"⛏️ **Drilling** — chunk ready {dts(st.ready, 'R')}")
        lines.append(progress_bar(st.progress(now)))
        lines.append(f"Ready: {dts(st.ready)} ({eve_time(st.ready)})")
        if st.auto:
            lines.append(f"Autofracture: {dts(st.auto)} ({eve_time(st.auto)})")
    elif st.phase == PHASE_READY:
        lines.append("🟢 **CHUNK READY** — fire the laser!")
        lines.append(progress_bar(1.0))
        if st.auto:
            lines.append(f"Autofracture {dts(st.auto, 'R')} ({eve_time(st.auto)})")
    elif st.phase == PHASE_FRACTURED:
        lines.append("💥 **FRACTURED** — the belt is open, go mine it!")
        lines.append(f"Popped {dts(st.fractured, 'R')} · belt despawns ~{dts(st.fractured + BELT_LIFETIME, 'R')}")
    elif st.phase == PHASE_IDLE:
        lines.append("💤 **Idle** — no extraction running")
    else:
        lines.append("❔ **No extraction data yet** — waiting for the next notification")

    if not s.drill_online:
        lines.append("⚠️ Moon Drilling service is **offline**")

    fuel = (fuel_line(s.fuel_expires, now, warn_days) if s.fuel_known
            else "❔ Unknown — rented, fuel is the landlord's job")
    fields = [{"name": "⛽ Fuel", "value": fuel, "inline": False}]
    if st.volumes:
        title = "🪨 Last extraction" if st.ore_is_last else "🪨 Ore"
        pairs = ore_breakdown(st.volumes, st.ore_types, configured_ores)
        fields.append({"name": title, "value": ore_lines(pairs, names), "inline": False})

    return {
        "title": f"🌕 {moon}",
        "description": f"**{s.name}**\n"
                       + (f"🏷️ Rented from {s.rented_from}\n" if s.rented_from else "")
                       + "\n" + "\n".join(lines),
        "color": COLOR.get(st.phase, COLOR[PHASE_UNKNOWN]),
        "fields": fields,
        "footer": {"text": "Last update"},
        "timestamp": now.isoformat(),
    }
