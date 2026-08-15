# PhoneHome — how to test it yourself

Everything runs from the repository root unless noted.

> **PowerShell gotcha, applies to every command below.** `Invoke-RestMethod` hands
> a JSON array to the pipeline as a **single object**, so `... | Select-Object`
> silently produces one row of arrays instead of many rows — or nothing at all.
> **Always assign to a variable first, then pipe it.** Every command here does that.
> Related: `Format-Table -AutoSize` prints *nothing* when the columns are wider than
> the console; it does not wrap or truncate.

## One-time setup

```powershell
winget install Python.Python.3.12
```

Reopen the terminal so `python` lands on PATH, then:

```powershell
cd PhoneHome
python -m pip install -r requirements.txt
```

---

# Day 1 — connections and names

### 1. Self-check (no server needed)

```powershell
python -m phonehome demo
```

Seven `ok` lines and `all checks passed`. Verifies public/private filtering, that a
live snapshot holds no private addresses and no duplicate keys, that the DNS parser
skips CNAME rows and junk, that the geo cache persists and negative-caches, that
both geo providers normalise to an identical record, that the failover verdict
persists and expires, and that `Get-DnsClientCache` works here.

### 2. Start the server

```powershell
python -m phonehome
```

Opens your browser automatically. Add `--no-browser` to suppress that.

Leave it running; use a second terminal below.

### 3. See what your machine is talking to

```powershell
$c = Invoke-RestMethod http://127.0.0.1:8000/api/connections; $c | Format-Table process, hostname, remote_port
```

### 4. Only the ones DNS could name

```powershell
$c = Invoke-RestMethod http://127.0.0.1:8000/api/connections; $c | Where-Object resolved | Format-Table process, hostname
```

Real hostnames next to the process responsible. Expect roughly a third of rows —
see the DNS-over-HTTPS note in Troubleshooting.

### 5. Prove the private-IP filter works on live data

```powershell
$c = Invoke-RestMethod http://127.0.0.1:8000/api/connections; ($c | Where-Object { $_.remote_ip -match '^(10\.|127\.|192\.168\.|169\.254\.|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.|172\.(1[6-9]|2\d|3[01])\.)' }).Count
```

Must print `0`. Your router, printer and phone are on the LAN and must never appear.
The `100.64` branch is carrier-grade NAT — the range that a naive `is_private`
check lets through.

### 6. Make something happen and watch it appear

```powershell
Start-Process "https://www.wikipedia.org"
```

Re-run step 3 a few seconds later.

### 7. Subsystem status

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health | Format-List
```

`dns_entries` in the dozens or hundreds, `dns_refreshes` climbing every 10s,
`geo_provider` showing which geo service is in use.

---

# Day 2 — geography and streaming

### 8. Where everything actually is

```powershell
$c = Invoke-RestMethod http://127.0.0.1:8000/api/connections; $c | Select-Object process, @{n='city';e={$_.geo.city}}, @{n='country';e={$_.geo.country}}, @{n='isp';e={$_.geo.isp}} | Format-Table
```

The ISP column is the interesting one. Verified output:

```
process       city          country       isp
brave.exe     Kansas City   United States Google LLC
Spotify.exe   Montreal      Canada        Google LLC
EXCEL.EXE     Pune          India         Microsoft Corporation
python.exe    San Francisco United States Cloudflare, Inc.
```

### 9. Countries you're currently touching

```powershell
(Invoke-RestMethod http://127.0.0.1:8000/api/health).countries_now
```

### 10. The cache is real and permanent

```powershell
python -c "import sqlite3; print(sqlite3.connect('phonehome/geoip.sqlite3').execute('select count(*), sum(ok) from geo').fetchone())"
```

Prints `(total_ips, successful_lookups)`. Now **stop the server (Ctrl+C) and start it
again**:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health | Select-Object geo_cached_ips, geo_lookups_this_session
```

`geo_cached_ips` matches the count above immediately while
`geo_lookups_this_session` stays at `0`. That's the proof no IP is looked up twice.

### 11. Watch the live stream

```powershell
python phonehome\ws_probe.py
```

Opening snapshot, then `+` new, `~` gained a hostname or location, `-` closed.
**This is exactly the data the globe consumes.**

### 12. Deltas actually close

With `ws_probe.py` running, quit your browser. A burst of `-` lines within a second.

---

# Day 3 — the globe

### 13. Open it

```powershell
python -m phonehome
```

Opens `http://127.0.0.1:8000/` automatically. You should see a dark globe centred
on your own location, a white pulsing marker where you are, and coloured arcs
firing outward as connections appear. Drag to spin; it resumes auto-rotating after
five seconds.

### 14. Check the page wired itself up

Press F12, Console tab, paste:

```js
({ conns: state.conns.size, located: [...state.conns.values()].filter(c=>c.geo).length, me: state.me && state.me.city, arcs: state.arcs.length, canvas: canvas.width+"x"+canvas.height })
```

`canvas` must **not** be `0x0` or `300x150` — that means the sizing failed.
`me` must be non-null or no arcs can be drawn.

### 15. Hover a dot

Tooltip shows hostname, city, ISP, process and port. Unresolved endpoints say
"no DNS name — went straight to IP" rather than pretending.

---

# Day 4 — sidebar, counters, report

### 16. Filter by process

Click any process in the left sidebar. The globe keeps only that process's dots and
arcs; the row highlights and the rest dim. Click again to clear.

### 17. Header counters

Destinations, countries, processes and elapsed time, updating live. Bottom-left also
reports how many endpoints have no location yet.

### 18. Session report

Click **Session report**. Four sections: top talkers, most contacted, countries, and
contacted-exactly-once. Verified output:

> 71 connections to 49 distinct destinations in 6 countries, from 18 processes,
> over 5 min. 29 of them never gave up a hostname.

Or without the browser:

```powershell
(Invoke-RestMethod http://127.0.0.1:8000/api/report).session | Format-List
```

### 19. The geo failover is remembered across restarts

ip-api.com is HTTP-only and some ISPs blackhole it. When that happens the app fails
over to ipwho.is **and writes that verdict to disk**, so the next launch doesn't pay
the strike-out cost again.

```powershell
python -c "import sqlite3, time; r = sqlite3.connect('phonehome/geoip.sqlite3').execute('select key, value from meta').fetchall(); print(r); print('failed', round((time.time()-float(r[0][1]))/60, 1), 'min ago') if r and r[0][1] else print('primary healthy')"
```

If a failover has happened, restarting the server logs:

```
ip-api.com failed 3 min ago -- starting on ipwho.is, will re-probe in 5.9 h
```

Measured effect on a connection where ip-api is blocked:

| | time to locate yourself |
|---|---|
| retrying the dead host | 9.0 s |
| remembered verdict | **0.7 s** |

After 6 hours it re-probes the primary automatically, and one success clears the marker.

### 20. Both entry points work

```powershell
python -m phonehome --no-browser --port 8010
```

```powershell
python phonehome\server.py
```

---

# Day 5 — rendering fixes

These are eyeball checks. Open the page and look.

### 21. It stays sharp when you zoom

Press **Ctrl +** a few times, then **Ctrl 0**. Country outlines and text must stay
crisp at every step — no soft or doubled edges.

Two separate causes were fixed here:

- The CSS box is now derived from the *rounded* backing store, so the two agree
  exactly. Previously the container measured `1020 x 659.5`, which gave a 660px
  backing store a 659.5px CSS box and left the browser resampling every frame.
- Browser zoom changes `devicePixelRatio` **without** changing the element's CSS
  size, so `ResizeObserver` never fired and the canvas kept its old resolution
  permanently. A `matchMedia("(resolution: Ndppx)")` listener now catches it.

Verify the first one from the console:

```js
canvas.width / parseFloat(canvas.style.width) === window.devicePixelRatio
```

Must be exactly `true` at every zoom level.

### 22. Scroll to zoom the globe itself

Scroll over the globe. It magnifies by redrawing vectors, so it gets *sharper*, not
blockier. Double-click resets. Range is 1x to 6x.

### 23. Every dot stays connected

Leave it running for a minute, then count: every coloured dot must have a faint line
running back to the white marker at your own location.

Previously arcs expired after ~10 seconds and never returned, so the globe filled up
with dots joined to nothing. Now there are two layers — a permanent thin link per
open connection, and the bright animated arc that fires once when a connection is
first seen.

Console check:

```js
[...state.conns.values()].filter(c => c.geo && c.geo.lat != null).length === linkCache.size
```

### 24. Arcs appear even on a slow start

The browser opens before the server has finished locating you, so its first snapshot
carries `self: null`. Confirm the recovery:

1. Stop the server.
2. In the page console: `state.me = null; state.conns.clear(); linkCache.clear()`
3. Start the server again and wait ~3 s.
4. `state.me` must be populated and arcs must appear.

Before this fix, that client drew **no arcs and no links for the entire session** —
the server only ever sent `self` inside the opening snapshot.

---

# Day 6 — polish

### 25. You can tell when the server dies

With the page open, stop the server. Within a second a red pill appears top-right:
*server offline — retrying in 4s*, then 8s, capping at 10s. Start the server again
and it disappears on its own.

Before this, a dead server looked identical to a quiet one — the globe kept spinning
over a frozen snapshot.

### 26. Zoom follows your cursor

Put the pointer over a specific country and scroll. That country stays under the
pointer instead of sliding away. On a globe this can't be done by offsetting a
translate — the sphere has to be rotated back by however far the anchor drifted.

Console check (drift should be a fraction of a degree, not tens):

```js
(() => { const at=[size.w*.62,size.h*.44], a=projection.invert(at);
  for(let i=0;i<5;i++) canvas.dispatchEvent(new WheelEvent("wheel",{deltaY:-100,cancelable:true,
    clientX:canvas.getBoundingClientRect().left+at[0], clientY:canvas.getBoundingClientRect().top+at[1]}));
  const b=projection.invert(at); return Math.abs(a[0]-b[0]).toFixed(2)+"° drift"; })()
```

Measured: **0.3°** across five zoom steps.

### 27. Dots stay readable when zoomed

Zoom to 6x. Endpoints grow too — sub-linearly (`zoom^0.45`, capped at 2.2x) so they
stay visible without swamping the globe. The hover grab radius scales with them.

### 28. Hovering highlights the dot

Hover an endpoint: a ring is drawn around it, so you can tell which one the tooltip
belongs to when several sit close together.

### 29. Tooltips stay on screen

Hover a dot near the top or side edge. The tooltip clamps horizontally and flips
below the dot when there isn't room above — worth checking with a long hostname like
`targetednotifications-tm.trafficmanager.net`.

### 30. Port already in use fails clearly

With one instance running, start another on the same port:

```powershell
python -m phonehome --port 8000
```

Expect a plain three-line explanation, not a uvicorn traceback:

```
  Port 8000 is already in use.
  Another PhoneHome is probably still running — close it, or pick
  another port:  python -m phonehome --port 8010
```

The port is probed before anything prints a URL, so it can't promise a link that
doesn't work.

### 31. Reduced motion is respected

With Windows *Settings → Accessibility → Visual effects → Animation effects* off, the
globe stops auto-rotating and the marker stops pulsing. Dragging still works.

---

# Day 7 — report correctness

### 32. The report is internally consistent

```powershell
$r = Invoke-RestMethod http://127.0.0.1:8000/api/report; ($r.top_processes | Measure-Object -Property connections -Sum).Sum -eq $r.session.connections_seen
```

Must print `True` — per-process connection counts have to add up to the session total.

### 33. No hostname appears twice

```powershell
$r = Invoke-RestMethod http://127.0.0.1:8000/api/report; ($r.top_destinations | Group-Object hostname | Where-Object Count -gt 1).Count
```

Must print `0`. The report groups by **hostname**, not by IP — a name with several
A/AAAA records is one destination to a reader. Previously `update.googleapis.com` printed
as two separate rows in a table headed "Host".

The `IPs` column shows how many addresses sit behind a name, and the summary reports
both figures: *"22 distinct destinations (23 addresses)"*.

### 34. Geography backfills into the report

Open the report within a few seconds of starting: some rows have no city yet. Open it
again a minute later — they're filled in. Hostname and location are looked up fresh at
report time rather than frozen when the connection was first recorded.

### 35. An empty report doesn't say "undefined"

Open the session report immediately at startup, before anything is recorded. Empty
sections must read *"Nothing yet."* Two of the four tables previously omitted the
argument and rendered the literal string `undefined`.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| A table prints nothing, or one row of arrays | `Invoke-RestMethod` passed the array as one object | Assign to `$c` first, then pipe — as every command above does |
| Everything shows as `unknown` | Protected system processes | Run the terminal as Administrator for full names — not required otherwise |
| **Only ~1/3 of rows have hostnames** | **Expected.** Browsers and Electron apps use DNS-over-HTTPS, so their lookups never touch the Windows resolver and leave no cache entry | Not fixable from here. Measured 7 of 21 on this machine. The UI shows the bare IP rather than pretending |
| `geo_provider` says `ipwho.is` | ip-api.com was unreachable twice and it failed over | Working as designed. ip-api is HTTP-only and some ISPs blackhole it — observed on Airtel. The verdict is remembered for 6 h, so later launches skip it immediately |
| No geography for ~15 s after a fresh start | First time the primary is being struck out on this machine | One-off. The verdict is then cached; subsequent starts locate in under a second |
| Globe is blank, canvas is `300x150` | Container had no size when it was measured | Should be impossible now — a `ResizeObserver` handles it. If it recurs, check `main` has a height |
| Geography looks wrong | GeoIP places the *edge node*; anycast/CDN addresses often land on your own ISP's region | Real limit of free IP geolocation; don't claim street accuracy |
| Port 8000 in use | Something else is bound | `python -m phonehome --port 8010` |
