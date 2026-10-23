"""What every mouse backend shares: bindings, the hold layer, DPI stepping, wake watching.

A backend subclass owns the hardware. It sets the model tables and VENDOR, fills in
name/buttons/dpi_choices/keymap in start(), and implements the hooks marked below.
Buttons are remapped in-kernel through the keymap where the kernel can express the
binding; everything else is disabled there and the backend feeds presses to _button().
"""
import asyncio
import logging
import os
from collections import namedtuple

import evdev
from evdev import ecodes as E

from .. import config, keymap
from ..errors import DeviceError

log = logging.getLogger(__name__)

KERNEL_TYPES = {"none", "key"}
WHEEL = ("wheel_up", "wheel_down")  # shift-layer only: one action per wheel notch
SCROLL_REPEAT = (0.30, 0.08)   # first repeat delay, then interval, while a tilt is held

# Daemon callbacks every mouse gets: on_change(mouse) when state changes, coroutine
# on_wake(mouse) when the link comes up, coroutine on_media(action) for media buttons,
# and volume (volume.Volume), which takes volume keys when they're pinned to a device.
Hooks = namedtuple("Hooks", "on_change on_wake on_media volume")


# What talking to a mouse can raise without it being a bug in ratd.
DEVICE_ERRORS = (DeviceError, TimeoutError, ConnectionError, OSError)


def sysattr(node, name):
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


def evdev_for(phys_prefix):
    """The evdev node carrying the mouse buttons, found by its physical path."""
    for path in evdev.list_devices():
        d = evdev.InputDevice(path)
        try:
            if d.phys == phys_prefix or d.phys.startswith(phys_prefix + "/"):
                keys = d.capabilities().get(E.EV_KEY, [])
                if E.BTN_LEFT in keys:
                    return path
        finally:
            d.close()
    return None


class Mouse:
    """One connected mouse. The daemon owns lifecycle; the backend owns the hardware state."""

    VENDOR = "?"           # config key prefix
    MODELS = {}            # model name -> button names, in button index order
    LABELS = {}            # button name -> GUI label
    DEFAULTS = {}          # button name -> what {"type": "default"} means for it

    def __init__(self, evdev_path, hooks):
        self.evdev_path = evdev_path
        self.on_change = hooks.on_change
        self.on_wake = hooks.on_wake
        self.on_media = hooks.on_media
        self.volume = hooks.volume
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
        self._held = {}                     # button name -> codes pressed via uinput
        self._repeat = {}                   # button name -> scroll repeat task
        self._pressed = {}                  # button name -> action it was pressed with
        self._shift = set()                 # shift buttons held down
        self._hold_timers = {}              # shift button -> task turning its layer on after hold_ms
        self._wake_fd = None                # evdev fd watched while the mouse is asleep
        self.kernel_keys = True             # can the evdev node emit keyboard keys?
        self._init_lock = asyncio.Lock()

    @property
    def where(self):
        """Where the mouse is attached, for log messages."""
        return self.evdev_path

    @property
    def model(self):
        """Model table key: 'G502 X LIGHTSPEED' (wireless) and 'G502 X' (wired) are one model."""
        for m in self.MODELS:
            if self.name == m or self.name.startswith(m + " "):
                return m
        return None

    @property
    def key(self):
        return f"{self.VENDOR}:{self.model or self.name}"

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
                "buttons": [{"id": b, "label": self.LABELS.get(b, b),
                             "default": self.DEFAULTS.get(b) if self.model else None,
                             "locked": b in config.LOCKED_BUTTONS} for b in self.buttons],
                "dpi_min": self.dpi_choices[0] if self.dpi_choices else None,
                "dpi_max": self.dpi_choices[-1] if self.dpi_choices else None}

    # ---- backend hooks ---------------------------------------------------------

    async def start(self):
        """Identify the mouse and take it over. Safe to call again after a reconnect."""
        raise NotImplementedError

    async def check(self):
        """Periodic health check. Returns True if the mouse needs (re)starting."""
        raise NotImplementedError

    async def _hand_back(self):
        """Return the hardware to its own onboard behaviour."""

    async def _write_dpi(self, dpi):
        raise NotImplementedError

    async def _apply_hw(self, profile):
        """Hardware settings of a profile other than DPI (polling rate, ...)."""

    def _layer_changed(self, layer):
        """The hold layer turned on or off (self._shift says which)."""

    def native_code(self, i):
        """Keycode button i sends with no remap: HID button usage n -> BTN_LEFT + n - 1."""
        return E.BTN_LEFT + i

    def _remap(self, i, code):
        self.keymap.set(0x90001 + i, code)

    # ---- lifecycle -------------------------------------------------------------

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

    async def _woke(self):
        await asyncio.sleep(0.3)            # let the mouse finish waking up
        await self.on_wake(self)

    def _offline(self):
        if self.connected:
            self.connected = False
            self.on_change(self)
        self.watch_wake()

    async def stop(self):
        """Undo the kernel remap and anything held, then hand the mouse back."""
        self._unwatch_wake()
        for name in list(self._held):
            self.vkbd.release(self._held.pop(name))
        for task in self._repeat.values():
            task.cancel()
        self._repeat.clear()
        self._pressed.clear()
        self._shift.clear()
        self._cancel_hold_timers()
        if self.keymap:
            try:
                self.restore_keymap()
            except OSError:
                pass
            self.keymap.close()
            self.keymap = None
        self.vkbd.close()
        await self._hand_back()

    def restore_keymap(self):
        for i in range(len(self.buttons)):
            self._remap(i, self.native_code(i))

    # ---- profiles --------------------------------------------------------------

    async def apply_profile(self, name, profile):
        self.profile_name = name
        await self._apply(profile, keep_dpi=False)
        self.on_change(self)

    async def _apply(self, profile, keep_dpi):
        self._profile = profile
        self._shift.clear()                 # a profile switch mid-hold must not leave a layer on
        self._cancel_hold_timers()
        if not keep_dpi:
            self._dpi_index = profile.get("dpi_index", 0)
            self._sniper = False
        self._dpi_index = min(self._dpi_index, len(profile["dpi"]) - 1)
        if not self.connected or not self.keymap:
            return
        for i in range(len(self.buttons)):
            self._remap(i, self._kernel_code(i))
        await self._apply_hw(profile)
        await self._set_dpi()

    def _kernel_code(self, i):
        """Keycode button i gets in the normal layer: its key, or reserved if we handle it."""
        act = self._action(self.buttons[i])
        if act["type"] == "default":
            return self.native_code(i)
        if act["type"] == "key" and self._kernel_can(act["key"]) and not self._pinned_volume([act["key"]]):
            return keymap.keycode(act["key"])
        return E.KEY_RESERVED               # none, or handled by us from the backend's events

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
        await self._write_dpi(dpi)
        self.dpi = dpi
        self.on_change(self)

    async def set_dpi(self, dpi):
        await self._set_dpi(dpi)

    async def _set_dpi_logged(self):
        try:
            await self._set_dpi()
        except DEVICE_ERRORS as e:
            log.warning("%s: DPI change failed: %s", self.name, e)

    # ---- button actions --------------------------------------------------------

    def _kernel_can(self, key):
        return self.kernel_keys or key.upper().startswith("BTN_")

    def _action(self, name):
        act = (self._profile or {}).get("buttons", {}).get(name, {"type": "default"})
        if act["type"] == "default" and name in self.DEFAULTS and self.model:
            return self.DEFAULTS[name]
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
            self._hold(name, act, down)
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

    def _hold(self, name, act, down):
        """Hold button: its optional "also" key goes out at once, mirroring the button,
        and the layer turns on after hold_ms (immediately if 0)."""
        also = act.get("also")
        timer = self._hold_timers.pop(name, None)
        if timer:
            timer.cancel()
        if down:
            if also:
                codes = [keymap.keycode(k) for k in also.get("keys") or [also["key"]]]
                self._held[name] = codes
                self.vkbd.press(codes)
            ms = act.get("hold_ms", 0)
            if ms:
                self._hold_timers[name] = asyncio.get_running_loop().create_task(self._hold_after(name, ms))
            else:
                self._set_shift(name, True)
        else:
            if name in self._held:
                self.vkbd.release(self._held.pop(name))
            self._set_shift(name, False)

    def _cancel_hold_timers(self):
        for t in self._hold_timers.values():
            t.cancel()
        self._hold_timers.clear()

    async def _hold_after(self, name, ms):
        await asyncio.sleep(ms / 1000)
        self._hold_timers.pop(name, None)
        self._set_shift(name, True)

    def _set_shift(self, name, down):
        was = bool(self._shift)
        (self._shift.add if down else self._shift.discard)(name)
        if bool(self._shift) == was:
            return
        log.debug("%s: second layer %s", self.name, "on" if self._shift else "off")
        layer = (self._profile or {}).get("shift", {})
        for i, b in enumerate(self.buttons):
            if b in layer and layer[b]["type"] != "default" and self.keymap:
                self._remap(i, E.KEY_RESERVED if self._shift else self._kernel_code(i))
        self._layer_changed(layer)

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
