"""SQLite storage: settings, seen moon events, status message ids, name cache."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from .logic import MoonEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS events (
    event_key TEXT PRIMARY KEY,
    structure_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    ts TEXT NOT NULL,
    data TEXT NOT NULL,
    announced INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_events_structure ON events(structure_id);
CREATE TABLE IF NOT EXISTS status_messages (
    structure_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS names (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS moon_ores (moon_id INTEGER PRIMARY KEY, type_ids TEXT NOT NULL);
"""


def _event_to_json(ev: MoonEvent) -> str:
    def dt(v):
        return v.isoformat() if v else None
    return json.dumps({
        "type": ev.type, "timestamp": dt(ev.timestamp), "structure_id": ev.structure_id,
        "moon_id": ev.moon_id, "solar_system_id": ev.solar_system_id,
        "structure_name": ev.structure_name, "ready_time": dt(ev.ready_time),
        "auto_time": dt(ev.auto_time), "volumes": ev.volumes, "ore_types": ev.ore_types,
        "actor_id": ev.actor_id,
    })


def _event_from_json(raw: str) -> MoonEvent:
    d = json.loads(raw)

    def dt(v):
        return datetime.fromisoformat(v) if v else None
    return MoonEvent(
        type=d["type"], timestamp=dt(d["timestamp"]), structure_id=d["structure_id"],
        moon_id=d["moon_id"], solar_system_id=d["solar_system_id"],
        structure_name=d["structure_name"], ready_time=dt(d["ready_time"]),
        auto_time=dt(d["auto_time"]), volumes=d.get("volumes") or [],
        ore_types=d.get("ore_types") or [],
        actor_id=d["actor_id"],
    )


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self.db.commit()

    # settings
    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key: str, value: str | None) -> None:
        if value is None:
            self.db.execute("DELETE FROM settings WHERE key=?", (key,))
        else:
            self.db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, value))
        self.db.commit()

    # events
    def add_event(self, ev: MoonEvent, announced: bool) -> bool:
        """Insert; returns True if the event was new."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO events(event_key, structure_id, type, ts, data, announced) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ev.key, ev.structure_id, ev.type, ev.timestamp.isoformat(), _event_to_json(ev), int(announced)))
        self.db.commit()
        return cur.rowcount == 1

    def mark_announced(self, key: str) -> None:
        self.db.execute("UPDATE events SET announced=1 WHERE event_key=?", (key,))
        self.db.commit()

    def events_for(self, structure_id: int) -> list[MoonEvent]:
        rows = self.db.execute("SELECT data FROM events WHERE structure_id=? ORDER BY ts",
                               (structure_id,)).fetchall()
        return [_event_from_json(r[0]) for r in rows]

    def event_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    # status messages
    def status_messages(self) -> dict[int, tuple[int, int]]:
        rows = self.db.execute("SELECT structure_id, channel_id, message_id FROM status_messages")
        return {sid: (cid, mid) for sid, cid, mid in rows}

    def set_status_message(self, structure_id: int, channel_id: int, message_id: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO status_messages VALUES (?, ?, ?)",
                        (structure_id, channel_id, message_id))
        self.db.commit()

    def drop_status_message(self, structure_id: int) -> None:
        self.db.execute("DELETE FROM status_messages WHERE structure_id=?", (structure_id,))
        self.db.commit()

    # ore type config per moon (needed because SeAT drops the type ids)
    def moon_ores(self, moon_id: int | None) -> list[int] | None:
        if not moon_id:
            return None
        row = self.db.execute("SELECT type_ids FROM moon_ores WHERE moon_id=?", (moon_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_moon_ores(self, moon_id: int, type_ids: list[int]) -> None:
        self.db.execute("INSERT OR REPLACE INTO moon_ores VALUES (?, ?)", (moon_id, json.dumps(sorted(type_ids))))
        self.db.commit()

    # names
    def names(self, ids) -> dict[int, str]:
        ids = list(ids)
        if not ids:
            return {}
        q = ",".join("?" * len(ids))
        return dict(self.db.execute(f"SELECT id, name FROM names WHERE id IN ({q})", ids).fetchall())

    def save_names(self, names: dict[int, str]) -> None:
        self.db.executemany("INSERT OR REPLACE INTO names VALUES (?, ?)", names.items())
        self.db.commit()
