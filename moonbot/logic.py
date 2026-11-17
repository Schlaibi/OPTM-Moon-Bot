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
    idle_since: datetime | None = None  # drill stopped (pop / cancel) and not restarted since

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
    # Notifications only have minute precision. Within one minute a cancel / pop / finish
    # belongs to the previous extraction, so a new start must be replayed last
    # (e.g. "cancel + restart" or "fire laser + start next drill" in the same minute).
    for ev in sorted(events, key=lambda e: (e.timestamp, e.type == EV_STARTED)):
        st.moon_id = ev.moon_id or st.moon_id
        if ev.volumes:
            st.volumes, st.ore_types = list(ev.volumes), list(ev.ore_types)
            stale = False
        elif ev.type == EV_STARTED:  # extraction without ore data (from the extraction table)
            stale = True
        if ev.type == EV_STARTED:
            st.phase, st.started, st.ready, st.auto, st.fractured = (
                PHASE_DRILLING, ev.timestamp, ev.ready_time, ev.auto_time, None)
            st.idle_since = None
        elif ev.type == EV_FINISHED:
            st.phase, st.fractured = PHASE_READY, None
            st.ready = st.ready or ev.timestamp
            st.auto = ev.auto_time or st.auto
        elif ev.type in POP_EVENT_TYPES:
            st.phase, st.fractured = PHASE_FRACTURED, ev.timestamp
            st.idle_since = ev.timestamp
        elif ev.type == EV_CANCELLED:
            st.phase = PHASE_IDLE
            st.idle_since = ev.timestamp

    # Advance by clock when a follow-up notification has not arrived (yet).
    if st.phase == PHASE_DRILLING and st.ready and now >= st.ready:
        st.phase = PHASE_READY
    if st.phase == PHASE_READY and st.auto and now >= st.auto:
        st.phase, st.fractured = PHASE_FRACTURED, st.auto
        st.idle_since = st.auto
    if st.phase == PHASE_FRACTURED and st.fractured and now >= st.fractured + BELT_LIFETIME:
        st.phase = PHASE_IDLE

    st.ore_is_last = bool(st.volumes) and (stale or st.phase in (PHASE_IDLE, PHASE_UNKNOWN))
    return st


# --------------------------------------------------------------------------- alerts

ALERT_IDLE_CLASSES = (32, 64)        # only valuable moons get the "drill is off" ping
ALERT_IDLE_AFTER = timedelta(hours=4)
ALERT_FUEL_BELOW = timedelta(days=2)
ALERT_FUEL_RESET = timedelta(days=3)  # re-arm the fuel alert only after a real refuel


def drill_idle_alert(st: MoonState, mclass: int | None, now: datetime) -> bool | None:
    """True: alert, False: condition cleared (re-arm), None: keep the current flag."""
    if st.phase in (PHASE_DRILLING, PHASE_READY):
        return False
    if (mclass in ALERT_IDLE_CLASSES and st.idle_since
            and st.phase in (PHASE_FRACTURED, PHASE_IDLE) and now - st.idle_since >= ALERT_IDLE_AFTER):
        return True
    return None


def fuel_alert(s: "StructureInfo", now: datetime) -> bool | None:
    """True: alert, False: refuelled (re-arm), None: keep the current flag."""
    if not s.fuel_known:
        return None
    if s.fuel_expires is None or s.fuel_expires - now < ALERT_FUEL_BELOW:
        return True
    if s.fuel_expires - now >= ALERT_FUEL_RESET:
        return False
    return None


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


# Moon ore rarity by base ore name. Variants ("Brimful Zeolites", "Shining Xenotime", ...)
# end with the base name, so the last word decides.
MOON_ORE_CLASS = {
    **dict.fromkeys(("Zeolites", "Sylvite", "Bitumens", "Coesite"), 4),
    **dict.fromkeys(("Cobaltite", "Euxenite", "Titanite", "Scheelite"), 8),
    **dict.fromkeys(("Otavite", "Sperrylite", "Vanadinite", "Chromite"), 16),
    **dict.fromkeys(("Carnotite", "Zircon", "Pollucite", "Cinnabar"), 32),
    **dict.fromkeys(("Xenotime", "Monazite", "Loparite", "Ytterbite"), 64),
}
CLASS_ICON = {4: "⚪", 8: "🟢", 16: "🔵", 32: "🟣", 64: "🟠"}
MOON_ORE_M3_PER_UNIT = 10.0


def ore_class(name: str | None) -> int | None:
    """Rarity (4/8/16/32/64) of a moon ore name, None if not a moon ore / unknown."""
    return MOON_ORE_CLASS.get(name.split()[-1]) if name else None


def moon_class(pairs: list[tuple[int | None, float]], names: dict[int, str]) -> int | None:
    """A moon is classed by its rarest ore (an R64 moon has at least one R64 ore)."""
    classes = [c for t, _ in pairs if (c := ore_class(names.get(t)))]
    return max(classes) if classes else None


def class_tag(c: int | None) -> str:
    return f"{CLASS_ICON[c]} R{c}" if c else ""


def ore_value(tid: int | None, m3: float, prices: dict[int, float]) -> float | None:
    """Rough ISK value of a chunk part: units × ESI average price of the raw ore."""
    price = prices.get(tid) if tid else None
    return m3 / MOON_ORE_M3_PER_UNIT * price if price else None


def ore_unit_prices(market: dict[int, float],
                    yields: dict[int, tuple[int, list[tuple[int, int]]]]) -> dict[int, float]:
    """ISK per unit of ore. Raw moon ore barely trades, so its market average is noise;
    the refined value (moon goo + minerals, 100% yield) is used wherever yields are known."""
    out = dict(market)
    for tid, (portion, mats) in yields.items():
        value = sum(qty * market.get(mat, 0.0) for mat, qty in mats)
        if value:
            out[tid] = value / max(portion, 1)
    return out


def fmt_isk(isk: float) -> str:
    if isk >= 1e9:
        return f"{isk / 1e9:.2f}B ISK"
    if isk >= 1e6:
        return f"{isk / 1e6:.0f}M ISK"
    return f"{isk / 1e3:.0f}k ISK"


def total_value(pairs: list[tuple[int | None, float]], prices: dict[int, float]) -> float | None:
    vals = [ore_value(t, v, prices) for t, v in pairs]
    return sum(v for v in vals if v) if any(vals) else None


def ore_label(tid: int | None, idx: int, names: dict[int, str]) -> str:
    """'🟠 R64 Monazite' (or just the name if the rarity is unknown)."""
    name = ore_name(tid, idx, names)
    tag = class_tag(ore_class(names.get(tid)))
    return f"{tag} {name}" if tag else name


def ore_lines(pairs: list[tuple[int | None, float]], names: dict[int, str],
              prices: dict[int, float] | None = None) -> str:
    prices = prices or {}
    lines = []
    for i, (t, v) in enumerate(pairs):
        val = ore_value(t, v, prices)
        lines.append(f"{ore_label(t, i + 1, names)} — {fmt_volume(v)}"
                     + (f" · ~{fmt_isk(val)}" if val else ""))
    total = fmt_volume(sum(v for _, v in pairs))
    tv = total_value(pairs, prices)
    lines.append(f"**Total:** {total}" + (f" · **~{fmt_isk(tv)}**" if tv else ""))
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
                  configured_ores: list[int] | None = None,
                  prices: dict[int, float] | None = None) -> dict:
    """Return a discord embed as a plain dict (discord.Embed.from_dict)."""
    moon = names.get(st.moon_id, s.system) if st.moon_id else s.system
    pairs = ore_breakdown(st.volumes, st.ore_types, configured_ores) if st.volumes else []
    if not pairs and configured_ores:  # no volumes yet, but /moons ores tells the class
        pairs = [(t, 0.0) for t in configured_ores]
    mclass = moon_class(pairs, names)
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
        fields.append({"name": title, "value": ore_lines(pairs, names, prices), "inline": False})

    return {
        "title": f"🌕 {moon}" + (f" · {class_tag(mclass)}" if mclass else ""),
        "description": f"**{s.name}**\n"
                       + (f"🏷️ Rented from {s.rented_from}\n" if s.rented_from else "")
                       + "\n" + "\n".join(lines),
        "color": COLOR.get(st.phase, COLOR[PHASE_UNKNOWN]),
        "fields": fields,
        "footer": {"text": "Last update" + (" · value ≈ refined, ESI average prices" if prices and st.volumes else "")},
        "timestamp": now.isoformat(),
    }
