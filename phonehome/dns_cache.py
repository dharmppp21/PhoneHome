"""Hostnames for IPs, taken from Windows' own DNS cache.

Windows already remembers which hostname resolved to which IP. Asking it is free
and instant; reverse DNS (PTR) lookups are slow and usually return nothing useful
for CDN and cloud addresses. So we read `Get-DnsClientCache` instead of resolving
anything ourselves.
"""

import asyncio
import ipaddress
import json
import logging
import subprocess
import time

log = logging.getLogger(__name__)

# ConvertTo-Json instead of parsing the table layout: PowerShell's default text
# output truncates long hostnames with "..." and shifts columns by console width.
_PS = (
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
    "Get-DnsClientCache | Select-Object Entry,Data | ConvertTo-Json -Compress"
)


def _run_powershell(timeout: float = 20.0) -> str:
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS],
        capture_output=True,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return proc.stdout.decode("utf-8", errors="replace")


def parse(raw: str) -> dict[str, str]:
    """Cache dump -> {ip: hostname}.

    CNAME rows carry another hostname in Data rather than an address, so we keep
    only rows whose Data actually parses as an IP. That doubles as the record-type
    filter and needs no knowledge of DNS type numbers.
    """
    raw = raw.strip()
    if not raw:
        return {}
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("could not parse Get-DnsClientCache output")
        return {}
    if isinstance(rows, dict):  # a single entry does not come back as a list
        rows = [rows]

    out: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ip = (row.get("Data") or "").strip()
        name = (row.get("Entry") or "").strip().rstrip(".")
        if not ip or not name:
            continue
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            continue
        out.setdefault(ip, name)  # first name wins when several map to one IP
    return out


class DnsCache:
    """Accumulating IP -> hostname map, refreshed from Windows every `interval`."""

    def __init__(self, interval: float = 10.0):
        self.map: dict[str, str] = {}
        self.interval = interval
        self.last_refresh: float | None = None
        self.refreshes = 0
        self._warned = False

    def hostname(self, ip: str) -> str | None:
        return self.map.get(ip)

    def refresh(self) -> int:
        """Blocking. Merges the current cache in. Returns rows seen this time."""
        found = parse(_run_powershell())
        # Merge, never replace. Windows evicts entries on TTL expiry; dropping them
        # here would make hostnames flicker back to bare IPs mid-session.
        self.map.update(found)
        self.last_refresh = time.time()
        self.refreshes += 1
        return len(found)

    async def run(self) -> None:
        while True:
            try:
                n = await asyncio.to_thread(self.refresh)
                log.debug("dns cache: %d rows, %d known", n, len(self.map))
            except Exception as exc:  # service disabled, PS missing, timeout
                if not self._warned:
                    log.warning("DNS cache unavailable, showing bare IPs (%s)", exc)
                    self._warned = True
            await asyncio.sleep(self.interval)
