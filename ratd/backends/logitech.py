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
import os
import struct

import evdev
from evdev import ecodes as E

from .. import config, keymap
from ..hidpp import HidppDevice, HidppError, CONNECTION_NOTIFY, SHORT

log = logging.getLogger(__name__)

ONBOARD, SPY, EXT_DPI, BATTERY, REPORT_RATE = 0x8100, 0x8110, 0x2202, 0x1004, 0x8061
HIRES_WHEEL = 0x2121
WHEEL_DIVERT = 0x01                 # 0x2121 mode bit: wheel reports go to HID++, not HID
MODE_ONBOARD, MODE_HOST = 1, 2
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
KERNEL_TYPES = {"none", "key"}
WHEEL = ("wheel_up", "wheel_down")  # shift-layer only: one action per wheel notch
SCROLL_REPEAT = (0.30, 0.08)   # first repeat delay, then interval, while a tilt is held


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


class LogitechMouse:
    """One connected mouse. The daemon owns lifecycle; we own the hardware state."""

    def __init__(self, node, devidx, evdev_path, on_change, on_wake, on_media, volume=None):
        self.dev = HidppDevice(node, devidx)
        self.evdev_path = evdev_path
        self.on_change = on_change          # callback(self) when state changes
        self.on_wake = on_wake              # coroutine fn(self) when the link comes up
        self.on_media = on_media            # coroutine fn(action) for media buttons
        self.volume = volume                # volume.Volume: takes volume keys when pinned to a device
        self.keymap = None
        self.vkbd = keymap.VirtualKeyboard()
        self.name = "?"
        self.buttons = []
        self.dpi_choices = []
        self.connected = False
        self.battery = None                 # (percent, status string)
        self.dpi = None
        self.report_rate = None             # Hz, None if the mouse can't report it
        self.rate_choices = []              # supported polling rates in Hz, ascending
        self.profile_name = None
        self._profile = None
        self._dpi_index = 0
        self._sniper = False
        self._spy_mask = 0
        self._held = {}                     # button name -> codes pressed via uinput
        self._repeat = {}                   # button name -> scroll repeat task
        self._pressed = {}                  # button name -> action it was pressed with
        self._shift = set()                 # shift buttons held down
        self._wheel = None                  # (mode without divert, counts per notch), if supported
        self._wheel_diverted = False
        self._wheel_acc = 0
        self._wake_fd = None                # evdev fd watched while the mouse is asleep
        self.kernel_keys = True             # can the evdev node emit keyboard keys?
        self._init_lock = asyncio.Lock()
        node.listen(devidx, self._report)

    @property
    def model(self):
        """Model table key: 'G502 X LIGHTSPEED' (wireless) and 'G502 X' (wired) are one model."""
        for m in MODELS:
            if self.name == m or self.name.startswith(m + " "):
                return m
        return None

    @property
    def key(self):
        return f"logitech:{self.model or self.name}"

    def default_section(self):
        btns = {b: {"type": "default"} for b in self.buttons}
        return {"default_profile": "Desktop",
                "profiles": {"Desktop": {"dpi": [800, 1600, 3200], "dpi_index": 1,
                                         "sniper_dpi": 400, "buttons": btns}}}

    def state(self):
        return {"key": self.key, "name": self.name, "connected": self.connected,
                "battery": self.battery[0] if self.battery else None,
                "battery_status": self.battery[1] if self.battery else None,
                "dpi": self.dpi, "report_rate": self.report_rate, "report_rates": self.rate_choices,
                "profile": self.profile_name,
                "model": self.model,
                "buttons": [{"id": b, "label": LABELS.get(b, b),
                             "default": DEFAULTS.get(b) if self.model else None,
                             "locked": b in config.LOCKED_BUTTONS} for b in self.buttons],
                "dpi_min": self.dpi_choices[0] if self.dpi_choices else None,
                "dpi_max": self.dpi_choices[-1] if self.dpi_choices else None}

    # ---- lifecycle -------------------------------------------------------------

    async def start(self):
        """Identify the mouse and take it over. Safe to call again after a reconnect."""
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

    def watch_wake(self):
        """Mouse isn't answering: take it over as soon as it produces any input."""
        if self._wake_fd is not None:
            return
        try:
            self._wake_fd = os.open(self.evdev_path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            return
        asyncio.get_running_loop().add_reader(self._wake_fd, self._wake_input)

    def _unwatch_wake(self):
        if self._wake_fd is not None:
            asyncio.get_running_loop().remove_reader(self._wake_fd)
            os.close(self._wake_fd)
            self._wake_fd = None

    def _wake_input(self):
        self._unwatch_wake()
        log.info("%s: input while offline, taking over", self.name)
        asyncio.get_running_loop().create_task(self._woke())

    def _offline(self):
        if self.connected:
            self.connected = False
            self.on_change(self)
        self.watch_wake()

    async def stop(self):
        """Hand the mouse back to its onboard profile and undo the kernel remap."""
        self._unwatch_wake()
        for name in list(self._held):
            self.vkbd.release(self._held.pop(name))
        for task in self._repeat.values():
            task.cancel()
        self._repeat.clear()
        self._pressed.clear()
        self._shift.clear()
        await self._divert_wheel(False, force=True)
        if self.keymap:
            try:
                self.restore_keymap()
            except OSError:
                pass
            self.keymap.close()
            self.keymap = None
        self.vkbd.close()
        # Always try, even if we think it's asleep: a stale flag must not leave it in host mode.
        try:
            await self.dev.call(SPY, 2, timeout=0.5)
            await self.dev.call(ONBOARD, 1, bytes([MODE_ONBOARD]), timeout=0.5)
            mode = (await self.dev.call(ONBOARD, 2, timeout=0.5))[0]
            log.info("%s: handed back to onboard profile (mode %d)", self.name, mode)
        except (HidppError, TimeoutError, ConnectionError, OSError) as e:
            log.warning("%s: couldn't restore onboard mode (%s); it reverts on power cycle",
                        self.name, str(e) or type(e).__name__)

    def restore_keymap(self):
        """Kernel default for a mouse: HID button usage n -> BTN_LEFT + n - 1."""
        for i in range(len(self.buttons)):
            self.keymap.set(0x90001 + i, E.BTN_LEFT + i)

    async def check(self):
        """Periodic health check. Returns True if the mouse needs (re)starting."""
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

    async def apply_profile(self, name, profile):
        self.profile_name = name
        await self._apply(profile, keep_dpi=False)
        self.on_change(self)

    async def _apply(self, profile, keep_dpi):
        self._profile = profile
        self._shift.clear()                 # a profile switch mid-hold must not leave a layer on
        if not keep_dpi:
            self._dpi_index = profile.get("dpi_index", 0)
            self._sniper = False
        self._dpi_index = min(self._dpi_index, len(profile["dpi"]) - 1)
        if not self.connected or not self.keymap:
            return
        for i, b in enumerate(self.buttons):
            self.keymap.set(0x90001 + i, self._kernel_code(i))
        await self._divert_wheel(False)
        await self._set_rate(profile.get("report_rate"))
        await self._set_dpi()

    def _kernel_code(self, i):
        """Keycode button i gets in the normal layer: its key, or reserved if we handle it."""
        act = self._action(self.buttons[i])
        if act["type"] == "default":
            return E.BTN_LEFT + i
        if act["type"] == "key" and self._kernel_can(act["key"]) and not self._pinned_volume([act["key"]]):
            return keymap.keycode(act["key"])
        return E.KEY_RESERVED               # none, or handled by us from spy

    def _pinned_volume(self, keys):
        """The volume keycode if these keys are one volume key that ratd handles itself."""
        if len(keys) == 1 and self.volume and self.volume.handles(keymap.keycode(keys[0])):
            return keymap.keycode(keys[0])
        return None

    def _current_dpi(self):
        p = self._profile
        if self._sniper and p.get("sniper_dpi"):
            return p["sniper_dpi"]
        return p["dpi"][self._dpi_index]

    async def _set_dpi(self, dpi=None):
        dpi = dpi or self._current_dpi()
        if self.dpi_choices:                # snap to the nearest value the sensor accepts
            dpi = min(self.dpi_choices, key=lambda c: abs(c - dpi))
        await self.dev.call(EXT_DPI, 6, struct.pack(">BHHB", 0, dpi, dpi, 0))
        self.dpi = dpi
        self.on_change(self)

    async def set_dpi(self, dpi):
        await self._set_dpi(dpi)

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

    async def _woke(self):
        await asyncio.sleep(0.3)            # let the mouse finish waking up
        await self.on_wake(self)

    def _spy(self, mask):
        changed, self._spy_mask = mask ^ self._spy_mask, mask
        for i, b in enumerate(self.buttons):
            if changed & (1 << i):
                down = bool(mask & (1 << i))
                log.debug("%s: %s %s -> %s", self.name, b, "down" if down else "up", self._action(b))
                self._button(b, down)

    def _kernel_can(self, key):
        return self.kernel_keys or key.upper().startswith("BTN_")

    def _action(self, name):
        act = (self._profile or {}).get("buttons", {}).get(name, {"type": "default"})
        if act["type"] == "default" and name in DEFAULTS and self.model:
            return DEFAULTS[name]
        return act

    def _layer(self):
        """The shift layer's bindings, if a shift button is held."""
        if not self._shift:
            return {}
        return {b: a for b, a in (self._profile or {}).get("shift", {}).items() if a["type"] != "default"}

    def _button(self, name, down):
        # A release goes to whatever the press did, even if shift changed in between.
        if down:
            shifted = name in self._layer()
            act = self._layer()[name] if shifted else self._action(name)
            self._pressed[name] = (act, shifted)
        else:
            act, shifted = self._pressed.pop(name, (self._action(name), False))
        t = act["type"]
        if t == "shift":
            self._set_shift(name, down)
            return
        # Shifted buttons are disabled in the keymap, so even plain keys go through uinput.
        if t == "key" and (shifted or not self._kernel_can(act["key"])):
            t, act = "keys", {"type": "keys", "keys": [act["key"]]}
        if t in KERNEL_TYPES or t == "default":
            return
        if t == "scroll":
            task = self._repeat.pop(name, None)
            if task:
                task.cancel()
            if down:
                step = -1 if act["direction"] == "left" else 1
                self._repeat[name] = asyncio.get_running_loop().create_task(self._scroll(step))
            return
        if t == "keys" and self._pinned_volume(act["keys"]):
            if down:
                asyncio.get_running_loop().create_task(self.volume.key(self._pinned_volume(act["keys"])))
        elif t == "keys":
            if down:
                codes = [keymap.keycode(k) for k in act["keys"]]
                self._held[name] = codes
                self.vkbd.press(codes)
            elif name in self._held:
                self.vkbd.release(self._held.pop(name))
        elif t == "dpi":
            loop = asyncio.get_running_loop()
            a, n = act["action"], len(self._profile["dpi"])
            if a == "shift":
                self._sniper = down
            elif not down:
                return
            elif a == "cycle":
                self._dpi_index = (self._dpi_index + 1) % n
            elif a == "up":
                self._dpi_index = min(self._dpi_index + 1, n - 1)
            elif a == "down":
                self._dpi_index = max(self._dpi_index - 1, 0)
            loop.create_task(self._set_dpi_logged())
        elif t == "command" and down:
            asyncio.get_running_loop().create_task(_run(act["command"]))
        elif t == "media" and down:
            asyncio.get_running_loop().create_task(self.on_media(act["action"]))

    def _set_shift(self, name, down):
        was = bool(self._shift)
        (self._shift.add if down else self._shift.discard)(name)
        if bool(self._shift) == was:
            return
        layer = (self._profile or {}).get("shift", {})
        for i, b in enumerate(self.buttons):
            if b in layer and layer[b]["type"] != "default" and self.keymap:
                self.keymap.set(0x90001 + i, E.KEY_RESERVED if self._shift else self._kernel_code(i))
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

    def _tap(self, name, act):
        """One wheel notch: press and release the action."""
        if act["type"] == "scroll":
            self.vkbd.hscroll(-1 if act["direction"] == "left" else 1)
        elif act["type"] not in ("none", "shift"):
            self._button(name, True)
            self._button(name, False)

    async def _scroll(self, step):
        self.vkbd.hscroll(step)
        await asyncio.sleep(SCROLL_REPEAT[0])
        while True:
            self.vkbd.hscroll(step)
            await asyncio.sleep(SCROLL_REPEAT[1])

    async def _set_dpi_logged(self):
        try:
            await self._set_dpi()
        except (HidppError, TimeoutError, ConnectionError) as e:
            log.warning("%s: DPI change failed: %s", self.name, e)

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


async def _run(command):
    import shlex
    try:
        proc = await asyncio.create_subprocess_exec(*shlex.split(command),
                                                    stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL,
                                                    start_new_session=True)
        asyncio.get_running_loop().create_task(proc.wait())
    except OSError as e:
        log.warning("command %r failed: %s", command, e)
