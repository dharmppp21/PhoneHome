"""Watch the /ws stream from a terminal. Run the server first, then: python ws_probe.py"""

import asyncio
import json
import sys

import websockets

URL = "ws://127.0.0.1:8000/ws"

# Without this, Python block-buffers stdout whenever it isn't a terminal, so piping
# this to a file or another process shows nothing until it exits.
sys.stdout.reconfigure(line_buffering=True)


def where(conn: dict) -> str:
    geo = conn.get("geo")
    if not geo:
        return "locating..."
    return ", ".join(p for p in (geo.get("city"), geo.get("country")) if p) or "unknown"


async def main() -> None:
    async with websockets.connect(URL) as ws:
        first = json.loads(await ws.recv())
        assert first["type"] == "snapshot", first
        me = first.get("self")
        print(f"you:  {me['city']}, {me['country']}  ({me['ip']})" if me else "you:  not located yet")
        print(f"open: {len(first['connections'])} connections\n")
        for conn in first["connections"][:15]:
            print(f"      {conn['process']:<24} {conn['hostname']:<48} {where(conn)}")

        print("\nwatching  (+ new   ~ enriched   - closed)   Ctrl+C to stop\n")
        while True:
            msg = json.loads(await ws.recv())
            for conn in msg.get("new", []):
                print(f"  +   {conn['process']:<24} {conn['hostname']:<48} {where(conn)}")
            for conn in msg.get("updated", []):
                print(f"  ~   {conn['process']:<24} {conn['hostname']:<48} {where(conn)}")
            for conn_id in msg.get("closed", []):
                print(f"  -   {conn_id}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
