"""Discord bot: standing moon status posts + pop announcements."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

from . import logic as L
from .clients import EsiClient, SeatClient, SeatDb
from .messages import build_pop
from .store import Store

log = logging.getLogger("moonbot")


def env(name: str, default: str | None = None, required: bool = False) -> str:
    v = os.environ.get(name, default)
    if required and not v:
        raise SystemExit(f"Missing environment variable {name}")
    return v or ""


DISCORD_TOKEN = env("DISCORD_TOKEN", required=True)
SEAT_URL = env("SEAT_URL", required=True)
SEAT_TOKEN = env("SEAT_TOKEN", required=True)
CORP_ID = int(env("CORP_ID", required=True))
NOTIFY_CHAR_IDS = [int(x) for x in env("NOTIFY_CHAR_IDS", required=True).split(",") if x.strip()]
TAX_COLLECTOR = env("TAX_COLLECTOR", "The tax collector")
GUILD_ID = int(env("GUILD_ID", "0") or 0)
DB_PATH = env("DB_PATH", "/data/moonbot.db")
STATUS_INTERVAL_MIN = int(env("STATUS_INTERVAL_MIN", "60"))
NOTIF_INTERVAL_MIN = int(env("NOTIF_INTERVAL_MIN", "10"))
PING_MAX_AGE_H = int(env("PING_MAX_AGE_H", "6"))  # never ping for pops older than this

# Optional: read notifications straight from SeAT's MariaDB (keeps the ore type ids)
SEAT_DB_HOST = env("SEAT_DB_HOST", "")
SEAT_DB_PORT = int(env("SEAT_DB_PORT", "3306"))
SEAT_DB_NAME = env("SEAT_DB_NAME", "seat")
SEAT_DB_USER = env("SEAT_DB_USER", "seat")
SEAT_DB_PASSWORD = env("SEAT_DB_PASSWORD", "")

K_STATUS_CH, K_PING_CH, K_PING_ROLE, K_FUEL_WARN = "status_channel", "ping_channel", "ping_role", "fuel_warn_days"


class MoonBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.store = Store(DB_PATH)
        self.lock = asyncio.Lock()
        self.http_session: aiohttp.ClientSession | None = None
        self.seat: SeatClient | None = None
        self.esi: EsiClient | None = None
        self.known_moons: dict[int, tuple[str, L.MoonState]] = {}  # for /moons ores
        self.tree.add_command(build_commands(self))

    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        self.seat = SeatClient(self.http_session, SEAT_URL, SEAT_TOKEN)
        self.esi = EsiClient(self.http_session)
        self.seatdb = SeatDb(SEAT_DB_HOST, SEAT_DB_PORT, SEAT_DB_NAME, SEAT_DB_USER,
                             SEAT_DB_PASSWORD) if SEAT_DB_HOST else None
        log.info("Notification source: %s", f"SeAT DB @ {SEAT_DB_HOST}" if self.seatdb else "SeAT API")
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.notif_loop.change_interval(minutes=NOTIF_INTERVAL_MIN)
        self.status_loop.change_interval(minutes=STATUS_INTERVAL_MIN)
        self.notif_loop.start()
        self.status_loop.start()

    async def close(self):
        if self.http_session:
            await self.http_session.close()
        await super().close()

    # ------------------------------------------------------------------ helpers

    def warn_days(self) -> int:
        return int(self.store.get(K_FUEL_WARN, "7"))

    def channel(self, key: str) -> discord.abc.Messageable | None:
        cid = self.store.get(key)
        return self.get_channel(int(cid)) if cid else None

    async def resolve_names(self, ids) -> dict[int, str]:
        ids = {int(i) for i in ids if i}
        known = self.store.names(ids)
        missing = [i for i in ids if i not in known]
        if missing:
            try:
                fresh = await self.esi.names(missing)
                self.store.save_names(fresh)
                known.update(fresh)
            except Exception:
                log.exception("ESI name lookup failed")
        return known

    # ------------------------------------------------------------------ notifications

    async def poll_notifications(self) -> int:
        """Fetch moon notifications, store new events, announce fresh pops. Returns #new events."""
        first_run = self.store.event_count() == 0
        source = "db" if self.seatdb else "api"
        # Read the whole history on first run or after switching source (to pick up
        # ore type ids for stored events); afterwards only the last few days.
        full = first_run or self.store.get("events_source") != source
        since = None if full else datetime.now(timezone.utc) - timedelta(days=3)

        rows: list[dict] = []
        if self.seatdb:
            rows = await self.seatdb.moon_notifications(NOTIFY_CHAR_IDS, since)
        else:
            for cid in NOTIFY_CHAR_IDS:
                try:
                    rows += await self.seat.moon_notifications(cid, since)
                except Exception:
                    log.exception("Fetching notifications for %s failed", cid)
        events: dict[str, L.MoonEvent] = {}
        for n in rows:
            ev = L.parse_notification(n)
            if ev:
                events[ev.key] = ev

        now = datetime.now(timezone.utc)
        changed = 0
        for ev in sorted(events.values(), key=lambda e: e.timestamp):
            is_pop = ev.type in L.POP_EVENT_TYPES
            fresh = now - ev.timestamp < timedelta(hours=PING_MAX_AGE_H)
            should_announce = is_pop and fresh and not first_run
            result = self.store.add_event(ev, announced=not should_announce)
            if result != "same":
                changed += 1
            if result == "new" and should_announce and await self.announce_pop(ev):
                self.store.mark_announced(ev.key)
        self.store.set("events_source", source)
        if full:
            log.info("Full import from %s: %d moon events", source, len(events))
        return changed

    async def announce_current_pop(self, structure_id: int) -> str | None:
        """Manually (re-)announce the open belt of one Athanor. Returns an error text or None."""
        events = self.store.events_for(structure_id)
        st = L.compute_state(events, datetime.now(timezone.utc))
        if st.phase != L.PHASE_FRACTURED or not events:
            return "This moon has no open belt right now."
        last = events[-1]
        ev = L.MoonEvent(L.EV_AUTO_FRACTURE, st.fractured, structure_id, moon_id=st.moon_id,
                         solar_system_id=last.solar_system_id, structure_name=last.structure_name,
                         volumes=st.volumes, ore_types=st.ore_types)
        if not await self.announce_pop(ev):
            return "No ping channel configured, run /moons setup first."
        return None

    async def announce_pop(self, ev: L.MoonEvent) -> bool:
        ch = self.channel(K_PING_CH)
        if ch is None:
            log.warning("Pop for %s but no ping channel configured", ev.structure_id)
            return False
        pairs = L.ore_breakdown(ev.volumes, ev.ore_types, self.store.moon_ores(ev.moon_id))
        names = await self.resolve_names([ev.moon_id, ev.solar_system_id, *(t for t, _ in pairs)])
        _, embed = build_pop(
            moon=names.get(ev.moon_id, "A moon"),
            structure=ev.structure_name or str(ev.structure_id),
            system=names.get(ev.solar_system_id, "?"),
            ore_names=[L.ore_name(t, i + 1, names) for i, (t, _) in enumerate(pairs)],
            total=L.fmt_volume(sum(ev.volumes)) if ev.volumes else None,
            collector=TAX_COLLECTOR,
        )
        role = self.store.get(K_PING_ROLE)
        content = f"<@&{role}>" if role else "@here"
        await ch.send(content=content, embed=discord.Embed.from_dict(embed),
                            allowed_mentions=discord.AllowedMentions(everyone=True, roles=True))
        return True

    # ------------------------------------------------------------------ status posts

    async def refresh_status(self) -> int:
        ch = self.channel(K_STATUS_CH)
        raw = await self.seat.structures(CORP_ID)
        structures = [s for s in (L.parse_structure(r) for r in raw) if s]
        now = datetime.now(timezone.utc)

        states = {s.structure_id: L.compute_state(self.store.events_for(s.structure_id), now)
                  for s in structures}
        configured = {sid: self.store.moon_ores(st.moon_id) for sid, st in states.items()}
        ids = set()
        for sid, st in states.items():
            ids.add(st.moon_id)
            ids.update(st.ore_types)
            ids.update(configured[sid] or [])
        names = await self.resolve_names(ids)
        self.known_moons = {s.structure_id: (s.name, states[s.structure_id]) for s in structures}
        if ch is None:  # not set up yet: only refresh the cache for /moons ores
            return len(structures)

        def sort_key(s):
            st = states[s.structure_id]
            return names.get(st.moon_id, s.system) if st.moon_id else s.system
        structures.sort(key=sort_key)

        existing = self.store.status_messages()
        for s in structures:
            embed = discord.Embed.from_dict(
                L.render_status(s, states[s.structure_id], names, now, self.warn_days(),
                                configured[s.structure_id]))
            await self.upsert_message(ch, s.structure_id, existing.get(s.structure_id), embed)

        gone = set(existing) - {s.structure_id for s in structures}
        for sid in gone:
            await self.delete_message(*existing[sid])
            self.store.drop_status_message(sid)
        return len(structures)

    async def upsert_message(self, ch, sid: int, existing: tuple[int, int] | None, embed: discord.Embed):
        if existing:
            cid, mid = existing
            if cid == ch.id:
                try:
                    await ch.get_partial_message(mid).edit(embed=embed)
                    return
                except discord.NotFound:
                    pass
            else:
                await self.delete_message(cid, mid)
        msg = await ch.send(embed=embed)
        self.store.set_status_message(sid, ch.id, msg.id)

    async def delete_message(self, channel_id: int, message_id: int):
        ch = self.get_channel(channel_id)
        if ch is None:
            return
        try:
            await ch.get_partial_message(message_id).delete()
        except discord.HTTPException:
            pass

    async def run_all(self) -> tuple[int, int]:
        async with self.lock:
            new = await self.poll_notifications()
            count = await self.refresh_status()
            return new, count

    # ------------------------------------------------------------------ loops

    @tasks.loop(minutes=10)
    async def notif_loop(self):
        try:
            async with self.lock:
                new = await self.poll_notifications()
                if new:  # a moon changed state -> update its post right away
                    await self.refresh_status()
        except Exception:
            log.exception("Notification loop failed")

    @tasks.loop(minutes=60)
    async def status_loop(self):
        try:
            async with self.lock:
                await self.refresh_status()
        except Exception:
            log.exception("Status loop failed")

    @notif_loop.before_loop
    @status_loop.before_loop
    async def _wait_ready(self):
        await self.wait_until_ready()


def build_commands(bot: MoonBot) -> app_commands.Group:
    group = app_commands.Group(name="moons", description="Moon tracker",
                               default_permissions=discord.Permissions(manage_guild=True),
                               guild_only=True)

    @group.command(name="setup", description="Choose the status channel and the pop-ping channel")
    @app_commands.describe(status_channel="Channel with one live post per moon",
                           ping_channel="Channel for 'moon popped' announcements",
                           ping_role="Role to ping (leave empty for @here)")
    async def setup(inter: discord.Interaction, status_channel: discord.TextChannel,
                    ping_channel: discord.TextChannel, ping_role: discord.Role | None = None):
        bot.store.set(K_STATUS_CH, str(status_channel.id))
        bot.store.set(K_PING_CH, str(ping_channel.id))
        bot.store.set(K_PING_ROLE, str(ping_role.id) if ping_role else None)
        await inter.response.defer(ephemeral=True, thinking=True)
        try:
            new, count = await bot.run_all()
            msg = f"✅ Status → {status_channel.mention}, pings → {ping_channel.mention} " \
                  f"({ping_role.mention if ping_role else '@here'}). {count} Athanors posted."
        except Exception as e:
            log.exception("Setup refresh failed")
            msg = f"⚠️ Saved, but refresh failed: `{e}`"
        await inter.followup.send(msg, ephemeral=True)

    @group.command(name="refresh", description="Pull fresh data from SeAT and update all posts now")
    async def refresh(inter: discord.Interaction):
        await inter.response.defer(ephemeral=True, thinking=True)
        try:
            new, count = await bot.run_all()
            await inter.followup.send(f"✅ {count} Athanors updated, {new} new moon events.", ephemeral=True)
        except Exception as e:
            log.exception("Manual refresh failed")
            await inter.followup.send(f"⚠️ Refresh failed: `{e}`", ephemeral=True)

    @group.command(name="fuelwarn", description="Days of fuel below which a moon turns red")
    async def fuelwarn(inter: discord.Interaction, days: app_commands.Range[int, 1, 90]):
        bot.store.set(K_FUEL_WARN, str(days))
        await inter.response.send_message(f"✅ Fuel warning at {days} days (yellow below {days * 2}).",
                                          ephemeral=True)

    @group.command(name="config", description="Show the current configuration")
    async def config(inter: discord.Interaction):
        s, p, r = (bot.store.get(k) for k in (K_STATUS_CH, K_PING_CH, K_PING_ROLE))
        await inter.response.send_message(
            f"**Status channel:** {f'<#{s}>' if s else '—'}\n"
            f"**Ping channel:** {f'<#{p}>' if p else '—'}\n"
            f"**Ping:** {f'<@&{r}>' if r else '@here'}\n"
            f"**Fuel warning:** {bot.warn_days()} days\n"
            f"**Corp:** {CORP_ID} · **Notification chars:** {', '.join(map(str, NOTIFY_CHAR_IDS))}\n"
            f"**Intervals:** status {STATUS_INTERVAL_MIN} min, notifications {NOTIF_INTERVAL_MIN} min\n"
            f"**Stored moon events:** {bot.store.event_count()}",
            ephemeral=True)

    @group.command(name="ores", description="Tell the bot which ores a moon has (SeAT drops the ore names)")
    @app_commands.describe(athanor="The Athanor / moon",
                           ores="Ore names, comma separated, any order, e.g. Sylvite, Scheelite, Sperrylite, Chromite")
    async def ores(inter: discord.Interaction, athanor: str, ores: str):
        await inter.response.defer(ephemeral=True, thinking=True)
        if not bot.known_moons:
            async with bot.lock:
                await bot.refresh_status()
        entry = bot.known_moons.get(int(athanor)) if athanor.isdigit() else None
        if not entry or not entry[1].moon_id:
            await inter.followup.send("⚠️ Unknown Athanor, or no extraction seen for it yet.", ephemeral=True)
            return
        name, st = entry
        wanted = [o.strip() for o in ores.split(",") if o.strip()]
        try:
            found = await bot.esi.type_ids(wanted)
        except Exception as e:
            await inter.followup.send(f"⚠️ ESI lookup failed: `{e}`", ephemeral=True)
            return
        missing = [o for o in wanted if o.lower() not in found]
        if missing:
            await inter.followup.send(f"⚠️ Unknown ore name(s): {', '.join(missing)}", ephemeral=True)
            return
        if st.volumes and len(wanted) != len(st.volumes):
            await inter.followup.send(
                f"⚠️ The last extraction of **{name}** has {len(st.volumes)} ores, you gave {len(wanted)}.",
                ephemeral=True)
            return
        type_ids = [found[o.lower()] for o in wanted]
        bot.store.set_moon_ores(st.moon_id, type_ids)
        bot.store.save_names({found[o.lower()]: o for o in wanted})
        async with bot.lock:
            await bot.refresh_status()
        await inter.followup.send(f"✅ **{name}**: {', '.join(wanted)}", ephemeral=True)

    @ores.autocomplete("athanor")
    async def ores_autocomplete(inter: discord.Interaction, current: str):
        return [app_commands.Choice(name=n[:100], value=str(sid))
                for sid, (n, _) in sorted(bot.known_moons.items(), key=lambda kv: kv[1][0])
                if current.lower() in n.lower()][:25]

    @group.command(name="ping", description="Post the pop announcement (with ping) for a moon whose belt is open")
    @app_commands.describe(athanor="The Athanor / moon (💥 = belt open)")
    async def ping(inter: discord.Interaction, athanor: str):
        await inter.response.defer(ephemeral=True, thinking=True)
        if not bot.known_moons:
            async with bot.lock:
                await bot.refresh_status()
        if not athanor.isdigit() or int(athanor) not in bot.known_moons:
            await inter.followup.send("⚠️ Unknown Athanor.", ephemeral=True)
            return
        try:
            error = await bot.announce_current_pop(int(athanor))
        except Exception as e:
            log.exception("Manual ping failed")
            error = f"Ping failed: `{e}`"
        name = bot.known_moons[int(athanor)][0]
        await inter.followup.send(f"⚠️ {error}" if error else f"✅ Pinged the pop of **{name}**.",
                                  ephemeral=True)

    @ping.autocomplete("athanor")
    async def ping_autocomplete(inter: discord.Interaction, current: str):
        items = sorted(bot.known_moons.items(),
                       key=lambda kv: (kv[1][1].phase != L.PHASE_FRACTURED, kv[1][0]))
        return [app_commands.Choice(
                    name=(("💥 " if st.phase == L.PHASE_FRACTURED else "") + n)[:100], value=str(sid))
                for sid, (n, st) in items if current.lower() in n.lower()][:25]

    @group.command(name="testpop", description="Preview a pop announcement (only you see it, no ping)")
    async def testpop(inter: discord.Interaction):
        _, embed = build_pop("Example VII - Moon 3", "Example - Moon Refinery", "Example",
                             ["Sylvite", "Chromite", "Sperrylite", "Scheelite"], "6.94M m³",
                             collector=TAX_COLLECTOR)
        await inter.response.send_message(embed=discord.Embed.from_dict(embed), ephemeral=True)

    return group


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    MoonBot().run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
