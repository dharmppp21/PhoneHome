"""Where an IP is, cached permanently in SQLite.

Two providers, both free and key-free:

  * **ip-api.com** — primary. Batches 100 IPs per request, so a whole session is
    usually one call. But it is **HTTP-only** (https costs money), and some ISPs
    blackhole it — observed failing on an Airtel connection in India while the
    rest of the internet was fine.
  * **ipwho.is** — fallback. HTTPS, one IP per request, slower, but reachable.

Every IP is looked up exactly once ever, so a normal session makes very few calls.
That matters for the rate limits, and it matters for privacy: this is the only
point where anything leaves the machine.
"""

import asyncio
import ipaddress
import logging
import sqlite3
import threading
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

PRIMARY_NAME = "ip-api.com"
BATCH_URL = "http://ip-api.com/batch"
BATCH_SELF_URL = "http://ip-api.com/json/"
FIELDS = "status,message,country,countryCode,city,lat,lon,isp,query"

FALLBACK_NAME = "ipwho.is"
FALLBACK_URL = "https://ipwho.is/{ip}"
FALLBACK_SELF_URL = "https://ipwho.is/"

MAX_PER_BATCH = 100
# ip-api throttles /batch harder than single lookups. 15/min x 100 IPs is far past
# anything a desktop generates, so there's nothing to gain by crowding the ceiling.
BATCH_PER_MIN = 15
SINGLE_PER_MIN = 45
# Two strikes and we stop waiting on a host that may be blackholed for this whole
# session. Short timeout so that verdict arrives in seconds, not minutes.
PRIMARY_STRIKES = 2
TIMEOUT = 5.0
# The verdict is remembered on disk. Without this, a network that blackholes
# ip-api pays the full strike-out cost (~16s of dead air with no geography) on
# every single launch. Re-probed after this long in case the block was temporary.
PRIMARY_RETRY_AFTER = 6 * 3600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS geo (
    ip           TEXT PRIMARY KEY,
    ok           INTEGER NOT NULL,
    lat          REAL,
    lon          REAL,
    city         TEXT,
    country      TEXT,
    country_code TEXT,
    isp          TEXT,
    fetched_at   REAL NOT NULL
)
"""

_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)
"""


def is_public(ip: str) -> bool:
    """True for addresses that belong to somebody else on the internet.

    `is_global` is the one to trust: it means "not in the IANA special-purpose
    registry", which already covers loopback, link-local, benchmark, TEST-NET,
    broadcast and future-use ranges in one go.

    Two traps this deliberately avoids:
      * `is_private` is **False** for 100.64.0.0/10 (carrier-grade NAT), so a stack
        of is_private/is_loopback/... checks lets your ISP's internal addresses
        through. `is_global` is False for it, correctly.
      * `is_global` is **True** for multicast, so it needs the extra guard.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_global and not addr.is_multicast


def from_ipapi(row: dict) -> dict | None:
    """ip-api row -> our shape, or None when it could not place the address."""
    if row.get("status") != "success":
        return None
    return {
        "lat": row.get("lat"),
        "lon": row.get("lon"),
        "city": row.get("city") or "",
        "country": row.get("country") or "",
        "country_code": row.get("countryCode") or "",
        "isp": row.get("isp") or "",
    }


def from_ipwho(row: dict) -> dict | None:
    """ipwho.is row -> our shape. Different field names, same information."""
    if not row.get("success"):
        return None
    conn = row.get("connection") or {}
    return {
        "lat": row.get("latitude"),
        "lon": row.get("longitude"),
        "city": row.get("city") or "",
        "country": row.get("country") or "",
        "country_code": row.get("country_code") or "",
        "isp": conn.get("isp") or conn.get("org") or "",
    }


class _RateLimiter:
    """Spaces calls out to `per_minute`. One in flight at a time, so no lock."""

    def __init__(self, per_minute: int):
        self.interval = 60.0 / per_minute
        self._next_at = 0.0

    async def wait(self) -> None:
        now = time.monotonic()
        if now < self._next_at:
            await asyncio.sleep(self._next_at - now)
        self._next_at = max(now, self._next_at) + self.interval


class GeoIP:
    def __init__(self, db_path: str | Path | None = None):
        path = Path(db_path or Path(__file__).parent / "geoip.sqlite3")
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute(_SCHEMA)
        self.db.execute(_META_SCHEMA)
        self.db.commit()
        self.lock = threading.Lock()
        # The whole cache lives in RAM: a few thousand rows at most, and it turns
        # get() into a dict hit on the hot path. None means "looked up, no result".
        self.mem: dict[str, dict | None] = self._load_all()
        self.pending: set[str] = set()
        self.lookups = 0

        self._strikes = 0
        self.provider = PRIMARY_NAME
        failed_ago = self._primary_failed_ago()
        if failed_ago is not None and failed_ago < PRIMARY_RETRY_AFTER:
            self._strikes = PRIMARY_STRIKES
            self.provider = FALLBACK_NAME
            log.info(
                "%s failed %.0f min ago -- starting on %s, will re-probe in %.1f h",
                PRIMARY_NAME,
                failed_ago / 60,
                FALLBACK_NAME,
                (PRIMARY_RETRY_AFTER - failed_ago) / 3600,
            )
        log.info("geoip cache: %d known IPs", len(self.mem))

    @property
    def primary_dead(self) -> bool:
        return self._strikes >= PRIMARY_STRIKES

    def _meta_get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _meta_set(self, key: str, value: str) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))
            self.db.commit()

    def _primary_failed_ago(self) -> float | None:
        """Seconds since the primary was last written off, or None if never."""
        raw = self._meta_get("primary_failed_at")
        if not raw:
            return None
        try:
            return max(0.0, time.time() - float(raw))
        except ValueError:
            return None

    def _load_all(self) -> dict[str, dict | None]:
        out: dict[str, dict | None] = {}
        for row in self.db.execute(
            "SELECT ip, ok, lat, lon, city, country, country_code, isp FROM geo"
        ):
            ip, ok = row[0], row[1]
            out[ip] = (
                {
                    "lat": row[2], "lon": row[3], "city": row[4],
                    "country": row[5], "country_code": row[6], "isp": row[7],
                }
                if ok
                else None
            )
        return out

    def get(self, ip: str) -> dict | None:
        return self.mem.get(ip)

    def enqueue(self, ip: str) -> None:
        if ip in self.mem or ip in self.pending or not is_public(ip):
            return
        self.pending.add(ip)

    def store(self, found: dict[str, dict | None]) -> None:
        """Commit {ip: record-or-None} to memory and disk."""
        now = time.time()
        with self.lock:
            for ip, rec in found.items():
                self.mem[ip] = rec
                self.db.execute(
                    "INSERT OR REPLACE INTO geo VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        ip,
                        1 if rec else 0,
                        rec["lat"] if rec else None,
                        rec["lon"] if rec else None,
                        rec["city"] if rec else None,
                        rec["country"] if rec else None,
                        rec["country_code"] if rec else None,
                        rec["isp"] if rec else None,
                        now,
                    ),
                )
            self.db.commit()

    # ---------- providers ----------

    async def _batch(self, client: httpx.AsyncClient, ips: list[str]) -> dict[str, dict | None]:
        resp = await client.post(BATCH_URL, params={"fields": FIELDS}, json=ips)
        resp.raise_for_status()
        rows = resp.json()
        rows = rows if isinstance(rows, list) else [rows]
        return {r["query"]: from_ipapi(r) for r in rows if r.get("query")}

    async def _single(self, client: httpx.AsyncClient, ip: str) -> dict | None:
        resp = await client.get(FALLBACK_URL.format(ip=ip))
        resp.raise_for_status()
        return from_ipwho(resp.json())

    async def my_location(self) -> dict | None:
        """Where this machine appears to be. Arcs need somewhere to start."""
        # Skip a host already known to be blackholed: this runs at startup, and
        # waiting out its timeout delays the globe's centre and every arc with it.
        sources = [(FALLBACK_SELF_URL, from_ipwho, None)]
        if not self.primary_dead:
            sources.insert(0, (BATCH_SELF_URL, from_ipapi, {"fields": FIELDS}))

        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            for url, parse, params in sources:
                try:
                    resp = await client.get(url, params=params)
                    resp.raise_for_status()
                    row = resp.json()
                except Exception as exc:
                    log.debug("self-location via %s failed: %s", url, exc)
                    continue
                rec = parse(row)
                if rec:
                    rec["ip"] = row.get("query") or row.get("ip") or ""
                    return rec
        log.warning("could not determine own location; arcs will not be drawn")
        return None

    async def run(self) -> None:
        batch_limiter = _RateLimiter(BATCH_PER_MIN)
        single_limiter = _RateLimiter(SINGLE_PER_MIN)

        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            while True:
                if not self.pending:
                    await asyncio.sleep(0.5)
                    continue

                if not self.primary_dead:
                    ips = [self.pending.pop() for _ in range(min(MAX_PER_BATCH, len(self.pending)))]
                    await batch_limiter.wait()
                    try:
                        found = await self._batch(client, ips)
                    except Exception as exc:
                        # Transport failure is not the IP's fault -- requeue rather
                        # than poisoning the cache with a permanent negative result.
                        self.pending.update(ips)
                        self._strikes += 1
                        if self.primary_dead:
                            self.provider = FALLBACK_NAME
                            self._meta_set("primary_failed_at", str(time.time()))
                            log.warning(
                                "%s unreachable (%s) -- switching to %s and "
                                "remembering that for %d h",
                                PRIMARY_NAME,
                                exc or type(exc).__name__,
                                FALLBACK_NAME,
                                PRIMARY_RETRY_AFTER // 3600,
                            )
                        await asyncio.sleep(2)
                        continue
                    self.store(found)
                    self.lookups += len(found)
                    # It answered, so clear any past verdict against it.
                    if self._meta_get("primary_failed_at"):
                        self._meta_set("primary_failed_at", "")
                    log.info("geoip: resolved %d IPs (%d cached)", len(found), len(self.mem))
                else:
                    ip = self.pending.pop()
                    await single_limiter.wait()
                    try:
                        rec = await self._single(client, ip)
                    except Exception as exc:
                        log.warning("geoip lookup failed for %s: %s", ip, exc)
                        self.pending.add(ip)
                        await asyncio.sleep(3)
                        continue
                    self.store({ip: rec})
                    self.lookups += 1
