"""ratd: device service. Owns the mice, switches profiles on window focus, serves D-Bus."""
import asyncio
import glob
import json
import logging
import os
import signal
import sys

import evdev
import pyudev
from dbus_next import BusType, Message, MessageType
from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, method, signal as dbus_signal

from . import config
from .backends.logitech import LogitechMouse
from .hidpp import HidppNode, HidppError

log = logging.getLogger("ratd")

BUS_NAME, OBJ_PATH, IFACE = "org.ratd", "/org/ratd", "org.ratd.Manager"
LOGITECH = 0x046D
CHECK_INTERVAL = 30          # seconds between health checks / battery refresh
LOW_BATTERY = (15, 5)        # notify when crossing these percentages


def _sysattr(node, name):
    """Read a key from /sys/class/hidraw/<node>/device/uevent."""
    try:
        with open(f"/sys/class/hidraw/{node}/device/uevent") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k == name:
                    return v
    except OSError:
        pass
    return None


def _is_hidpp_node(node):
    """Logitech hidraw node whose report descriptor starts with vendor page 0xFF00."""
    hid_id = _sysattr(node, "HID_ID")
    if not hid_id or int(hid_id.split(":")[1], 16) != LOGITECH:
        return False
    try:
        with open(f"/sys/class/hidraw/{node}/device/report_descriptor", "rb") as f:
            return f.read(3) == b"\x06\x00\xff"
    except OSError:
        return False


def _evdev_for(phys_prefix):
    """The evdev node carrying the mouse buttons, found by its physical path."""
    for path in evdev.list_devices():
        d = evdev.InputDevice(path)
        try:
            if d.phys == phys_prefix or d.phys.startswith(phys_prefix + "/"):
                keys = d.capabilities().get(evdev.ecodes.EV_KEY, [])
                if evdev.ecodes.BTN_LEFT in keys:
                    return path
        finally:
            d.close()
    return None


class Daemon:
    def __init__(self):
        self.cfg = config.load()
        self.nodes = {}              # hidraw path -> HidppNode
        self.mice = {}               # (hidraw path, devidx) -> LogitechMouse
        self.focus = ("", "")        # (window class, caption)
        self.recent = []             # recently focused window classes, newest first
        self.override = None         # manually chosen profile, or None for automatic
        self.iface = None
        self._battery_warned = {}
        self._apply_tasks = set()

    # ---- discovery -------------------------------------------------------------

    async def scan(self):
        for sysdir in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
            node = os.path.basename(sysdir)
            if f"/dev/{node}" not in self.nodes and _is_hidpp_node(node):
                await self._probe(node)

    async def _probe(self, node):
        path = f"/dev/{node}"
        try:
            hn = HidppNode(path)
        except OSError as e:
            log.warning("%s: can't open (%s)", path, e)
            return
        name = _sysattr(node, "HID_NAME") or ""
        phys = _sysattr(node, "HID_PHYS") or ""
        receiver = "Receiver" in name
        found = False
        for idx in (range(1, 7) if receiver else (0xFF,)):
            # A paired receiver slot has its own evdev node even while the mouse sleeps.
            mouse_phys = f"{phys}:{idx}" if receiver else phys.rsplit("/", 1)[0]
            ev = _evdev_for(mouse_phys)
            if not ev:
                continue
            m = LogitechMouse(hn, idx, ev, self._changed, self.bring_up)
            self.mice[(path, idx)] = m
            found = True
            await self.bring_up(m)
        if found:
            self.nodes[path] = hn
        else:
            hn.close()

    async def bring_up(self, m):
        """Take the mouse over and apply its profile. Harmless if it's asleep; we retry on wake."""
        try:
            await m.start()
        except (HidppError, TimeoutError, ConnectionError, OSError) as e:
            log.info("%s idx %d: not answering yet (%s)", m.dev.node.path, m.dev.idx,
                     str(e) or type(e).__name__)
            m.watch_wake()
            return
        except Exception:               # a bug in one device's setup must not take the service down
            log.exception("%s idx %d: setup failed", m.dev.node.path, m.dev.idx)
            return
        section, created = config.device_section(self.cfg, m.key, m.default_section())
        if created:
            config.save(self.cfg)
            log.info("%s: created default profile in %s", m.name, config.PATH)
        await self._apply(m, force=True)

    def forget(self, path):
        for key in [k for k in self.mice if k[0] == path]:
            m = self.mice.pop(key)
            if m.keymap:
                m.keymap.close()
                m.keymap = None
            log.info("%s: removed", m.name)
        hn = self.nodes.pop(path, None)
        if hn:
            hn.close()
        self._emit()

    # ---- profiles --------------------------------------------------------------

    def wanted_profile(self, m):
        section = self.cfg["devices"][m.key]
        name = self.override or config.match_rule(self.cfg, *self.focus)
        if name not in section["profiles"]:
            name = section["default_profile"]
        return name, section["profiles"][name]

    async def _apply(self, m, force=False):
        if not m.connected or m.key not in self.cfg["devices"]:
            return                          # bring_up() applies once it can talk to the mouse
        name, profile = self.wanted_profile(m)
        if force or name != m.profile_name:
            try:
                await m.apply_profile(name, profile)
                log.info("%s: profile %s", m.name, name)
            except (HidppError, TimeoutError, ConnectionError, OSError) as e:
                log.warning("%s: applying profile %s failed: %s", m.name, name, e)

    async def apply_all(self, force=False):
        for m in list(self.mice.values()):
            await self._apply(m, force)

    def schedule_apply(self, force=False):
        t = asyncio.get_running_loop().create_task(self.apply_all(force))
        self._apply_tasks.add(t)
        t.add_done_callback(self._apply_tasks.discard)

    # ---- state -----------------------------------------------------------------

    def state(self):
        return {"focus": {"class": self.focus[0], "caption": self.focus[1]},
                "override": self.override,
                "recent": self.recent,
                "devices": self._visible_devices()}

    def _visible_devices(self):
        """One entry per mouse: if it's reachable wired and wireless, show the live link."""
        best = {}
        for m in self.mice.values():
            if m.name == "?":
                continue
            cur = best.get(m.key)
            if cur is None or (m.connected and not cur.connected):
                best[m.key] = m
        return [m.state() for m in best.values()]

    def _changed(self, m):
        if m.battery and m.battery[0] is not None:
            self._check_battery(m)
        self._emit()

    def _emit(self):
        if self.iface:
            self.iface.StateChanged(json.dumps(self.state()))

    def _check_battery(self, m):
        pct, status = m.battery
        last = self._battery_warned.get(m.key)
        if status != "discharging":
            self._battery_warned.pop(m.key, None)
            return
        for limit in LOW_BATTERY:
            if pct <= limit and (last is None or last > limit):
                self._battery_warned[m.key] = limit
                if not self.cfg.get("notify_low_battery", True):
                    break
                asyncio.get_running_loop().create_task(self._notify(
                    f"{m.name} battery low", f"{pct}% remaining"))
                break

    async def _notify(self, summary, body):
        try:
            proc = await asyncio.create_subprocess_exec(
                "notify-send", "-a", "ratd", "-i", "battery-low", summary, body)
            await proc.wait()
        except OSError:
            pass

    # ---- loops -----------------------------------------------------------------

    async def health_loop(self):
        while True:
            await asyncio.sleep(CHECK_INTERVAL)
            for m in list(self.mice.values()):
                try:
                    if await m.check():
                        await self.bring_up(m)
                except (HidppError, TimeoutError, ConnectionError, OSError) as e:
                    log.warning("%s: health check failed: %s", m.name, e)

    async def udev_loop(self):
        ctx = pyudev.Context()
        mon = pyudev.Monitor.from_netlink(ctx)
        mon.filter_by("hidraw")
        mon.start()
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue()
        loop.add_reader(mon.fileno(), lambda: queue.put_nowait(mon.poll(0)))
        while True:
            dev = await queue.get()
            if dev is None:
                continue
            if dev.action == "add":
                await asyncio.sleep(1.0)      # let evdev nodes appear too
                await self.scan()
            elif dev.action == "remove":
                self.forget(dev.device_node)

    async def shutdown(self):
        for m in self.mice.values():
            await m.stop()
        for hn in self.nodes.values():
            hn.close()


class Manager(ServiceInterface):
    def __init__(self, d):
        super().__init__(IFACE)
        self.d = d

    @method()
    def GetState(self) -> "s":
        return json.dumps(self.d.state())

    @method()
    def GetConfig(self) -> "s":
        return json.dumps(self.d.cfg)

    @method()
    def SetConfig(self, cfg_json: "s") -> "s":
        """Validate, save, and apply a whole config. Returns '' or an error message."""
        try:
            cfg = json.loads(cfg_json)
            config.save(cfg)
        except (ValueError, config.ConfigError) as e:
            return str(e)
        self.d.cfg = cfg
        self.d.schedule_apply(force=True)
        return ""

    @method()
    def ReloadConfig(self) -> "s":
        try:
            self.d.cfg = config.load()
        except (ValueError, config.ConfigError) as e:
            return str(e)
        self.d.schedule_apply(force=True)
        return ""

    @method()
    def SetProfile(self, name: "s"):
        """Force a profile by name on every device that has it; '' returns to automatic."""
        self.d.override = name or None
        self.d.schedule_apply()
        self.d._emit()

    @method()
    def SetDpi(self, dpi: "i"):
        for m in self.d.mice.values():
            if m.connected:
                asyncio.get_running_loop().create_task(m.set_dpi(dpi))

    @method()
    def WindowActivated(self, window_class: "s", caption: "s"):
        self.d.focus = (window_class, caption)
        if window_class:
            entry = {"class": window_class, "caption": caption}
            self.d.recent = [entry] + [r for r in self.d.recent if r["class"] != window_class][:19]
        self.d.schedule_apply()
        self.d._emit()

    @method()
    def OpenSettings(self):
        """Launch the settings window (used by the panel widget)."""
        asyncio.get_running_loop().create_task(_spawn([sys.executable, "-m", "ratd.gui"]))

    @dbus_signal()
    def StateChanged(self, state_json) -> "s":
        return state_json


async def _spawn(argv):
    try:
        proc = await asyncio.create_subprocess_exec(*argv, start_new_session=True,
                                                    stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
    except OSError as e:
        log.warning("couldn't start %s: %s", argv, e)


async def _resync_focus(bus):
    """Reload the ratd-focus KWin script so it re-reports the current window to us."""
    script = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
                          "kwin/scripts/ratd-focus/contents/code/main.js")
    if not os.path.exists(script):
        log.warning("KWin focus script not installed; per-game profiles won't switch")
        return
    async def kwin(member, signature="", body=()):
        reply = await bus.call(Message(destination="org.kde.KWin", path="/Scripting",
                                       interface="org.kde.kwin.Scripting", member=member,
                                       signature=signature, body=list(body)))
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{member}: {reply.error_name} {reply.body}")
        return reply.body

    try:
        if (await kwin("isScriptLoaded", "s", ["ratd-focus"]))[0]:
            await kwin("unloadScript", "s", ["ratd-focus"])
        await kwin("loadScript", "ss", [script, "ratd-focus"])
        await kwin("start")
    except Exception as e:      # KWin missing or not Plasma: focus switching just stays off
        log.warning("couldn't reload KWin focus script: %s", e)


async def main():
    logging.basicConfig(level=os.environ.get("RATD_LOG", "INFO"),
                        format="%(levelname)s %(name)s: %(message)s")
    d = Daemon()
    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    d.iface = Manager(d)
    bus.export(OBJ_PATH, d.iface)
    reply = await bus.request_name(BUS_NAME)
    if reply.value not in (1, 4):     # primary owner / already owner
        raise SystemExit("ratd is already running")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        loop.add_signal_handler(sig, stop.set)

    await _resync_focus(bus)
    await d.scan()
    if not d.mice:
        log.warning("no supported mouse found yet; waiting for one to appear")
    tasks = [loop.create_task(d.health_loop()), loop.create_task(d.udev_loop())]
    await stop.wait()
    log.info("shutting down, restoring onboard mode")
    for t in tasks:
        t.cancel()
    await d.shutdown()
    bus.disconnect()


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
