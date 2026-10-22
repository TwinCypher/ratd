"""ratctl: command-line client for the ratd service."""
import argparse
import asyncio
import json
import sys

from dbus_next import BusType
from dbus_next.aio import MessageBus

from .daemon import BUS_NAME, OBJ_PATH, IFACE


async def _proxy():
    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    intro = await bus.introspect(BUS_NAME, OBJ_PATH)
    return bus.get_proxy_object(BUS_NAME, OBJ_PATH, intro).get_interface(IFACE)


def _print_state(s):
    print(f"focus: {s['focus']['class'] or '-'}" + (f"   (profile forced: {s['override']})" if s["override"] else ""))
    if not s["devices"]:
        print("no devices")
    for d in s["devices"]:
        batt = f"{d['battery']}% {d['battery_status']}" if d["battery"] is not None else "?"
        state = "connected" if d["connected"] else "asleep/offline"
        print(f"{d['name']}: {state}, profile {d['profile']}, {d['dpi']} DPI, battery {batt}")


async def main(argv):
    ap = argparse.ArgumentParser(prog="ratctl")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("status", help="show devices, profile, DPI, battery")
    p = sub.add_parser("profile", help="force a profile, or 'auto' to follow window focus")
    p.add_argument("name")
    p = sub.add_parser("dpi", help="set DPI right now (until the next profile change)")
    p.add_argument("value", type=int)
    sub.add_parser("reload", help="re-read ~/.config/ratd/config.json")
    sub.add_parser("watch", help="print state changes as they happen")
    args = ap.parse_args(argv)
    try:
        m = await _proxy()
    except Exception as e:
        sys.exit(f"ratd isn't running ({e})")
    if args.cmd in (None, "status"):
        _print_state(json.loads(await m.call_get_state()))
    elif args.cmd == "profile":
        await m.call_set_profile("" if args.name == "auto" else args.name)
    elif args.cmd == "dpi":
        await m.call_set_dpi(args.value)
    elif args.cmd == "reload":
        err = await m.call_reload_config()
        sys.exit(err or None)
    elif args.cmd == "watch":
        m.on_state_changed(lambda s: _print_state(json.loads(s)))
        await asyncio.Event().wait()


def run():
    try:
        asyncio.run(main(sys.argv[1:]))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
