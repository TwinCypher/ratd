"""Logitech G600 (wired, pre-HID++): onboard-profile takeover plus a raw-report button spy.

The G600 has no host mode. ratd writes its own onboard profile into the last slot and
activates it: left/right/middle, the wheel tilts and G9-G19 report as HID mouse buttons
1-16 on the mouse interface, and the other four (G-Shift, G7, G8, G20) as F13-F16 on the
keyboard interface. Those are remapped in-kernel like on the HID++ mice. A button mapped
to KEY_RESERVED vanishes from evdev entirely (no press, no release, no MSC_SCAN), so
presses are read from the raw HID reports on hidraw instead, which the keymap never sees.

Profile writes go to flash. The mouse has to be left alone ~2 s before and after one, or
it stores the profile shifted by a byte. DPI changes only select one of the profile's
four DPI slots, which takes ~6 ms and is safe at any rate.

On stop, the profile that was active before the takeover is activated again.
"""
import asyncio
import fcntl
import logging
import os
import time

import evdev
from evdev import ecodes as E

from .. import keymap
from ..errors import DeviceError
from .base import Mouse, DEVICE_ERRORS, sysattr

log = logging.getLogger(__name__)

HID_ID = "0003:0000046D:0000C24A"
RATD_SLOT = 2                       # onboard profile ratd writes and runs (0-2)
PROFILE_SIZE = 154                  # feature reports 0xF3-0xF5, report id included
ACTIVE = 0xF0                       # read: profile << 4 | dpi slot << 1; write: see below
SETTLE = 2.0                        # quiet seconds around a flash write
DPI_MIN, DPI_MAX, DPI_STEP = 200, 8200, 50
RATES = [125, 250, 500, 1000]       # polling rate byte = 1000 / Hz - 1

# Onboard button codes (code, modifier bits, HID key usage).
NATIVE_BUTTONS = ["BTN_LEFT", "BTN_RIGHT", "BTN_MIDDLE", "BTN_SIDE", "BTN_EXTRA"]   # codes 1-5
CODE_DPI = {0x11: "up", 0x12: "down", 0x13: "cycle", 0x15: "shift"}
CODE_PROFILE_CYCLE, CODE_GSHIFT = 0x14, 0x17
MODIFIER_KEYS = ["KEY_LEFTCTRL", "KEY_LEFTSHIFT", "KEY_LEFTALT", "KEY_LEFTMETA",
                 "KEY_RIGHTCTRL", "KEY_RIGHTSHIFT", "KEY_RIGHTALT", "KEY_RIGHTMETA"]

# Button table order = slot in the onboard profile = our button index.
MODELS = {"G600": ["left", "right", "middle", "tilt_left", "tilt_right", "gshift", "g7", "g8"]
                  + [f"g{n}" for n in range(9, 21)]}
LABELS = {"left": "Left click", "right": "Right click", "middle": "Middle click",
          "tilt_left": "Wheel tilt left", "tilt_right": "Wheel tilt right",
          "gshift": "G-Shift (beside right click)", "g7": "G7 (top, rear)",
          "g8": "G8 (top, behind the wheel)",
          **{f"g{n}": f"G{n} (side)" for n in range(9, 21)}}
DEFAULTS = {
    "left": {"type": "key", "key": "BTN_LEFT"}, "right": {"type": "key", "key": "BTN_RIGHT"},
    "middle": {"type": "key", "key": "BTN_MIDDLE"},
    "tilt_left": {"type": "scroll", "direction": "left"},
    "tilt_right": {"type": "scroll", "direction": "right"},
    "gshift": {"type": "shift"},
    "g7": {"type": "dpi", "action": "shift"}, "g8": {"type": "dpi", "action": "cycle"},
    **{f"g{n}": {"type": "key", "key": k} for n, k in zip(range(9, 21),
       ["KEY_1", "KEY_2", "KEY_3", "KEY_4", "KEY_5", "KEY_6", "KEY_7", "KEY_8", "KEY_9",
        "KEY_0", "KEY_MINUS", "KEY_EQUAL"])},
}
# How ratd's profile makes each slot report: HID mouse button n, or a keyboard usage.
MOUSE_CODE = {0: 1, 1: 2, 2: 3, 3: 4, 4: 5, **{8 + k: 6 + k for k in range(11)}}
KEY_USAGE = {5: 0x68, 6: 0x69, 7: 0x6A, 19: 0x6B}                  # F13-F16
SLOT_OF_CODE = {c: i for i, c in MOUSE_CODE.items()}
SLOT_OF_USAGE = {u: i for i, u in KEY_USAGE.items()}


def _ioc(nr, size):
    return (3 << 30) | (size << 16) | (ord("H") << 8) | nr      # _IOC(R|W, 'H', nr, size)


def _phys_node(phys, want_key):
    """evdev node at exactly this physical path that has key/button `want_key`."""
    for path in evdev.list_devices():
        d = evdev.InputDevice(path)
        try:
            if d.phys == phys and want_key in d.capabilities().get(E.EV_KEY, []):
                return path
        finally:
            d.close()
    return None


def _hidraw_at(phys):
    for node in sorted(os.listdir("/sys/class/hidraw")):
        if sysattr(node, "HID_PHYS") == phys:
            return f"/dev/{node}"
    return None


def probe(node, hooks):
    """The G600's configuration interface (vendor feature reports on interface 1)."""
    if sysattr(node, "HID_ID") != HID_ID:
        return None
    try:
        with open(f"/sys/class/hidraw/{node}/device/report_descriptor", "rb") as f:
            if b"\x85\xf3" not in f.read():               # no profile report: interface 0
                return None
    except OSError:
        return None
    usb = (sysattr(node, "HID_PHYS") or "").rsplit("/", 1)[0]
    handle = G600Node(f"/dev/{node}")
    mouse_ev = _phys_node(f"{usb}/input0", E.BTN_LEFT)
    kbd_ev = _phys_node(f"{usb}/input1", E.KEY_A)
    mouse_raw = _hidraw_at(f"{usb}/input0")
    if not (mouse_ev and kbd_ev and mouse_raw):
        log.warning("%s: G600 found but its input nodes aren't all there yet", handle.path)
        return handle, {}
    return handle, {0: G600Mouse(handle, mouse_raw, mouse_ev, kbd_ev, hooks)}


class G600Node:
    """Feature reports on the configuration interface, kept apart in time around flash writes."""

    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDWR)
        self._lock = asyncio.Lock()
        self._last = 0.0                # monotonic time of the last request
        self._quiet_until = 0.0         # no request at all before this (a flash write is running)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    async def _wait(self, until):
        delay = until - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    async def get(self, rid, size):
        async with self._lock:
            await self._wait(self._quiet_until)
            buf = bytearray(size)
            buf[0] = rid
            n = fcntl.ioctl(self.fd, _ioc(0x07, size), buf)
            self._last = time.monotonic()
            return bytes(buf[:n])

    async def set(self, data, flash=False):
        """Send a feature report. flash=True: it gets written to flash, so keep clear of it."""
        async with self._lock:
            await self._wait(max(self._quiet_until, self._last + SETTLE if flash else 0))
            n = fcntl.ioctl(self.fd, _ioc(0x06, len(data)), bytearray(data))
            self._last = time.monotonic()
            if flash:
                self._quiet_until = self._last + SETTLE
            if n != len(data):
                raise DeviceError(f"short feature write: {n} of {len(data)} bytes")

    async def active(self):
        """(onboard profile, dpi slot) in use."""
        b = (await self.get(ACTIVE, 4))[1]
        return b >> 4, b >> 1 & 3


class G600Mouse(Mouse):
    VENDOR = "logitech"
    MODELS, LABELS, DEFAULTS = MODELS, LABELS, DEFAULTS

    def __init__(self, node, mouse_hidraw, evdev_path, kbd_evdev, hooks):
        super().__init__(evdev_path, hooks)
        self.node = node
        self.mouse_hidraw = mouse_hidraw
        self.kbd_evdev = kbd_evdev
        self.kbd_keymap = None
        self.dpi_choices = list(range(DPI_MIN, DPI_MAX + 1, DPI_STEP))
        self.rate_choices = RATES
        self._orig_codes = {}           # (node, scancode) -> keycode before we touched it
        self._hand_back_slot = 0
        self._hand_back_report = None   # profile report active before the takeover
        self._onboard = None            # ratd's profile report as the mouse has it
        self._slots = []                # its four DPI slots, 0 = unused
        self._spy_fds = []
        self._mouse_mask = 0
        self._keys = set()

    @property
    def where(self):
        return self.node.path

    # ---- lifecycle -------------------------------------------------------------

    async def start(self):
        async with self._init_lock:
            active, _ = await self.node.active()
            if self._hand_back_report is None:
                # Already on ratd's slot means a previous run didn't hand back: use profile 0.
                self._hand_back_slot = active if active != RATD_SLOT else 0
                self._hand_back_report = await self.node.get(0xF3 + self._hand_back_slot, PROFILE_SIZE)
                self._onboard = await self.node.get(0xF3 + RATD_SLOT, PROFILE_SIZE)
                self.name = "G600"
                self.buttons = MODELS["G600"]
            if self.keymap is None:
                self.keymap = keymap.Keymap(self.evdev_path)
                self.kbd_keymap = keymap.Keymap(self.kbd_evdev)
                for i in range(len(self.buttons)):
                    which, sc = self._scan(i)
                    self._orig_codes.setdefault((which, sc), self._keymap(which).get(sc))
            if self._profile:
                self._slots = self._slots_for(self._profile)
                rate = self._rate_for(self._profile)
            else:
                self._slots = [d * 50 for d in self._hand_back_report[14:18] if d]
                rate = 1000 // (self._hand_back_report[11] + 1)
            await self._write_onboard(self._build(rate))
            self.report_rate = rate
            if active != RATD_SLOT:
                await self.node.set(bytes([ACTIVE, 0x80 | RATD_SLOT << 4, 0, 0]), flash=True)
                if (await self.node.active())[0] != RATD_SLOT:
                    raise DeviceError("the G600 didn't switch to ratd's onboard profile")
            self._start_spy()
            self.connected = True
            if self._profile:
                await self._apply(self._profile, keep_dpi=True)
            log.info("%s: onboard profile %d taken over (hand back to %d)", self.name,
                     RATD_SLOT, self._hand_back_slot)
        self.on_change(self)

    async def check(self):
        try:
            active, _ = await self.node.active()
        except DEVICE_ERRORS as e:
            if self.connected:
                log.info("%s: not answering (%s)", self.name, str(e) or type(e).__name__)
                self.connected = False
                self.on_change(self)
            return False
        if active != RATD_SLOT or not self.connected:
            log.info("%s: on onboard profile %d, taking it over", self.name, active)
            return True
        return False

    async def _hand_back(self):
        self._stop_spy()
        if self.kbd_keymap:
            self.kbd_keymap.close()
            self.kbd_keymap = None
        try:
            await self.node.set(bytes([ACTIVE, 0x80 | self._hand_back_slot << 4, 0, 0]), flash=True)
            log.info("%s: handed back to onboard profile %d", self.name, self._hand_back_slot)
        except DEVICE_ERRORS as e:
            log.warning("%s: couldn't switch back to onboard profile %d (%s)", self.name,
                        self._hand_back_slot, str(e) or type(e).__name__)

    def default_section(self):
        """The onboard profile that was active before, translated into a ratd profile."""
        hb = self._hand_back_report
        dpis = [d * 50 for d in hb[14:18] if d] or [800, 1600, 3200]
        p = {"dpi": dpis, "dpi_index": min(max(hb[13] - 1, 0), len(dpis) - 1),
             "report_rate": min(RATES, key=lambda r: abs(r - 1000 // (hb[11] + 1))),
             "buttons": {}}
        if hb[12]:
            p["sniper_dpi"] = hb[12] * 50
        shift = {}
        for i, b in enumerate(self.buttons):
            normal = self._translate(b, hb[31 + 3 * i:34 + 3 * i])
            shifted = self._translate(b, hb[94 + 3 * i:97 + 3 * i])
            p["buttons"][b] = normal
            if shifted != normal and shifted["type"] != "shift":
                shift[b] = shifted
        if shift and any(a["type"] == "shift" for a in p["buttons"].values()):
            p["shift"] = shift          # reachable only if some button holds the layer
        return {"default_profile": "Desktop", "profiles": {"Desktop": p}}

    def _translate(self, name, entry):
        code, mods, usage = entry
        if code == 0 and usage:
            try:
                key = _key_name(self.kbd_keymap.get(0x70000 + usage))
            except (OSError, KeyError):
                return {"type": "none"}
            mod_keys = [MODIFIER_KEYS[i] for i in range(8) if mods >> i & 1]
            act = {"type": "keys", "keys": mod_keys + [key]} if mod_keys else {"type": "key", "key": key}
        elif code == 0 or code == CODE_PROFILE_CYCLE:
            act = {"type": "none"}
        elif 1 <= code <= len(NATIVE_BUTTONS):
            act = {"type": "key", "key": NATIVE_BUTTONS[code - 1]}
        elif code in CODE_DPI:
            act = {"type": "dpi", "action": CODE_DPI[code]}
        elif code == CODE_GSHIFT:
            act = {"type": "shift"}
        else:
            return {"type": "default"}
        return {"type": "default"} if act == DEFAULTS.get(name) or name == "left" else act

    # ---- onboard profile -------------------------------------------------------

    def _build(self, rate):
        """ratd's profile report: the hand-back profile's look, our buttons, DPI slots, rate."""
        r = bytearray(self._hand_back_report)
        r[0] = 0xF3 + RATD_SLOT
        for i in range(20):
            entry = bytes([MOUSE_CODE[i], 0, 0]) if i in MOUSE_CODE else bytes([0, 0, KEY_USAGE[i]])
            r[31 + 3 * i:34 + 3 * i] = entry
            r[94 + 3 * i:97 + 3 * i] = entry
        r[11] = 1000 // rate - 1
        r[12] = 0                                   # the onboard DPI shift button is unused
        r[13] = 1
        r[14:18] = bytes(d // 50 for d in (self._slots + [0] * 4)[:4])
        return bytes(r)

    async def _write_onboard(self, data):
        if data == self._onboard:
            return
        await self.node.set(data, flash=True)
        back = await self.node.get(data[0], PROFILE_SIZE)
        if back != data:
            raise DeviceError("the G600 stored ratd's profile wrongly")
        self._onboard = data
        log.info("%s: wrote onboard profile %d (DPI slots %s, %d Hz)", self.name, RATD_SLOT,
                 self._slots, 1000 // (data[11] + 1))

    def _snap(self, dpi):
        return min(self.dpi_choices, key=lambda c: abs(c - dpi))

    def _slots_for(self, profile):
        """Four DPI slots: the profile's first DPI values, with room kept for sniper DPI."""
        vals = []
        for d in profile["dpi"]:
            if self._snap(d) not in vals:
                vals.append(self._snap(d))
        sniper = profile.get("sniper_dpi")
        if sniper and self._snap(sniper) not in vals[:4]:
            vals = vals[:3] + [self._snap(sniper)]
        return vals[:4]

    def _rate_for(self, profile):
        hz = profile.get("report_rate")
        return max([r for r in RATES if r <= hz] or RATES[:1]) if hz else RATES[-1]

    async def _apply_hw(self, profile):
        self._slots = self._slots_for(profile)
        rate = self._rate_for(profile)
        await self._write_onboard(self._build(rate))
        self.report_rate = rate

    async def _write_dpi(self, dpi):
        if dpi not in self._slots:
            # Not one of the four slots (a fifth DPI step, or set from outside): rewrite one.
            if len(self._slots) < 4:
                self._slots.append(dpi)
            else:
                self._slots[3] = dpi
            await self._write_onboard(self._build(self.report_rate))
        await self.node.set(bytes([ACTIVE, 0x40 | self._slots.index(dpi) << 1, 0, 0]))

    # ---- keymap ----------------------------------------------------------------

    def _scan(self, i):
        if i in MOUSE_CODE:
            return "mouse", 0x90000 + MOUSE_CODE[i]
        return "kbd", 0x70000 + KEY_USAGE[i]

    def _keymap(self, which):
        return self.keymap if which == "mouse" else self.kbd_keymap

    def native_code(self, i):
        return self._orig_codes[self._scan(i)]

    def _remap(self, i, code):
        which, sc = self._scan(i)
        self._keymap(which).set(sc, code)

    def detach(self):
        super().detach()
        self._stop_spy()
        if self.kbd_keymap:
            self.kbd_keymap.close()
            self.kbd_keymap = None

    def restore_keymap(self):
        for (which, sc), code in self._orig_codes.items():
            km = self._keymap(which)
            if km:
                km.set(sc, code)

    def _kernel_can(self, name, key):
        # The mouse node is mouse-only (libinput drops keys there); the keyboard node is
        # a keyboard (buttons there would be ignored).
        on_mouse = self.buttons.index(name) in MOUSE_CODE
        return key.upper().startswith("BTN_") == on_mouse

    # ---- button spy (raw HID reports) --------------------------------------------

    def _start_spy(self):
        if self._spy_fds:
            return
        loop = asyncio.get_running_loop()
        for path, handler in ((self.mouse_hidraw, self._mouse_report), (self.node.path, self._kbd_report)):
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            loop.add_reader(fd, self._read_reports, fd, handler)
            self._spy_fds.append(fd)
        self._mouse_mask, self._keys = 0, set()

    def _stop_spy(self):
        loop = asyncio.get_running_loop()
        for fd in self._spy_fds:
            loop.remove_reader(fd)
            os.close(fd)
        self._spy_fds = []

    def _read_reports(self, fd, handler):
        while True:
            try:
                r = os.read(fd, 64)
            except BlockingIOError:
                return
            except OSError:                 # unplugged: the daemon forgets us on the udev event
                self._stop_spy()
                self.connected = False
                self.on_change(self)
                return
            if not r:
                return
            handler(r)

    def _mouse_report(self, r):
        # Interface 0, no report id: 16-bit button bitmask, then X, Y, wheel, pan.
        mask = r[0] | r[1] << 8
        changed, self._mouse_mask = mask ^ self._mouse_mask, mask
        if changed:
            for code, i in SLOT_OF_CODE.items():
                if changed >> (code - 1) & 1:
                    self._button(self.buttons[i], bool(mask >> (code - 1) & 1))

    def _kbd_report(self, r):
        # Interface 1, report 1: modifier bits, then up to five key usages.
        if r[0] != 1:
            return
        keys = {u for u in r[2:] if u in SLOT_OF_USAGE}
        for usage in keys ^ self._keys:
            self._button(self.buttons[SLOT_OF_USAGE[usage]], usage in keys)
        self._keys = keys


def _key_name(code):
    name = E.KEY.get(code) or E.BTN.get(code)
    if isinstance(name, (list, tuple)):
        name = name[0]
    if not name:
        raise KeyError(code)
    return name
