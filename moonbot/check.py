"""Dry run without Discord: `python -m moonbot.check`

Reads the same environment as the bot and prints what it would see. Use it to verify
the API key, the optional SeAT DB connection and the data before going live.
"""
import asyncio
import os
from datetime import datetime, timezone

import aiohttp

from . import logic as L
from .logic import parse_structure
from .clients import EsiClient, SeatClient, SeatDb


async def main():
    url = os.environ["SEAT_URL"]
    token = os.environ["SEAT_TOKEN"]
    corp = int(os.environ["CORP_ID"])
    chars = [int(x) for x in os.environ["NOTIFY_CHAR_IDS"].split(",") if x.strip()]
    db_host = os.environ.get("SEAT_DB_HOST", "")
    now = datetime.now(timezone.utc)

    async with aiohttp.ClientSession() as s:
        seat, esi = SeatClient(s, url, token), EsiClient(s)
        raw = await seat.structures(corp)
        athanors = [a for a in (parse_structure(r) for r in raw) if a]
        print(f"SeAT API: {len(raw)} structures, {len(athanors)} Athanors")

        if db_host:
            db = SeatDb(db_host, int(os.environ.get("SEAT_DB_PORT", "3306")),
                        os.environ.get("SEAT_DB_NAME", "seat"), os.environ.get("SEAT_DB_USER", "seat"),
                        os.environ.get("SEAT_DB_PASSWORD", ""))
            notifs = await db.moon_notifications(chars)
            print(f"SeAT DB @ {db_host}: {len(notifs)} moon notifications")
        else:
            notifs = []
            for c in chars:
                notifs += await seat.moon_notifications(c)
            print(f"SeAT API: {len(notifs)} moon notifications (no ore names, set SEAT_DB_HOST)")

        events: dict[str, L.MoonEvent] = {}
        for n in notifs:
            ev = L.parse_notification(n)
            if ev:
                events[ev.key] = ev

        by_sid: dict[int, list] = {}
        for ev in events.values():
            by_sid.setdefault(ev.structure_id, []).append(ev)
        ids = {e.moon_id for e in events.values()} | {t for e in events.values() for t in e.ore_types}
        names = await esi.names(list(ids))
        for a in athanors:
            st = L.compute_state(by_sid.get(a.structure_id, []), now)
            print(f"\n{a.name} [{a.system}]  fuel={a.fuel_expires}  drill_online={a.drill_online}")
            print(f"  moon={names.get(st.moon_id)} phase={st.phase} ready={st.ready} "
                  f"progress={st.progress(now):.0%}")
            pairs = L.ore_breakdown(st.volumes, st.ore_types, None)
            print("    ore:", ", ".join(f"{L.ore_name(t, i + 1, names)} {L.fmt_volume(v)}"
                                       for i, (t, v) in enumerate(pairs)),
                  "(last extraction)" if st.ore_is_last else "")


if __name__ == "__main__":
    asyncio.run(main())
