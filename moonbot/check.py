"""Dry run without Discord: `python -m moonbot.check`

Reads SEAT_URL, SEAT_TOKEN, CORP_ID, NOTIFY_CHAR_IDS from the environment and prints
what the bot would see. Use it to verify the API key and data before going live.
"""
import asyncio
import os
from datetime import datetime, timezone

import aiohttp

from . import logic as L
from .logic import parse_structure
from .clients import EsiClient, SeatClient


async def main():
    url = os.environ["SEAT_URL"]
    token = os.environ["SEAT_TOKEN"]
    corp = int(os.environ["CORP_ID"])
    chars = [int(x) for x in os.environ["NOTIFY_CHAR_IDS"].split(",") if x.strip()]
    now = datetime.now(timezone.utc)

    async with aiohttp.ClientSession() as s:
        seat, esi = SeatClient(s, url, token), EsiClient(s)
        raw = await seat.structures(corp)
        athanors = [a for a in (parse_structure(r) for r in raw) if a]
        print(f"{len(raw)} structures, {len(athanors)} Athanors")

        events: dict[str, L.MoonEvent] = {}
        for c in chars:
            notifs = await seat.moon_notifications(c)
            print(f"char {c}: {len(notifs)} moon notifications")
            for n in notifs:
                ev = L.parse_notification(n)
                if ev:
                    events[ev.key] = ev

        by_sid: dict[int, list] = {}
        for ev in events.values():
            by_sid.setdefault(ev.structure_id, []).append(ev)
        ids = {e.moon_id for e in events.values()}
        names = await esi.names(list(ids))
        for a in athanors:
            st = L.compute_state(by_sid.get(a.structure_id, []), now)
            print(f"\n{a.name} [{a.system}]  fuel={a.fuel_expires}  drill_online={a.drill_online}")
            print(f"  moon={names.get(st.moon_id)} phase={st.phase} ready={st.ready} "
                  f"progress={st.progress(now):.0%}")
            print("    ore volumes (ascending type id):", [L.fmt_volume(v) for v in st.volumes],
                  "(last extraction)" if st.ore_is_last else "")


if __name__ == "__main__":
    asyncio.run(main())
