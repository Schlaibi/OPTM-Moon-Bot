"""HTTP clients: SeAT API (authenticated) and public ESI (names only)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp

from .logic import MOON_EVENT_TYPES, Extraction, parse_ts


def _norm(ts: str) -> str:
    return parse_ts(ts).isoformat()

log = logging.getLogger(__name__)

USER_AGENT = "MoonBot/1.0"
ESI_BASE = "https://esi.evetech.net/latest"
MAX_PAGES = 100


class SeatClient:
    def __init__(self, session: aiohttp.ClientSession, base_url: str, token: str):
        self.s = session
        self.base = base_url.rstrip("/")
        self.headers = {"X-Token": token, "Accept": "application/json", "User-Agent": USER_AGENT}

    async def _get_all(self, path: str, params: dict | None = None) -> list[dict]:
        """GET a paginated SeAT v2 resource and follow links.next."""
        url: str | None = f"{self.base}{path}"
        out: list[dict] = []
        for _ in range(MAX_PAGES):
            if url is None:
                break
            async with self.s.get(url, headers=self.headers, params=params) as r:
                if r.status >= 400:
                    body = (await r.text())[:300]
                    raise RuntimeError(f"SeAT {r.status} on {url}: {body}")
                data = await r.json()
            if isinstance(data, list):
                return out + data
            out += data.get("data") or []
            url = (data.get("links") or {}).get("next")
            params = None  # next link already carries the query string
        return out

    async def structures(self, corporation_id: int) -> list[dict]:
        return await self._get_all(f"/api/v2/corporation/structures/{corporation_id}")

    async def _page(self, url: str, page: int) -> dict:
        async with self.s.get(url, headers=self.headers, params={"page": page}) as r:
            if r.status >= 400:
                raise RuntimeError(f"SeAT {r.status} on {url} page {page}: {(await r.text())[:300]}")
            return await r.json()

    async def moon_notifications(self, character_id: int, since: datetime | None = None) -> list[dict]:
        """Moon-mining notifications of one character.

        SeAT ignores $filter and page size here (always 15 per page, oldest pages first),
        so we walk pages from the last one backwards. With `since` we stop once a whole
        page is older than that; without it we read everything (first run).
        """
        url = f"{self.base}/api/v2/character/notifications/{character_id}"
        first = await self._page(url, 1)
        last_page = int((first.get("meta") or {}).get("last_page") or 1)
        rows: list[dict] = list(first.get("data") or [])
        cutoff = since.isoformat() if since else None

        page = last_page
        while page > 1:
            batch = list(range(page, max(1, page - 10), -1))  # 10 pages concurrently
            results = await asyncio.gather(*(self._page(url, p) for p in batch))
            stop = False
            for res in results:
                data = res.get("data") or []
                rows += data
                if cutoff and data and max(_norm(n["timestamp"]) for n in data) < cutoff:
                    stop = True
            if stop:
                break
            page = batch[-1] - 1
        return [n for n in rows if n.get("type") in MOON_EVENT_TYPES]


class SeatDb:
    """Read-only access to SeAT's MariaDB: moon notifications and extractions.

    The SeAT API drops the ore type ids from oreVolumeByType; the raw YAML stored
    in `character_notifications.text` still has them. Extraction times come from
    `corporation_industry_mining_extractions`. Only SELECTs are issued.
    """

    def __init__(self, host: str, port: int, database: str, user: str, password: str):
        self.params = dict(host=host, port=port, database=database, user=user,
                           password=password, charset="utf8mb4", connect_timeout=10,
                           read_timeout=60, autocommit=True)

    def _query(self, character_ids: list[int], since: datetime | None) -> list[dict]:
        import pymysql  # optional dependency, only needed with SEAT_DB_HOST
        sql = ("SELECT type, timestamp, text FROM character_notifications "
               f"WHERE character_id IN ({','.join(['%s'] * len(character_ids))}) "
               f"AND type IN ({','.join(['%s'] * len(MOON_EVENT_TYPES))})")
        args: list = [*character_ids, *MOON_EVENT_TYPES]
        if since is not None:
            sql += " AND timestamp >= %s"
            args.append(since.strftime("%Y-%m-%d %H:%M:%S"))
        conn = pymysql.connect(**self.params, cursorclass=pymysql.cursors.DictCursor)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                return list(cur.fetchall())
        finally:
            conn.close()

    async def moon_notifications(self, character_ids: list[int], since: datetime | None = None) -> list[dict]:
        return await asyncio.to_thread(self._query, character_ids, since)

    def _select(self, sql: str, args: list) -> list[dict]:
        import pymysql
        conn = pymysql.connect(**self.params, cursorclass=pymysql.cursors.DictCursor)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                return list(cur.fetchall())
        finally:
            conn.close()

    async def extractions(self, corp_ids: list[int], since: datetime) -> list[Extraction]:
        """Moon extractions SeAT pulled from ESI (needs a Director of that corp in SeAT).
        Only extractions whose autofracture is after `since`."""
        if not corp_ids:
            return []
        sql = ("SELECT corporation_id, structure_id, moon_id, extraction_start_time, "
               "chunk_arrival_time, natural_decay_time FROM corporation_industry_mining_extractions "
               f"WHERE corporation_id IN ({','.join(['%s'] * len(corp_ids))}) "
               "AND natural_decay_time >= %s")
        rows = await asyncio.to_thread(self._select, sql,
                                       [*corp_ids, since.strftime("%Y-%m-%d %H:%M:%S")])
        utc = lambda d: d.replace(tzinfo=timezone.utc) if d else None  # noqa: E731
        return [Extraction(int(r["corporation_id"]), int(r["structure_id"]),
                           int(r["moon_id"]) if r["moon_id"] else None,
                           utc(r["extraction_start_time"]), utc(r["chunk_arrival_time"]),
                           utc(r["natural_decay_time"])) for r in rows]

    async def refine_yields(self, base_names) -> dict[int, tuple[int, list[tuple[int, int]]]]:
        """Reprocessing yields from SeAT's SDE copy for all ore types whose name ends in
        one of `base_names` (incl. variants): type id -> (portion size, [(material, qty)])."""
        names = list(base_names)
        sql = ("SELECT m.typeID AS tid, m.materialTypeID AS mat, m.quantity AS qty, "
               "t.portionSize AS portion FROM invTypeMaterials m "
               "JOIN invTypes t ON t.typeID = m.typeID WHERE "
               + " OR ".join(["t.typeName LIKE %s"] * len(names)))
        rows = await asyncio.to_thread(self._select, sql, [f"%{n}" for n in names])
        out: dict[int, tuple[int, list[tuple[int, int]]]] = {}
        for r in rows:
            portion, mats = out.setdefault(int(r["tid"]), (int(r["portion"] or 1), []))
            mats.append((int(r["mat"]), int(r["qty"])))
        return out

    async def structure_info(self, structure_ids: list[int]) -> dict[int, dict]:
        """Name / type / system of structures SeAT knows (universe_structures). Best effort."""
        if not structure_ids:
            return {}
        sql = (f"SELECT * FROM universe_structures WHERE structure_id IN "
               f"({','.join(['%s'] * len(structure_ids))})")
        try:
            rows = await asyncio.to_thread(self._select, sql, list(structure_ids))
        except Exception as e:
            log.info("universe_structures not readable (%s)", e)
            return {}
        return {int(r["structure_id"]): r for r in rows}


class EsiClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.s = session

    async def type_ids(self, names: list[str]) -> dict[str, int]:
        """Resolve item names (e.g. 'Sylvite') to type ids. Keys are lower-cased names."""
        async with self.s.post(f"{ESI_BASE}/universe/ids/", json=names,
                               headers={"User-Agent": USER_AGENT}) as r:
            r.raise_for_status()
            data = await r.json()
        return {row["name"].lower(): int(row["id"]) for row in data.get("inventory_types") or []}

    async def prices(self) -> dict[int, float]:
        """Public ESI market prices: type id -> average price (adjusted as fallback)."""
        async with self.s.get(f"{ESI_BASE}/markets/prices/", headers={"User-Agent": USER_AGENT}) as r:
            r.raise_for_status()
            data = await r.json()
        return {int(p["type_id"]): float(p.get("average_price") or p.get("adjusted_price") or 0)
                for p in data if p.get("average_price") or p.get("adjusted_price")}

    async def _moon_name(self, moon_id: int) -> dict[int, str]:
        async with self.s.get(f"{ESI_BASE}/universe/moons/{moon_id}/",
                              headers={"User-Agent": USER_AGENT}) as r:
            if r.status != 200:
                return {}
            return {moon_id: (await r.json())["name"]}

    async def names(self, ids: list[int]) -> dict[int, str]:
        """Resolve moon / type / system ids via the public /universe/names/ endpoint."""
        result: dict[int, str] = {}
        ids = sorted(set(i for i in ids if i))
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            async with self.s.post(f"{ESI_BASE}/universe/names/", json=chunk,
                                   headers={"User-Agent": USER_AGENT}) as r:
                if r.status == 404:  # moons (or one bad id) fail the whole batch -> one by one
                    if len(chunk) > 1:
                        for one in chunk:
                            result.update(await self.names([one]))
                    else:
                        result.update(await self._moon_name(chunk[0]))
                    continue
                r.raise_for_status()
                for row in await r.json():
                    result[int(row["id"])] = row["name"]
        return result
