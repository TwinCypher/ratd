"""Volume keys pinned to one output device.

Plasma's volume keys change the default sink, which moves to whatever was used last.
With "volume_device" set, ratd handles volume keys itself: the mouse's bindings directly,
and the volume keys of the input devices named in "volume_keyboards" (knobs, media keys)
by grabbing them and passing every other event through a uinput copy. Devices not listed,
like a headset's own dial, keep their usual behaviour.
"""
import asyncio
import logging
import re

import evdev
from evdev import ecodes as E

log = logging.getLogger(__name__)

KEYS = {E.KEY_VOLUMEUP: "up", E.KEY_VOLUMEDOWN: "down", E.KEY_MUTE: "mute"}


async def _pactl(*args):
    proc = await asyncio.create_subprocess_exec("pactl", *args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode:
        raise RuntimeError(err.decode().strip() or f"pactl {args[0]} failed")
    return out.decode()


class Volume:
    def __init__(self, bus, cfg):
        self.bus = bus
        self.cfg = cfg                      # fn() -> current config
        self.tap = KeyboardTap(self.key, lambda: self.cfg().get("volume_keyboards", []))
        self._lock = asyncio.Lock()         # knob spins: one read-modify-write at a time

    @property
    def device(self):
        return self.cfg().get("volume_device")

    def handles(self, code):
        return bool(self.device) and code in KEYS

    def refresh(self):
        """Grab or release keyboards after a config change or hotplug."""
        self.tap.refresh(bool(self.device))

    async def key(self, code):
        dev, step = self.device, self.cfg().get("volume_step", 5)
        if not dev:
            return
        try:
            async with self._lock:
                if KEYS[code] == "mute":
                    await _pactl("set-sink-mute", dev, "toggle")
                else:
                    cur = int(re.search(r"(\d+)%", await _pactl("get-sink-volume", dev)).group(1))
                    new = max(0, min(100, cur + (step if KEYS[code] == "up" else -step)))
                    await _pactl("set-sink-mute", dev, "0")
                    await _pactl("set-sink-volume", dev, f"{new}%")
                muted = "yes" in await _pactl("get-sink-mute", dev)
                pct = 0 if muted else int(re.search(r"(\d+)%", await _pactl("get-sink-volume", dev)).group(1))
            await self._osd(pct)
        except Exception as e:      # missing device or no pactl: the key just does nothing
            log.warning("volume %s on %s failed: %s", KEYS[code], dev, e)

    async def _osd(self, pct):
        from dbus_next import Message
        try:
            await self.bus.call(Message(destination="org.kde.plasmashell", path="/org/kde/osdService",
                                        interface="org.kde.osdService", member="volumeChanged",
                                        signature="ii", body=[pct, 100]))
        except Exception as e:      # not Plasma: no popup
            log.debug("volume OSD failed: %s", e)


def candidate(d):
    """Can this input device's volume keys be taken over? Only media/consumer nodes:
    never a typing node (KEY_A), since everything on a grabbed node goes through us."""
    keys = d.capabilities().get(E.EV_KEY, [])
    return (E.KEY_VOLUMEUP in keys and E.KEY_A not in keys
            and not d.name.startswith(("ratd", "keyd")))


class KeyboardTap:
    """Grabs the chosen devices' input nodes, eats volume keys, forwards the rest."""

    def __init__(self, on_key, names):
        self.on_key = on_key
        self.names = names                  # fn() -> device names to take over
        self.taps = {}                      # event path -> (InputDevice, UInput)

    def _wanted(self, d):
        return d.name in self.names() and candidate(d)

    def refresh(self, active):
        for path in list(self.taps):        # unselected since last time
            if not active or self.taps[path][0].name not in self.names():
                self._drop(path)
        if not active:
            return
        for path in evdev.list_devices():
            if path in self.taps:
                continue
            try:
                d = evdev.InputDevice(path)
            except OSError:
                continue
            if not self._wanted(d):
                d.close()
                continue
            try:
                ui = evdev.UInput.from_device(d, name=f"ratd passthrough: {d.name}")
                d.grab()
            except OSError as e:
                log.warning("can't take over volume keys of %s: %s", d.name, e)
                d.close()
                continue
            self.taps[path] = (d, ui)
            asyncio.get_running_loop().add_reader(d.fd, self._read, path)
            log.info("volume keys of %s now go to the pinned device", d.name)

    def _read(self, path):
        d, ui = self.taps[path]
        try:
            for ev in d.read():
                if ev.type == E.EV_KEY and ev.code in KEYS:
                    if ev.value:            # press or autorepeat
                        asyncio.get_running_loop().create_task(self.on_key(ev.code))
                    continue
                ui.write_event(ev)
        except BlockingIOError:
            pass
        except OSError:                     # unplugged
            self._drop(path)

    def _drop(self, path):
        d, ui = self.taps.pop(path)
        try:
            asyncio.get_running_loop().remove_reader(d.fd)
        except (ValueError, OSError):
            pass
        for close in (d.ungrab, d.close, ui.close):
            try:
                close()
            except OSError:
                pass
