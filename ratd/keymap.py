"""In-kernel button remapping (EVIOCGKEYCODE_V2 / EVIOCSKEYCODE_V2) and the uinput
device used for bindings the kernel can't express (combos, etc.)."""
import fcntl
import os
import struct

import evdev
from evdev import ecodes as E

EVIOCGKEYCODE_V2 = 0x80284504
EVIOCSKEYCODE_V2 = 0x40284504
_ENTRY = "=BBHI32s"   # struct input_keymap_entry: flags, len, index, keycode, scancode[32]


class Keymap:
    """Scancode -> keycode table of one evdev node."""

    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)

    def close(self):
        os.close(self.fd)

    def get(self, scancode):
        buf = bytearray(struct.pack(_ENTRY, 0, 4, 0, 0, struct.pack("=I", scancode)))
        fcntl.ioctl(self.fd, EVIOCGKEYCODE_V2, buf)
        return struct.unpack_from("=I", buf, 4)[0]

    def set(self, scancode, keycode):
        fcntl.ioctl(self.fd, EVIOCSKEYCODE_V2,
                    struct.pack(_ENTRY, 0, 4, 0, keycode, struct.pack("=I", scancode)))


def keycode(name):
    """'KEY_V' / 'BTN_SIDE' / 'v' -> evdev code. Raises KeyError for unknown names."""
    n = name.strip().upper()
    if not n.startswith(("KEY_", "BTN_")):
        n = "KEY_" + n
    return E.ecodes[n]


class VirtualKeyboard:
    """uinput device for key combos and horizontal scroll. Created lazily on first use."""

    def __init__(self):
        self._ui = None

    def _dev(self):
        if self._ui is None:
            keys = [c for c in E.KEY.keys() if c < E.KEY_MAX and c != E.KEY_RESERVED]
            keys += [c for c in E.BTN.keys() if isinstance(c, int)]
            self._ui = evdev.UInput({E.EV_KEY: sorted(set(keys)),
                                     E.EV_REL: [E.REL_X, E.REL_Y, E.REL_HWHEEL, E.REL_HWHEEL_HI_RES]},
                                    name="ratd virtual input")
        return self._ui

    def hscroll(self, step):
        ui = self._dev()
        ui.write(E.EV_REL, E.REL_HWHEEL, step)
        ui.write(E.EV_REL, E.REL_HWHEEL_HI_RES, step * 120)
        ui.syn()

    def press(self, codes):
        ui = self._dev()
        for c in codes:
            ui.write(E.EV_KEY, c, 1)
        ui.syn()

    def release(self, codes):
        ui = self._dev()
        for c in reversed(codes):
            ui.write(E.EV_KEY, c, 0)
        ui.syn()

    def close(self):
        if self._ui:
            self._ui.close()
            self._ui = None
