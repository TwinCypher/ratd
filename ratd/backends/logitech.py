"""Logitech HID++ 2.0 gaming mice in host mode.

Host mode (0x8100 onboard profiles -> mode 2) stops the mouse running its onboard
bindings; every button then arrives at the kernel as its own evdev button, which we
remap in-kernel. Bindings the kernel can't express are disabled in the keymap and
handled from Mouse Button Spy (0x8110) presses instead.

While a "shift" button is held, buttons with a binding in the profile's shift layer are
disabled in the keymap too, and the wheel is diverted to us (0x2121 target bit) if the
layer binds it, so wheel notches arrive as HID++ events instead of scrolling.
"""
import asyncio
import logging
import struct

import evdev
from evdev import ecodes as E

from .. import keymap
from ..hidpp import HidppDevice, HidppNode, HidppError, CONNECTION_NOTIFY, SHORT
from .base import Mouse, WHEEL, sysattr, evdev_for

log = logging.getLogger(__name__)

ONBOARD, SPY, EXT_DPI, BATTERY, REPORT_RATE = 0x8100, 0x8110, 0x2202, 0x1004, 0x8061
HIRES_WHEEL = 0x2121
WHEEL_DIVERT = 0x01                 # 0x2121 mode bit: wheel reports go to HID++, not HID
MODE_ONBOARD, MODE_HOST = 1, 2
LOGITECH = 0x046D
BATTERY_STATUS = {0: "discharging", 1: "charging", 2: "almost full", 3: "full",
                  4: "slow charging", 5: "invalid battery", 6: "thermal error"}
RATE_HZ = [125, 250, 500, 1000, 2000, 4000, 8000]    # 0x8061 rate index -> Hz

# Button index (= spy bit = HID button usage - 1) -> name, per model.
MODELS = {
    "G502 X": ["left", "right", "middle", "back", "sniper", "forward",
               "tilt_left", "tilt_right", "top", "dpi_up", "dpi_down"],
}
LABELS = {
    "left": "Left click", "right": "Right click", "middle": "Middle click",
    "back": "Back", "forward": "Forward", "sniper": "Sniper (thumb)",
    "tilt_left": "Wheel tilt left", "tilt_right": "Wheel tilt right",
    "top": "Top button (behind wheel)", "dpi_up": "DPI up", "dpi_down": "DPI down",
}
# What {"type": "default"} means per button: the mouse's factory behaviour. Buttons
# not listed fall back to their raw kernel code (BTN_LEFT + index).
DEFAULTS = {
    "left": {"type": "key", "key": "BTN_LEFT"}, "right": {"type": "key", "key": "BTN_RIGHT"},
    "middle": {"type": "key", "key": "BTN_MIDDLE"},
    "back": {"type": "key", "key": "BTN_SIDE"}, "forward": {"type": "key", "key": "BTN_EXTRA"},
    "tilt_left": {"type": "scroll", "direction": "left"},
    "tilt_right": {"type": "scroll", "direction": "right"},
    "dpi_up": {"type": "dpi", "action": "up"}, "dpi_down": {"type": "dpi", "action": "down"},
    "sniper": {"type": "dpi", "action": "shift"}, "top": {"type": "dpi", "action": "cycle"},
}


def parse_dpi_list(raw):
    """0x2202 DPI list: plain values, or 0xExxx 'step' markers followed by an upper bound."""
    out, i = [], 0
    while i + 1 < len(raw):
        v = struct.unpack_from(">H", raw, i)[0]
        if v == 0:
            break
        if v >> 13 == 0b111:
            step, last = v & 0x1FFF, struct.unpack_from(">H", raw, i + 2)[0]
            out += range(out[-1] + step, last + 1, step)
            i += 4
        else:
            out.append(v)
            i += 2
    return out


def _is_hidpp_node(node):
    """Logitech hidraw node whose report descriptor starts with vendor page 0xFF00."""
    hid_id = sysattr(node, "HID_ID")
    if not hid_id or int(hid_id.split(":")[1], 16) != LOGITECH:
        return False
    try:
        with open(f"/sys/class/hidraw/{node}/device/report_descriptor", "rb") as f:
            return f.read(3) == b"\x06\x00\xff"
    except OSError:
        return False


def probe(node, hooks):
    """The mice behind a HID++ hidraw node: a receiver's paired slots, or one wired mouse.
    Returns (node handle, {device index: mouse}), or None if the node isn't HID++."""
    if not _is_hidpp_node(node):
        return None
    hn = HidppNode(f"/dev/{node}")
    name = sysattr(node, "HID_NAME") or ""
    phys = sysattr(node, "HID_PHYS") or ""
    receiver = "Receiver" in name
    mice = {}
    for idx in (range(1, 7) if receiver else (0xFF,)):
        # A paired receiver slot has its own evdev node even while the mouse sleeps.
        ev = evdev_for(f"{phys}:{idx}" if receiver else phys.rsplit("/", 1)[0])
        if ev:
            mice[idx] = LogitechMouse(hn, idx, ev, hooks)
    return hn, mice


class LogitechMouse(Mouse):
    """One HID++ mouse, run in host mode with buttons reported through Mouse Button Spy."""

    VENDOR = "logitech"
    MODELS, LABELS, DEFAULTS = MODELS, LABELS, DEFAULTS

    def __init__(self, node, devidx, evdev_path, hooks):
        super().__init__(evdev_path, hooks)
        self.dev = HidppDevice(node, devidx)
        self._spy_mask = 0
        self._wheel = None                  # (mode without divert, counts per notch), if supported
        self._wheel_diverted = False
        self._wheel_acc = 0
        node.listen(devidx, self._report)

    @property
    def where(self):
        return f"{self.dev.node.path} idx {self.dev.idx}"

    # ---- lifecycle -------------------------------------------------------------

    async def start(self):
        async with self._init_lock:
            await self.dev.ping()
            if self.name == "?":
                self.name = await self.dev.name()
                n = (await self.dev.call(SPY, 0))[0]
                self.buttons = MODELS.get(self.model) or [f"button{i + 1}" for i in range(n)]
                raw = b""
                for page in range(16):
                    r = await self.dev.call(EXT_DPI, 2, bytes([0, 0, page]))
                    raw += r[3:]
                    if b"\0\0" in r[3:]:
                        break
                self.dpi_choices = parse_dpi_list(raw)
            if self.keymap is None:
                self.keymap = keymap.Keymap(self.evdev_path)
                # Keyboard keys from a node udev classed as mouse-only get dropped by
                # libinput (the wired G502 X), so those go through uinput instead.
                d = evdev.InputDevice(self.evdev_path)
                try:
                    self.kernel_keys = E.KEY_A in d.capabilities().get(E.EV_KEY, [])
                finally:
                    d.close()
            await self.dev.call(ONBOARD, 1, bytes([MODE_HOST]))
            await self.dev.call(SPY, 1)
            await self._read_wheel()
            self.connected = True
            self._unwatch_wake()
            await self._read_rates()
            await self._read_battery()
            if self._profile:
                await self._apply(self._profile, keep_dpi=True)
            log.info("%s: host mode on, %d buttons, DPI %d-%d", self.name, len(self.buttons),
                     self.dpi_choices[0], self.dpi_choices[-1])
        self.on_change(self)

    async def _hand_back(self):
        await self._divert_wheel(False, force=True)
        # Always try, even if we think it's asleep: a stale flag must not leave it in host mode.
        try:
            await self.dev.call(SPY, 2, timeout=0.5)
            await self.dev.call(ONBOARD, 1, bytes([MODE_ONBOARD]), timeout=0.5)
            mode = (await self.dev.call(ONBOARD, 2, timeout=0.5))[0]
            log.info("%s: handed back to onboard profile (mode %d)", self.name, mode)
        except (HidppError, TimeoutError, ConnectionError, OSError) as e:
            log.warning("%s: couldn't restore onboard mode (%s); it reverts on power cycle",
                        self.name, str(e) or type(e).__name__)

    async def check(self):
        try:
            mode = (await self.dev.call(ONBOARD, 2, timeout=0.5))[0]
        except (HidppError, TimeoutError, ConnectionError) as e:
            if self.connected:
                log.info("%s: not answering (%s), treating as asleep", self.name, str(e) or type(e).__name__)
            self._offline()
            return False
        if mode != MODE_HOST or not self.connected:
            log.info("%s: answering in mode %d, taking it over", self.name, mode)
            return True
        await self._read_battery()
        self.on_change(self)
        return False

    # ---- profiles --------------------------------------------------------------

    async def _apply_hw(self, profile):
        await self._divert_wheel(False)
        await self._set_rate(profile.get("report_rate"))

    async def _write_dpi(self, dpi):
        await self.dev.call(EXT_DPI, 6, struct.pack(">BHHB", 0, dpi, dpi, 0))

    # ---- events ----------------------------------------------------------------

    def _report(self, r):
        if r is None:                       # node went away
            self.connected = False
            self.on_change(self)
            return
        if r[0] == SHORT and r[2] == CONNECTION_NOTIFY:
            up = not (r[4] & 0x40)
            log.info("%s: link %s", self.name, "up" if up else "down")
            if up:
                asyncio.get_running_loop().create_task(self._woke())
            else:
                self._offline()
            return
        if r[3] & 0x0F:                     # a reply to someone's request, not a notification
            return
        fid = self.dev.feature_id_for(r[2])
        if fid == SPY:
            self._spy(struct.unpack_from(">H", r, 4)[0])
        elif fid == BATTERY:
            self._battery_from(r[4:])
            self.on_change(self)
        elif fid == HIRES_WHEEL and r[3] >> 4 == 0 and self._wheel_diverted:
            self._wheel_moved(r[4:])

    def _spy(self, mask):
        changed, self._spy_mask = mask ^ self._spy_mask, mask
        for i, b in enumerate(self.buttons):
            if changed & (1 << i):
                down = bool(mask & (1 << i))
                log.debug("%s: %s %s -> %s", self.name, b, "down" if down else "up", self._action(b))
                self._button(b, down)

    # ---- wheel -----------------------------------------------------------------

    def _layer_changed(self, layer):
        if any(w in layer and layer[w]["type"] != "default" for w in WHEEL) or not self._shift:
            asyncio.get_running_loop().create_task(self._divert_wheel(bool(self._shift)))

    async def _read_wheel(self):
        try:
            per_notch = (await self.dev.call(HIRES_WHEEL, 0))[0]
            mode = (await self.dev.call(HIRES_WHEEL, 1))[0] & ~WHEEL_DIVERT
        except (HidppError, TimeoutError) as e:
            log.debug("%s: no hi-res wheel control: %s", self.name, e)
            self._wheel = None
            return
        self._wheel = (mode, per_notch or 1)
        self._wheel_diverted = True         # unknown after a takeover: make sure it's off
        await self._divert_wheel(False)

    async def _divert_wheel(self, on, force=False):
        if not self._wheel or (on == self._wheel_diverted and not force):
            return
        mode = self._wheel[0] | (WHEEL_DIVERT if on else 0)
        try:
            await self.dev.call(HIRES_WHEEL, 2, bytes([mode]), timeout=0.5)
        except (HidppError, TimeoutError, ConnectionError, OSError) as e:
            log.warning("%s: wheel %s failed: %s", self.name, "divert" if on else "restore",
                        str(e) or type(e).__name__)
            return
        self._wheel_diverted, self._wheel_acc = on, 0

    def _wheel_moved(self, data):
        # data[0] bit 4 = hi-res counts, bits 0-3 = periods; data[1:3] = signed delta, + is up
        delta = struct.unpack_from(">h", data, 1)[0]
        notch = self._wheel[1] if data[0] & 0x10 else 1
        self._wheel_acc += delta
        layer = self._layer()
        while abs(self._wheel_acc) >= notch:
            up = self._wheel_acc > 0
            self._wheel_acc -= notch if up else -notch
            act = layer.get("wheel_up" if up else "wheel_down")
            if act:
                self._tap("wheel_up" if up else "wheel_down", act)

    # ---- polling rate and battery ----------------------------------------------

    async def _read_rates(self):
        try:
            supported = struct.unpack(">H", (await self.dev.call(REPORT_RATE, 1))[:2])[0]
        except (HidppError, TimeoutError) as e:
            log.debug("%s: report rate unavailable: %s", self.name, e)
            return
        self.rate_choices = [hz for i, hz in enumerate(RATE_HZ) if supported >> i & 1]
        # fn2 (getReportRate) on the G502 X keeps answering 1000 Hz whatever was set, so
        # treat the rate as unknown after every takeover and let the profile write it.
        self.report_rate = None

    async def _set_rate(self, hz=None):
        """Profile's polling rate (nearest supported at or below it), or the fastest if unset."""
        if not self.rate_choices:
            return
        hz = max([c for c in self.rate_choices if c <= hz] or self.rate_choices[:1]) if hz else self.rate_choices[-1]
        if hz != self.report_rate:
            try:
                await self.dev.call(REPORT_RATE, 3, bytes([RATE_HZ.index(hz)]))
            except (HidppError, TimeoutError) as e:
                log.warning("%s: setting polling rate failed: %s", self.name, e)
                return
            log.info("%s: polling rate %s -> %d Hz", self.name, self.report_rate, hz)
            self.report_rate = hz

    async def _read_battery(self):
        try:
            self._battery_from(await self.dev.call(BATTERY, 1))
        except (HidppError, TimeoutError) as e:
            log.debug("%s: battery read failed: %s", self.name, e)

    def _battery_from(self, data):
        pct, _level, status = data[0], data[1], data[2]
        self.battery = (pct or None, BATTERY_STATUS.get(status, f"status {status}"))
