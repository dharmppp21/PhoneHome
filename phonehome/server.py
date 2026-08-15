"""PhoneHome -- every server this machine talks to, live.

Run:    python server.py          (from inside the phonehome/ folder)
Check:  python server.py demo

No packet capture, no driver, no admin. psutil.net_connections wraps Windows'
GetExtendedTcpTable, which hands out the remote address *and* the owning PID to an
unelevated process. Hostnames come from the Windows DNS cache, geography from a
one-lookup-per-IP-ever call to ip-api.com.
"""

import asyncio
import logging
import socket
import sys
import threading
import time
import webbrowser
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

import psutil
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from dns_cache import DnsCache
from geo import GeoIP, is_public

STATIC_DIR = Path(__file__).parent / "static"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s"
)
log = logging.getLogger("phonehome")

POLL_SECONDS = 1.0


class Monitor:
    def __init__(self, dns: DnsCache, geo: GeoIP):
        self.dns = dns
        self.geo = geo
        self.active: dict[tuple, dict] = {}
        self.clients: set[WebSocket] = set()
        self.my_location: dict | None = None
        self.started = time.time()
        self.ticks = 0
        self.total_seen = 0
        self._proc_names: dict[int, str] = {}
        # Everything ever seen this session, keyed by (process, ip). Survives the
        # connection closing, which is the whole point of the end-of-session report.
        self.history: dict[tuple[str, str], dict] = {}

    # ---------- collection ----------

    def _process_name(self, pid: int | None) -> str:
        if pid is None:
            return "unknown"
        cached = self._proc_names.get(pid)
        if cached is not None:
            return cached
        try:
            name = psutil.Process(pid).name()
        except (psutil.AccessDenied, psutil.NoSuchProcess, ValueError, OSError):
            # Protected system processes deny access unless elevated. Their traffic
            # still counts, so keep the connection and lose only the label.
            name = "unknown"
        self._proc_names[pid] = name
        return name

    def snapshot(self) -> dict[tuple, dict]:
        """Blocking (~10-50ms). Current established connections to public addresses."""
        out: dict[tuple, dict] = {}
        live_pids: set[int] = set()
        now = time.time()

        for conn in psutil.net_connections(kind="inet"):
            if conn.status != psutil.CONN_ESTABLISHED or not conn.raddr:
                continue
            ip = conn.raddr.ip
            if not is_public(ip):
                continue
            key = (conn.pid, ip, conn.raddr.port)
            if key in out:  # same endpoint over several sockets counts once
                continue
            live_pids.add(conn.pid)
            out[key] = {
                "id": f"{conn.pid}-{ip}-{conn.raddr.port}",
                "pid": conn.pid,
                "process": self._process_name(conn.pid),
                "local_port": conn.laddr.port if conn.laddr else None,
                "remote_ip": ip,
                "remote_port": conn.raddr.port,
                "hostname": self.dns.hostname(ip) or ip,
                "resolved": self.dns.hostname(ip) is not None,
                "geo": self.geo.get(ip),
                "first_seen": now,
                "last_seen": now,
            }

        # PIDs are recycled by Windows. Dropping names for processes that are gone
        # means a reused PID can never inherit a stale label.
        self._proc_names = {p: n for p, n in self._proc_names.items() if p in live_pids}
        return out

    def _enrich(self, rec: dict) -> bool:
        """Fill in hostname/geo that arrived after the connection did."""
        changed = False
        host = self.dns.hostname(rec["remote_ip"])
        if host and host != rec["hostname"]:
            rec["hostname"] = host
            rec["resolved"] = True
            changed = True
        if rec["geo"] is None:
            found = self.geo.get(rec["remote_ip"])
            if found is not None:
                rec["geo"] = found
                changed = True
        return changed

    def _remember(self, rec: dict, now: float) -> None:
        key = (rec["process"], rec["remote_ip"])
        entry = self.history.get(key)
        if entry is None:
            # Hostname and geo are looked up fresh at report time rather than stored
            # here, so a name that arrives ten minutes later still shows up.
            self.history[key] = {
                "process": rec["process"],
                "remote_ip": rec["remote_ip"],
                "count": 1,
                "first_seen": now,
                "last_seen": now,
            }
        else:
            entry["count"] += 1
            entry["last_seen"] = now

    def report(self) -> dict:
        """Everything seen this session, summarised."""
        by_process_dests: dict[str, set] = defaultdict(set)
        by_process_conns: Counter = Counter()
        by_dest: dict[str, dict] = {}
        all_addresses: set[str] = set()
        countries: Counter = Counter()

        for entry in self.history.values():
            ip, proc = entry["remote_ip"], entry["process"]
            by_process_dests[proc].add(ip)
            by_process_conns[proc] += entry["count"]
            all_addresses.add(ip)

            # Group on the hostname, not the address. A name with several A/AAAA
            # records is one destination to a reader, and grouping by IP printed
            # "update.googleapis.com" as two separate rows in the same table.
            host = self.dns.hostname(ip)
            row = by_dest.setdefault(
                host or ip,
                {
                    "hostname": host or ip,
                    "resolved": host is not None,
                    "geo": None,
                    "count": 0,
                    "addresses": set(),
                    "processes": set(),
                    "first_seen": entry["first_seen"],
                },
            )
            row["count"] += entry["count"]
            row["addresses"].add(ip)
            row["processes"].add(proc)
            row["first_seen"] = min(row["first_seen"], entry["first_seen"])
            # Any one of the addresses will do for placing it on the map; they are
            # the same service, and the first located one wins.
            if row["geo"] is None:
                row["geo"] = self.geo.get(ip)

        for row in by_dest.values():
            if row["geo"] and row["geo"].get("country"):
                countries[row["geo"]["country"]] += 1

        def flatten(row: dict) -> dict:
            out = dict(row)
            out["processes"] = sorted(row["processes"])
            out["addresses"] = len(row["addresses"])
            out["country"] = (row["geo"] or {}).get("country", "")
            out["city"] = (row["geo"] or {}).get("city", "")
            out["isp"] = (row["geo"] or {}).get("isp", "")
            out.pop("geo", None)
            return out

        rows = [flatten(r) for r in by_dest.values()]
        resolved = sum(1 for r in rows if r["resolved"])

        return {
            "session": {
                "started": self.started,
                "duration_seconds": round(time.time() - self.started, 1),
                "connections_seen": self.total_seen,
                "distinct_destinations": len(by_dest),
                "distinct_addresses": len(all_addresses),
                "distinct_processes": len(by_process_dests),
                "countries": len(countries),
                "resolved_destinations": resolved,
                "unresolved_destinations": len(rows) - resolved,
            },
            "top_processes": sorted(
                (
                    {
                        "process": proc,
                        "destinations": len(dests),
                        "connections": by_process_conns[proc],
                    }
                    for proc, dests in by_process_dests.items()
                ),
                key=lambda r: (-r["destinations"], -r["connections"]),
            ),
            "top_destinations": sorted(rows, key=lambda r: -r["count"])[:25],
            "countries": [
                {"country": c, "destinations": n} for c, n in countries.most_common()
            ],
            # Contacted exactly once and never again -- the interesting tail, where
            # one-shot telemetry and update pings live.
            "one_offs": sorted(
                (r for r in rows if r["count"] == 1), key=lambda r: r["first_seen"]
            ),
        }

    # ---------- streaming ----------

    async def broadcast(self, message: dict) -> None:
        for ws in list(self.clients):
            try:
                await ws.send_json(message)
            except Exception:
                self.clients.discard(ws)

    def snapshot_message(self) -> dict:
        return {
            "type": "snapshot",
            "self": self.my_location,
            "connections": list(self.active.values()),
        }

    async def run(self) -> None:
        while True:
            try:
                current = await asyncio.to_thread(self.snapshot)
            except Exception as exc:
                log.warning("poll failed: %s", exc)
                await asyncio.sleep(POLL_SECONDS)
                continue

            now = time.time()
            new, updated, closed = [], [], []

            for key, fresh in current.items():
                rec = self.active.get(key)
                if rec is None:
                    self.geo.enqueue(fresh["remote_ip"])
                    self.active[key] = fresh
                    self.total_seen += 1
                    self._remember(fresh, now)
                    new.append(fresh)
                else:
                    rec["last_seen"] = now
                    if self._enrich(rec):
                        updated.append(rec)

            for key in [k for k in self.active if k not in current]:
                closed.append(self.active.pop(key)["id"])

            self.ticks += 1
            if new or updated or closed:
                await self.broadcast(
                    {"type": "delta", "new": new, "updated": updated, "closed": closed}
                )
            await asyncio.sleep(POLL_SECONDS)


dns = DnsCache()
geo = GeoIP()
monitor = Monitor(dns, geo)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    async def locate_self():
        monitor.my_location = await geo.my_location()
        if monitor.my_location:
            log.info(
                "you appear to be in %s, %s",
                monitor.my_location["city"] or "?",
                monitor.my_location["country"] or "?",
            )
            # The browser is opened at startup and usually connects before this
            # resolves, so its snapshot carried self:null. Push the answer when it
            # arrives -- otherwise that client draws no arcs at all, all session.
            await monitor.broadcast({"type": "self", "self": monitor.my_location})

    tasks = [
        asyncio.create_task(dns.run(), name="dns"),
        asyncio.create_task(geo.run(), name="geo"),
        asyncio.create_task(monitor.run(), name="monitor"),
        asyncio.create_task(locate_self(), name="locate"),
    ]
    log.info("watching (geo via %s)", geo.provider)
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="PhoneHome", lifespan=lifespan)


@app.get("/api/connections")
async def connections():
    return sorted(monitor.active.values(), key=lambda r: (r["process"].lower(), r["hostname"]))


@app.get("/api/health")
async def health():
    countries = {
        r["geo"]["country"] for r in monitor.active.values() if r["geo"] and r["geo"]["country"]
    }
    return {
        "uptime_seconds": round(time.time() - monitor.started, 1),
        "polls": monitor.ticks,
        "active_connections": len(monitor.active),
        "connections_seen_total": monitor.total_seen,
        "distinct_destinations": len({r["remote_ip"] for r in monitor.active.values()}),
        "countries_now": sorted(countries),
        "processes_now": sorted({r["process"] for r in monitor.active.values()}),
        "dns_entries": len(dns.map),
        "dns_refreshes": dns.refreshes,
        "geo_cached_ips": len(geo.mem),
        "geo_pending": len(geo.pending),
        "geo_lookups_this_session": geo.lookups,
        "geo_provider": geo.provider,
        "websocket_clients": len(monitor.clients),
        "my_location": monitor.my_location,
    }


@app.get("/api/report")
async def report():
    return monitor.report()


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    monitor.clients.add(websocket)
    try:
        await websocket.send_json(monitor.snapshot_message())
        while True:
            await websocket.receive_text()  # client never sends; this waits for close
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.debug("websocket dropped: %s", exc)
    finally:
        monitor.clients.discard(websocket)


# ---------- self-check ----------


def demo() -> None:
    """Assert-based check of the parts that can be wrong without being obvious."""
    import dns_cache

    # 1. public/private classification -- the filter the whole tool depends on
    private = [
        "127.0.0.1",        # loopback
        "10.0.0.5",         # RFC1918
        "192.168.1.1",      # RFC1918
        "172.16.0.1",       # RFC1918
        "169.254.1.1",      # link-local
        "100.64.0.1",       # carrier-grade NAT -- is_private says False, watch out
        "198.18.0.1",       # benchmarking
        "192.0.2.1",        # TEST-NET-1
        "240.0.0.1",        # reserved for future use
        "255.255.255.255",  # broadcast
        "224.0.0.1",        # multicast -- is_global says True, watch out
        "ff02::1",          # IPv6 multicast
        "0.0.0.0",
        "::1",
        "fe80::1",
        "fd00::1",          # IPv6 unique-local
        "not-an-ip",
    ]
    public = ["8.8.8.8", "1.1.1.1", "157.240.1.35", "2606:4700:4700::1111"]
    for ip in private:
        assert not is_public(ip), f"{ip} should be filtered out"
    for ip in public:
        assert is_public(ip), f"{ip} should be kept"
    print(f"  ok  is_public rejects {len(private)}, keeps {len(public)}")

    # 2. no private address can reach a live snapshot
    snap = monitor.snapshot()
    for rec in snap.values():
        assert is_public(rec["remote_ip"]), f"private IP leaked: {rec['remote_ip']}"
    keys = list(snap.keys())
    assert len(keys) == len(set(keys)), "duplicate (pid, ip, port) keys"
    print(f"  ok  live snapshot: {len(snap)} connections, all public, no duplicates")

    # 3. DNS parsing, including the CNAME rows that carry a name where an IP goes
    parsed = dns_cache.parse(
        '[{"Entry":"example.com","Data":"93.184.216.34"},'
        '{"Entry":"cdn.example.com","Data":"example.com"},'
        '{"Entry":"v6.example.com","Data":"2606:2800:220:1:248:1893:25c8:1946"},'
        '{"Entry":"","Data":"1.2.3.4"}]'
    )
    assert parsed == {
        "93.184.216.34": "example.com",
        "2606:2800:220:1:248:1893:25c8:1946": "v6.example.com",
    }, parsed
    assert dns_cache.parse("") == {}
    logging.getLogger("dns_cache").setLevel(logging.ERROR)  # the next line logs on purpose
    assert dns_cache.parse("not json") == {}
    logging.getLogger("dns_cache").setLevel(logging.NOTSET)
    assert dns_cache.parse('{"Entry":"solo.com","Data":"5.6.7.8"}') == {"5.6.7.8": "solo.com"}
    print("  ok  DNS parser: skips CNAMEs, blanks, junk; handles single-row JSON")

    # 4. geo cache survives a reopen and remembers failures without retrying
    import tempfile
    from pathlib import Path as _Path

    with tempfile.TemporaryDirectory() as tmp:
        path = _Path(tmp) / "t.sqlite3"
        g = GeoIP(path)
        # both providers must normalise to the same shape, or the cache would hold
        # two different record layouts depending on which one answered
        from geo import from_ipapi, from_ipwho

        a = from_ipapi({"status": "success", "lat": 37.4, "lon": -122.0,
                        "city": "Mountain View", "country": "United States",
                        "countryCode": "US", "isp": "Google LLC"})
        b = from_ipwho({"success": True, "latitude": 37.4, "longitude": -122.0,
                        "city": "Mountain View", "country": "United States",
                        "country_code": "US", "connection": {"isp": "Google LLC"}})
        assert a == b, f"providers disagree:\n{a}\n{b}"
        assert from_ipapi({"status": "fail"}) is None
        assert from_ipwho({"success": False}) is None

        g.store({"8.8.8.8": a, "0.0.0.1": None})
        g.db.close()
        again = GeoIP(path)
        assert again.get("8.8.8.8")["city"] == "Mountain View"
        assert again.get("0.0.0.1") is None
        again.enqueue("8.8.8.8")   # already known
        again.enqueue("0.0.0.1")   # known failure
        again.enqueue("192.168.1.1")  # private
        assert again.pending == set(), again.pending
        again.enqueue("9.9.9.9")
        assert again.pending == {"9.9.9.9"}
        assert not again.primary_dead, "a fresh cache must try the primary"
        again.db.close()
        print("  ok  geo cache: persists, negative-caches, never re-queues known IPs")
        print("  ok  both geo providers normalise to an identical record")

        # the failover verdict must outlive the process, or a blackholed primary
        # costs a full strike-out on every launch
        import geo as geo_mod

        def reopened_with(failed_at: str) -> GeoIP:
            g2 = GeoIP(path)
            g2._meta_set("primary_failed_at", failed_at)
            g2.db.close()
            return GeoIP(path)

        recent = reopened_with(str(time.time()))
        assert recent.primary_dead, "a recent failure must skip the primary"
        assert recent.provider == geo_mod.FALLBACK_NAME
        recent.db.close()

        stale = reopened_with(str(time.time() - geo_mod.PRIMARY_RETRY_AFTER - 60))
        assert not stale.primary_dead, "an old failure must be re-probed"
        assert stale.provider == geo_mod.PRIMARY_NAME
        stale.db.close()

        for junk in ("", "not-a-number"):
            g3 = reopened_with(junk)
            assert not g3.primary_dead, f"junk marker {junk!r} must not disable the primary"
            g3.db.close()
    print("  ok  failover verdict persists, expires, and survives junk markers")

    # 5. real DNS cache read -- proves the PowerShell path works on this machine
    try:
        found = dns.refresh()
        print(f"  ok  Get-DnsClientCache returned {found} usable rows")
    except Exception as exc:
        print(f"  WARN Get-DnsClientCache unavailable ({exc}) -- will show bare IPs")

    print("\nall checks passed")


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "demo":
        demo()
        return

    port = 8000
    if "--port" in argv:
        i = argv.index("--port") + 1
        if i >= len(argv) or not argv[i].isdigit():
            sys.exit("--port needs a number, e.g. --port 8010")
        port = int(argv[i])
    url = f"http://127.0.0.1:{port}/"

    # Claim the port before promising a URL. Otherwise a port that's already taken
    # prints a working-looking link, opens a browser at a dead tab, and buries the
    # real cause in a uvicorn traceback.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        sys.exit(
            f"\n  Port {port} is already in use.\n"
            f"  Another PhoneHome is probably still running — close it, or pick\n"
            f"  another port:  python -m phonehome --port {port + 10}\n"
        )
    finally:
        probe.close()

    if "--no-browser" not in argv:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    print(f"\n  PhoneHome -> {url}\n")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
