"""ratd settings window. Edits ~/.config/ratd/config.json through the service (SetConfig),
so every change is validated and applied by the daemon."""
import copy
import html
import json
import os
import subprocess
import sys

from evdev import ecodes as E
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal, Slot
from PySide6.QtDBus import QDBusConnection, QDBusInterface, QDBusMessage
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPalette, QPen, QTransform
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton, QRadioButton, QSizePolicy, QSpinBox,
    QStackedWidget, QTableWidget, QTableWidgetItem, QTabWidget, QToolButton, QVBoxLayout,
    QWidget, QGridLayout,
)

from . import config
from .backends import logitech

BUS, PATH, IFACE = "org.ratd", "/org/ratd", "org.ratd.Manager"

# ---- describing actions ---------------------------------------------------------

KEY_NAMES = {
    "KEY_LEFTCTRL": "Ctrl", "KEY_RIGHTCTRL": "Right Ctrl", "KEY_LEFTSHIFT": "Shift",
    "KEY_RIGHTSHIFT": "Right Shift", "KEY_LEFTALT": "Alt", "KEY_RIGHTALT": "AltGr",
    "KEY_LEFTMETA": "Meta", "KEY_RIGHTMETA": "Right Meta", "KEY_ESC": "Esc",
    "KEY_PAGEUP": "Page Up", "KEY_PAGEDOWN": "Page Down", "KEY_CAPSLOCK": "Caps Lock",
    "BTN_LEFT": "Left click", "BTN_RIGHT": "Right click", "BTN_MIDDLE": "Middle click",
    "BTN_SIDE": "Mouse back", "BTN_EXTRA": "Mouse forward",
    "KEY_PLAYPAUSE": "Play/Pause", "KEY_NEXTSONG": "Next track", "KEY_PREVIOUSSONG": "Previous track",
    "KEY_STOPCD": "Stop", "KEY_VOLUMEUP": "Volume up", "KEY_VOLUMEDOWN": "Volume down",
    "KEY_MUTE": "Mute",
}
# Player actions go to an MPRIS player chosen by ratd; volume is system-wide, so plain keys.
MEDIA_ACTIONS = {"playpause": "Play/Pause", "next": "Next track", "previous": "Previous track",
                 "stop": "Stop"}
MEDIA_KEYS = ["KEY_VOLUMEUP", "KEY_VOLUMEDOWN", "KEY_MUTE"]
# Old configs bound these as keys; the dialog reopens them as the MPRIS action.
KEY_TO_MEDIA = {"KEY_PLAYPAUSE": "playpause", "KEY_NEXTSONG": "next",
                "KEY_PREVIOUSSONG": "previous", "KEY_STOPCD": "stop"}
MOUSE_BUTTONS = ["BTN_LEFT", "BTN_RIGHT", "BTN_MIDDLE", "BTN_SIDE", "BTN_EXTRA"]
MODIFIERS = {"KEY_LEFTCTRL", "KEY_RIGHTCTRL", "KEY_LEFTSHIFT", "KEY_RIGHTSHIFT",
             "KEY_LEFTALT", "KEY_RIGHTALT", "KEY_LEFTMETA", "KEY_RIGHTMETA"}
DPI_ACTIONS = {"cycle": "DPI cycle", "up": "DPI up", "down": "DPI down", "shift": "Sniper (hold)"}


def key_label(name):
    if name in KEY_NAMES:
        return KEY_NAMES[name]
    n = name.removeprefix("KEY_").removeprefix("BTN_")
    if n.startswith("KP") and len(n) > 2:
        return "Keypad " + n[2:].replace("ENTER", "Enter").replace("PLUS", "+").replace("MINUS", "-")
    return n if len(n) <= 3 else n.replace("_", " ").title()


def describe(act, default=None):
    t = act.get("type") if act else "default"
    if t == "default":
        return describe(default) if default else "Default"
    if t == "none":
        return "Disabled"
    if t == "key":
        return key_label(act["key"])
    if t == "keys":
        return " + ".join(key_label(k) for k in act["keys"])
    if t == "dpi":
        return DPI_ACTIONS.get(act.get("action"), "DPI")
    if t == "scroll":
        return f"Scroll {act.get('direction')}"
    if t == "command":
        return "Run: " + act.get("command", "")
    if t == "media":
        return MEDIA_ACTIONS.get(act.get("action"), "Media")
    if t == "shift":
        also = act.get("also")
        if also:
            return describe(also) + " + hold for layer"
        return "Hold for second layer"
    if t == "wheel":                    # GUI-only: what a wheel notch does when not bound
        return f"Scroll {act.get('direction')}"
    return t


def output_devices():
    """[(sink name, description)] from PipeWire/PulseAudio; empty if pactl isn't there."""
    try:
        out = subprocess.run(["pactl", "-f", "json", "list", "sinks"], capture_output=True,
                             text=True, timeout=3).stdout
        return [(x["name"], x.get("description") or x["name"]) for x in json.loads(out)]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def volume_keyboards():
    """Names of connected input devices whose volume keys ratd could take over."""
    import evdev
    from .volume import candidate
    names = set()
    for path in evdev.list_devices():
        try:
            d = evdev.InputDevice(path)
        except OSError:
            continue
        if candidate(d):
            names.add(d.name)
        d.close()
    return sorted(names)


# ---- service connection ----------------------------------------------------------

class Service(QObject):
    stateChanged = Signal(dict)

    def __init__(self):
        super().__init__()
        self.bus = QDBusConnection.sessionBus()
        self.iface = QDBusInterface(BUS, PATH, IFACE, self.bus)
        self.bus.connect(BUS, PATH, IFACE, "StateChanged", self, "1_on_state(QString)")

    @Slot(str)
    def _on_state(self, s):
        self.stateChanged.emit(json.loads(s))

    def _call(self, member, *args):
        reply = self.iface.call(member, *args)
        if reply.type() == QDBusMessage.MessageType.ErrorMessage:
            raise RuntimeError(reply.errorMessage() or "ratd isn't running")
        return reply.arguments()

    def state(self):
        return json.loads(self._call("GetState")[0])

    def config(self):
        return json.loads(self._call("GetConfig")[0])

    def set_config(self, cfg):
        return self._call("SetConfig", json.dumps(cfg))[0]

    def set_profile(self, name):
        self._call("SetProfile", name)


# ---- the G502 X drawing -------------------------------------------------------------

def smooth(points):
    """Closed Catmull-Rom spline through points, as a QPainterPath."""
    p = QPainterPath(QPointF(*points[0]))
    n = len(points)
    for i in range(n):
        p0, p1, p2, p3 = (points[(i + k) % n] for k in (-1, 0, 1, 2))
        c1 = QPointF(p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = QPointF(p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        p.cubicTo(c1, c2, QPointF(*p2))
    return p


def rrect(x, y, w, h, r=6):
    p = QPainterPath()
    p.addRoundedRect(QRectF(x, y, w, h), r, r)
    return p


def triangle(cx, cy, direction, s=7):
    p = QPainterPath()
    d = -1 if direction == "left" else 1
    p.moveTo(cx + d * s, cy)
    p.lineTo(cx - d * s * 0.6, cy - s)
    p.lineTo(cx - d * s * 0.6, cy + s)
    p.closeSubpath()
    return p


class G502XDrawing:
    """Top view of a G502 X in an 800x560 model space: shapes, anchors, callout sides."""
    SIZE = (800, 560)
    BODY = [(300, 72), (400, 52), (500, 74), (532, 190), (546, 330), (522, 462), (430, 522),
            (338, 506), (292, 430), (250, 330), (244, 262), (272, 180)]

    def __init__(self):
        self.body = smooth(self.BODY)
        clip_l, clip_r = QPainterPath(), QPainterPath()
        clip_l.addRect(QRectF(0, 0, 397, 238))
        clip_r.addRect(QRectF(403, 0, 400, 238))
        wheel_slot = rrect(378, 88, 44, 150, 10)
        self.shapes = {
            "left": self.body.intersected(clip_l).subtracted(wheel_slot),
            "right": self.body.intersected(clip_r).subtracted(wheel_slot),
            "middle": rrect(386, 98, 28, 82, 12),
            "tilt_left": triangle(372, 139, "left"),
            "tilt_right": triangle(428, 139, "right"),
            "top": rrect(387, 214, 26, 20, 5),
            "dpi_up": rrect(304, 112, 24, 34, 7),
            "dpi_down": rrect(302, 152, 24, 34, 7),
            "sniper": rrect(256, 222, 30, 26, 8),
            "forward": rrect(248, 268, 34, 22, 8),
            "back": rrect(246, 300, 34, 22, 8),
        }
        self.toggle = rrect(391, 190, 18, 16, 4)    # scroll-mode toggle (mechanical)
        self.anchors = {
            "left": (350, 200), "right": (462, 186), "middle": (400, 112),
            "tilt_left": (370, 139), "tilt_right": (430, 139), "top": (400, 224),
            "dpi_up": (316, 129), "dpi_down": (314, 169), "sniper": (271, 235),
            "forward": (265, 279), "back": (263, 311),
            "wheel_up": (406, 104), "wheel_down": (406, 172),
        }
        self.sides = {"left": ["dpi_up", "tilt_left", "dpi_down", "left", "sniper", "forward", "back"],
                      "right": ["wheel_up", "middle", "wheel_down", "tilt_right", "right", "top"]}


DRAWINGS = {"G502 X": G502XDrawing}
BOX_W, BOX_H = 196, 46


class MouseDiagram(QWidget):
    clicked = Signal(str)

    def __init__(self, drawing):
        super().__init__()
        self.d = drawing
        self.setMouseTracking(True)
        self.setMinimumSize(520, 380)
        self.buttons, self.actions, self.defaults = {}, {}, {}
        self.default_prefix = "Default"
        self.hover = None
        self.boxes = self._layout_boxes()
        bounds = self.d.body.boundingRect()
        for r in self.boxes.values():
            bounds = bounds.united(r)
        self.bounds = bounds.adjusted(-8, -8, 8, 8)      # fit what's drawn, not the whole model space

    def _layout_boxes(self, only=None):
        w, h = self.d.SIZE
        boxes = {}
        for side, names in self.d.sides.items():
            names = [n for n in names if only is None or n in only]
            gap = (h - 40) / max(len(names), 1)
            for i, n in enumerate(names):
                x = 232 - BOX_W if side == "left" else 560      # just clear of the body
                boxes[n] = QRectF(x, 20 + gap * i + (gap - BOX_H) / 2, BOX_W, BOX_H)
        return boxes

    def set_profile(self, buttons, actions, defaults, default_prefix="Default"):
        self.buttons, self.actions, self.defaults = buttons, actions, defaults
        self.default_prefix = default_prefix
        self.boxes = self._layout_boxes(buttons)
        self.update()

    def _transform(self):
        b = self.bounds
        s = min(self.width() / b.width(), self.height() / b.height())
        t = QTransform()
        t.translate((self.width() - b.width() * s) / 2, (self.height() - b.height() * s) / 2)
        t.scale(s, s)
        t.translate(-b.left(), -b.top())
        return t

    def _hit(self, pos):
        p = self._transform().inverted()[0].map(QPointF(pos))
        for n, r in self.boxes.items():
            if r.contains(p):
                return n
        for n, shape in self.d.shapes.items():
            if shape.contains(p):
                return n
        return None

    def mouseMoveEvent(self, e):
        h = self._hit(e.position().toPoint())
        if h != self.hover:
            self.hover = h
            self.setCursor(Qt.PointingHandCursor if h else Qt.ArrowCursor)
            self.update()

    def leaveEvent(self, e):
        self.hover = None
        self.update()

    def mouseReleaseEvent(self, e):
        h = self._hit(e.position().toPoint())
        if h and e.button() == Qt.LeftButton:
            self.clicked.emit(h)

    def paintEvent(self, e):
        pal = self.palette()
        fg, bg = pal.color(QPalette.WindowText), pal.color(QPalette.Window)
        accent = pal.color(QPalette.Highlight)
        mid = QColor(fg)
        mid.setAlphaF(0.35)
        shell = QColor(fg)
        shell.setAlphaF(0.08)

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setTransform(self._transform())

        p.setPen(QPen(mid, 2))
        p.setBrush(shell)
        p.drawPath(self.d.body)
        p.setPen(QPen(mid, 1.5))
        p.drawLine(QPointF(400, 54), QPointF(400, 88))          # split between the main buttons
        p.drawPath(self.d.toggle)

        for n, shape in self.d.shapes.items():
            hot = n == self.hover
            changed = self.actions.get(n, {"type": "default"}).get("type") != "default"
            fill = QColor(accent if hot else fg)
            fill.setAlphaF(0.55 if hot else (0.28 if changed else 0.14))
            p.setPen(QPen(accent if hot else mid, 1.5))
            p.setBrush(fill)
            p.drawPath(shape)

        title = QFont(self.font())
        title.setPointSizeF(self.font().pointSizeF() * 0.85)
        bold = QFont(self.font())
        bold.setBold(True)
        for n, r in self.boxes.items():
            if n not in self.buttons:
                continue
            hot = n == self.hover
            act = self.actions.get(n, {"type": "default"})
            changed = act.get("type") != "default"
            ax, ay = self.d.anchors[n]
            edge = QPointF(r.right(), r.center().y()) if r.left() < 400 else QPointF(r.left(), r.center().y())
            line = QColor(accent if hot else fg)
            line.setAlphaF(0.9 if hot else 0.35)
            p.setPen(QPen(line, 1.5 if hot else 1))
            p.drawLine(edge, QPointF(ax, ay))
            p.setBrush(line)
            p.drawEllipse(QPointF(ax, ay), 3, 3)

            box_bg = QColor(bg)
            p.setPen(QPen(accent if hot or changed else mid, 2 if hot else 1))
            p.setBrush(box_bg)
            p.drawRoundedRect(r, 8, 8)
            if changed:
                p.setPen(Qt.NoPen)
                p.setBrush(accent)
                p.drawRoundedRect(QRectF(r.left() + 4, r.top() + 8, 3, r.height() - 16), 1.5, 1.5)
            p.setPen(mid if not hot else fg)
            p.setFont(title)
            p.drawText(r.adjusted(14, 5, -8, -r.height() / 2), Qt.AlignLeft | Qt.AlignVCenter,
                       self.buttons[n])
            p.setPen(fg)
            p.setFont(bold)
            text = describe(act, self.defaults.get(n))
            if act.get("type") == "default":
                text = f"{self.default_prefix} · " + text
            fm = p.fontMetrics()
            p.drawText(r.adjusted(14, r.height() / 2 - 4, -8, -4), Qt.AlignLeft | Qt.AlignVCenter,
                       fm.elidedText(text, Qt.ElideRight, int(r.width() - 22)))
        p.end()


class ButtonGrid(QWidget):
    """Fallback for mice without a drawing: one button per mouse button."""
    clicked = Signal(str)

    def __init__(self):
        super().__init__()
        self.grid = QGridLayout(self)
        self.grid.setAlignment(Qt.AlignTop)

    def set_profile(self, buttons, actions, defaults, default_prefix="Default"):
        while self.grid.count():
            self.grid.takeAt(0).widget().deleteLater()
        for i, (bid, label) in enumerate(buttons.items()):
            act = actions.get(bid, {"type": "default"})
            b = QPushButton(f"{label}\n{describe(act, defaults.get(bid))}")
            b.setMinimumHeight(52)
            b.clicked.connect(lambda _=False, bid=bid: self.clicked.emit(bid))
            self.grid.addWidget(b, i // 2, i % 2)


# ---- editing one binding --------------------------------------------------------------

def block_global_shortcuts(on):
    """Pause Plasma's global shortcuts (as KDE's own shortcut editor does), so a key like
    F12 reaches the capture box instead of starting Spectacle."""
    global _shortcuts_blocked
    if on == _shortcuts_blocked:
        return
    _shortcuts_blocked = on
    QDBusInterface("org.kde.kglobalaccel", "/kglobalaccel", "org.kde.KGlobalAccel",
                   QDBusConnection.sessionBus()).call("blockGlobalShortcuts", on)


_shortcuts_blocked = False


class KeyCapture(QLineEdit):
    """Press a key or a combo; stores evdev names (KEY_LEFTCTRL, KEY_Z)."""
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.setPlaceholderText("Click here, then press a key or combo")
        self.keys = []
        self._held = []

    def set_keys(self, keys):
        self.keys = list(keys)
        self.setText(" + ".join(key_label(k) for k in self.keys))

    def keyPressEvent(self, e):
        name = self._name(e)
        if not name:
            return
        if not self._held:
            self._held = []
        if name not in self._held:
            self._held.append(name)
        mods = [k for k in self._held if k in MODIFIERS]
        rest = [k for k in self._held if k not in MODIFIERS]
        self.set_keys(mods + rest)
        if rest:                                   # a real key completes the combo
            self._held = []
            self.changed.emit()

    def keyReleaseEvent(self, e):
        name = self._name(e)
        if self._held and all(k in MODIFIERS for k in self._held) and name in self._held:
            self._held = []                       # modifier on its own is a valid binding
            self.changed.emit()

    def _name(self, e):
        if e.isAutoRepeat():
            return None
        code = e.nativeScanCode() - 8             # XKB keycode -> evdev code
        name = E.KEY.get(code)
        if isinstance(name, list):
            name = name[0]
        return name

    def focusInEvent(self, e):
        self._held = []
        block_global_shortcuts(True)
        super().focusInEvent(e)

    def focusOutEvent(self, e):
        block_global_shortcuts(False)
        super().focusOutEvent(e)

    def hideEvent(self, e):                       # dialog closed while the box had focus
        block_global_shortcuts(False)
        super().hideEvent(e)


class BindingDialog(QDialog):
    TYPES = [("default", "Default"), ("key", "Keyboard key or combo"), ("mouse", "Mouse button"),
             ("media", "Media control"), ("dpi", "DPI"), ("scroll", "Horizontal scroll"), ("command", "Run a command"),
             ("shift", "Hold for second layer"), ("none", "Disabled")]

    def __init__(self, parent, label, act, default, media_ignore=(), layer=False):
        super().__init__(parent)
        self.setWindowTitle(f"{label} — second layer" if layer else f"{label} button")
        self.default = default
        lay = QVBoxLayout(self)
        form = QFormLayout()
        lay.addLayout(form)

        self.kind = QComboBox()
        for t, text in self.TYPES:
            if layer and t == "shift":
                continue
            if t == "default" and layer:
                text = "Same as normal"
            self.kind.addItem(text + (f" ({describe(default)})" if t == "default" and default else ""), t)
        form.addRow("Action", self.kind)

        self.pages = QStackedWidget()
        form.addRow("", self.pages)
        self.page = {}

        self.capture = KeyCapture()
        self.mouse = QComboBox()
        for b in MOUSE_BUTTONS:
            self.mouse.addItem(key_label(b), b)
        self.media = QComboBox()
        for k, v in MEDIA_ACTIONS.items():
            self.media.addItem(v, f"media:{k}")
        for k in MEDIA_KEYS:
            self.media.addItem(key_label(k), f"key:{k}")
        self.ignore = QLineEdit(", ".join(media_ignore))
        self.ignore.setPlaceholderText("e.g. firefox, chromium")
        self.ignore.setToolTip("Players whose name contains one of these are never controlled.\n"
                               "firefox also covers LibreWolf; chromium covers Helium, Chrome, Brave.\n"
                               "Applies to every media button, not just this one.")
        media_page = QWidget()
        mf = QFormLayout(media_page)
        mf.setContentsMargins(0, 0, 0, 0)
        mf.addRow(self.media)
        mf.addRow("Ignore apps", self.ignore)
        self.media.currentIndexChanged.connect(
            lambda: self.ignore.setEnabled(self.media.currentData().startswith("media:")))
        self.dpi = QComboBox()
        for k, v in DPI_ACTIONS.items():
            self.dpi.addItem(v, k)
        self.scroll = QComboBox()
        self.scroll.addItem("Left", "left")
        self.scroll.addItem("Right", "right")
        self.command = QLineEdit()
        self.command.setPlaceholderText("e.g. spectacle -r")
        blank = QLabel("")
        shift_page = QWidget()
        sf = QFormLayout(shift_page)
        sf.setContentsMargins(0, 0, 0, 0)
        sf.addRow(QLabel("While held, buttons use their second-layer bindings\n"
                         "(Buttons tab → While holding …)."))
        self.also = KeyCapture()
        self.also.setPlaceholderText("Optional: a key sent at once, e.g. V")
        clear = QPushButton(QIcon.fromTheme("edit-clear"), "Clear")
        clear.setToolTip("No key")
        clear.clicked.connect(lambda: self.also.set_keys([]))
        row = QHBoxLayout()
        row.addWidget(self.also, 1)
        row.addWidget(clear)
        sf.addRow("Also press", row)
        self.hold_ms = QSpinBox()
        self.hold_ms.setRange(0, 2000)
        self.hold_ms.setSingleStep(25)
        self.hold_ms.setSuffix(" ms")
        self.hold_ms.setSpecialValueText("Immediately")
        self.hold_ms.setToolTip("How long the button must be held before the layer turns on.\n"
                                "A quick tap shorter than this only sends the 'Also press' key.")
        sf.addRow("Layer after", self.hold_ms)
        # A key that fires on every press needs a hold delay, or every tap turns the layer on too.
        self.also.changed.connect(lambda: self.also.keys and not self.hold_ms.value()
                                  and self.hold_ms.setValue(200))
        for t, w in (("default", QLabel("Uses the normal binding while the hold button is held." if layer
                                        else "Does what this button normally does.")),
                     ("shift", shift_page),
                     ("key", self.capture), ("mouse", self.mouse), ("media", media_page), ("dpi", self.dpi),
                     ("scroll", self.scroll), ("command", self.command), ("none", blank)):
            self.page[t] = self.pages.addWidget(w)
        self.kind.currentIndexChanged.connect(self._kind_changed)

        t = act.get("type", "default")
        if t == "key" and act["key"].startswith("BTN_"):
            self._select("mouse")
            self.mouse.setCurrentIndex(max(0, self.mouse.findData(act["key"])))
        elif t == "media" or (t == "key" and act["key"] in MEDIA_KEYS + list(KEY_TO_MEDIA)):
            data = f"media:{act['action']}" if t == "media" else (
                f"media:{KEY_TO_MEDIA[act['key']]}" if act["key"] in KEY_TO_MEDIA else f"key:{act['key']}")
            self._select("media")
            self.media.setCurrentIndex(max(0, self.media.findData(data)))
        elif t in ("key", "keys"):
            self._select("key")
            self.capture.set_keys([act["key"]] if t == "key" else act["keys"])
        else:
            self._select(t)
            if t == "dpi":
                self.dpi.setCurrentIndex(max(0, self.dpi.findData(act.get("action"))))
            elif t == "scroll":
                self.scroll.setCurrentIndex(max(0, self.scroll.findData(act.get("direction"))))
            elif t == "command":
                self.command.setText(act.get("command", ""))
            elif t == "shift":
                also = act.get("also")
                self.also.set_keys((also.get("keys") or [also["key"]]) if also else [])
                self.hold_ms.setValue(act.get("hold_ms", 0))

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _select(self, t):
        self.kind.setCurrentIndex(max(0, self.kind.findData(t)))
        self._kind_changed()

    def _kind_changed(self):
        t = self.kind.currentData()
        self.pages.setCurrentIndex(self.page[t])
        if t == "key":
            self.capture.setFocus()

    def _accept(self):
        if self.kind.currentData() == "key" and not self.capture.keys:
            QMessageBox.warning(self, "No key", "Click the box and press the key or combo to bind.")
            return
        if self.kind.currentData() == "command" and not self.command.text().strip():
            QMessageBox.warning(self, "No command", "Type the command to run.")
            return
        self.accept()

    def action(self):
        t = self.kind.currentData()
        if t == "key":
            keys = self.capture.keys
            return {"type": "key", "key": keys[0]} if len(keys) == 1 else {"type": "keys", "keys": keys}
        if t == "mouse":
            return {"type": "key", "key": self.mouse.currentData()}
        if t == "media":
            kind, v = self.media.currentData().split(":", 1)
            return {"type": "media", "action": v} if kind == "media" else {"type": "key", "key": v}
        if t == "dpi":
            return {"type": "dpi", "action": self.dpi.currentData()}
        if t == "scroll":
            return {"type": "scroll", "direction": self.scroll.currentData()}
        if t == "command":
            return {"type": "command", "command": self.command.text().strip()}
        if t == "shift":
            act = {"type": "shift"}
            keys = self.also.keys
            if keys:
                act["also"] = {"type": "key", "key": keys[0]} if len(keys) == 1 else {"type": "keys", "keys": keys}
            if self.hold_ms.value():
                act["hold_ms"] = self.hold_ms.value()
            return act
        return {"type": t}

    def media_ignore(self):
        return [s.strip() for s in self.ignore.text().split(",") if s.strip()]


# ---- DPI and rules tabs ------------------------------------------------------------------

class DpiEditor(QWidget):
    changed = Signal()

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        intro = QLabel("DPI stages. DPI up/down and DPI cycle step through these; "
                       "the selected one is where the profile starts.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.rows = QVBoxLayout()
        lay.addLayout(self.rows)
        add = QPushButton(QIcon.fromTheme("list-add"), "Add stage")
        add.clicked.connect(self._add)
        lay.addWidget(add, 0, Qt.AlignLeft)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)
        self.sniper = QSpinBox()
        self.sniper.setRange(100, 44000)
        self.sniper.setSingleStep(50)
        self.sniper.setSuffix(" DPI")
        self.sniper.valueChanged.connect(self._sniper_changed)
        form.addRow("Sniper DPI (while held)", self.sniper)
        self.rate = QComboBox()
        self.rate.currentIndexChanged.connect(self._rate_changed)
        self.rate.setToolTip("How often the mouse reports movement. Lower rates save some battery.")
        form.addRow("Polling rate", self.rate)
        lay.addLayout(form)
        lay.addStretch()
        self.group = QButtonGroup(self)
        self.profile = None
        self.limits = (100, 44000)

    def set_profile(self, profile, limits, rates=()):
        self.profile, self.limits = profile, limits
        self._rebuild()
        self.rate.blockSignals(True)
        self.rate.clear()
        rates = list(rates) or [125, 250, 500, 1000]
        self.rate.addItem(f"Fastest ({rates[-1]} Hz)", None)
        for hz in reversed(rates):
            self.rate.addItem(f"{hz} Hz", hz)
        self.rate.setCurrentIndex(max(0, self.rate.findData(profile.get("report_rate"))))
        self.rate.blockSignals(False)
        self.sniper.blockSignals(True)
        self.sniper.setRange(*limits)
        self.sniper.setValue(profile.get("sniper_dpi", 400))
        self.sniper.blockSignals(False)

    def _rebuild(self):
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.layout():
                while item.layout().count():
                    w = item.layout().takeAt(0).widget()
                    if w:                      # stretch spacers have no widget
                        w.hide()               # deleteLater waits for the event loop
                        w.deleteLater()
        for b in self.group.buttons():
            self.group.removeButton(b)
        p = self.profile
        for i, dpi in enumerate(p["dpi"]):
            row = QHBoxLayout()
            radio = QRadioButton(f"Stage {i + 1}")
            radio.setChecked(i == p.get("dpi_index", 0))
            radio.toggled.connect(lambda on, i=i: on and self._set_index(i))
            self.group.addButton(radio)
            spin = QSpinBox()
            spin.setRange(*self.limits)
            spin.setSingleStep(50)
            spin.setSuffix(" DPI")
            spin.setValue(dpi)
            spin.valueChanged.connect(lambda v, i=i: self._set_dpi(i, v))
            rm = QToolButton()
            rm.setIcon(QIcon.fromTheme("list-remove"))
            rm.setEnabled(len(p["dpi"]) > 1)
            rm.clicked.connect(lambda _=False, i=i: self._remove(i))
            row.addWidget(radio)
            row.addWidget(spin)
            row.addWidget(rm)
            row.addStretch()
            self.rows.addLayout(row)

    def _set_index(self, i):
        self.profile["dpi_index"] = i
        self.changed.emit()

    def _set_dpi(self, i, v):
        self.profile["dpi"][i] = v
        self.changed.emit()

    def _add(self):
        self.profile["dpi"].append(min(self.profile["dpi"][-1] * 2, self.limits[1]))
        self._rebuild()
        self.changed.emit()

    def _remove(self, i):
        del self.profile["dpi"][i]
        self.profile["dpi_index"] = min(self.profile.get("dpi_index", 0), len(self.profile["dpi"]) - 1)
        self._rebuild()
        self.changed.emit()

    def _rate_changed(self, _i):
        if self.profile is None:
            return
        hz = self.rate.currentData()
        if hz:
            self.profile["report_rate"] = hz
        else:
            self.profile.pop("report_rate", None)
        self.changed.emit()

    def _sniper_changed(self, v):
        if self.profile is not None:
            self.profile["sniper_dpi"] = v
            self.changed.emit()


class RulesEditor(QWidget):
    changed = Signal()

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        intro = QLabel("When a window matching a rule is focused, its profile is used. "
                       "The first matching rule wins; anything else uses the default profile.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Window (class, * wildcards)", "Profile"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.itemChanged.connect(self._edited)
        lay.addWidget(self.table)
        row = QHBoxLayout()
        self.add = QPushButton(QIcon.fromTheme("list-add"), "Add from recent windows")
        self.menu = QMenu(self)
        self.add.setMenu(self.menu)
        self.menu.aboutToShow.connect(self._fill_menu)
        blank = QPushButton(QIcon.fromTheme("list-add"), "Add blank rule")
        blank.clicked.connect(lambda: self._append("", None))
        rm = QPushButton(QIcon.fromTheme("list-remove"), "Remove")
        rm.clicked.connect(self._remove)
        for w in (self.add, blank, rm):
            row.addWidget(w)
        row.addStretch()
        lay.addLayout(row)
        self.cfg, self.profiles, self.recent = None, [], []

    def set_data(self, cfg, profiles, recent):
        self.cfg, self.profiles, self.recent = cfg, profiles, recent
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for r in cfg["rules"]:
            self._add_row(r["match"], r["profile"])
        self.table.blockSignals(False)

    def set_recent(self, recent):
        self.recent = recent

    def _add_row(self, match, profile):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(match))
        combo = QComboBox()
        combo.addItems(self.profiles)
        if profile in self.profiles:
            combo.setCurrentText(profile)
        combo.currentTextChanged.connect(self._edited)
        self.table.setCellWidget(row, 1, combo)

    def _append(self, match, profile):
        self._add_row(match, profile or (self.profiles[0] if self.profiles else ""))
        self._edited()
        if not match:
            self.table.editItem(self.table.item(self.table.rowCount() - 1, 0))

    def _fill_menu(self):
        self.menu.clear()
        if not self.recent:
            self.menu.addAction("No windows seen yet").setEnabled(False)
        for r in self.recent:
            caption = r["caption"] if len(r["caption"]) < 50 else r["caption"][:47] + "..."
            a = self.menu.addAction(f"{r['class']}   —   {caption}")
            a.triggered.connect(lambda _=False, c=r["class"]: self._append(c, None))

    def _remove(self):
        for row in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(row)
        self._edited()

    def _edited(self, *_):
        rules = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            match = item.text().strip() if item else ""
            if match:
                rules.append({"match": match, "profile": self.table.cellWidget(row, 1).currentText()})
        self.cfg["rules"] = rules
        self.changed.emit()


# ---- main window ---------------------------------------------------------------------------

class Window(QMainWindow):
    def __init__(self, svc):
        super().__init__()
        self.svc = svc
        self.setWindowTitle("Mouse settings")
        self.setWindowIcon(QIcon.fromTheme("input-mouse"))
        self.resize(1080, 700)
        self.cfg = self.saved = None
        self.state = {}
        self.dkey = None

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        top = QHBoxLayout()
        self.device = QComboBox()
        self.device.currentIndexChanged.connect(self._device_changed)
        top.addWidget(QLabel("Device"))
        top.addWidget(self.device)
        top.addStretch()
        self.status = QLabel()
        top.addWidget(self.status)
        outer.addLayout(top)

        body = QHBoxLayout()
        outer.addLayout(body, 1)
        side = QVBoxLayout()
        side.addWidget(QLabel("<b>Profiles</b>"))
        self.plist = QListWidget()
        self.plist.setMaximumWidth(230)
        self.plist.currentRowChanged.connect(self._profile_selected)
        side.addWidget(self.plist, 1)
        grid = QGridLayout()
        for i, (text, icon, fn) in enumerate((
                ("New", "document-new", self._new), ("Duplicate", "edit-copy", self._duplicate),
                ("Rename", "edit-rename", self._rename), ("Delete", "edit-delete", self._delete),
                ("Import…", "document-import", self._import), ("Export…", "document-export", self._export))):
            b = QPushButton(QIcon.fromTheme(icon), text)
            b.clicked.connect(fn)
            grid.addWidget(b, i // 2, i % 2)
        side.addLayout(grid)
        self.make_default = QPushButton(QIcon.fromTheme("starred-symbolic"), "Use as default")
        self.make_default.clicked.connect(self._set_default)
        side.addWidget(self.make_default)
        self.activate = QPushButton(QIcon.fromTheme("media-playback-start"), "Use now (until Automatic)")
        self.activate.clicked.connect(self._force)
        side.addWidget(self.activate)
        self.auto = QPushButton(QIcon.fromTheme("view-refresh"), "Automatic switching")
        self.auto.clicked.connect(lambda: self._try(lambda: self.svc.set_profile("")))
        side.addWidget(self.auto)
        body.addLayout(side)

        self.tabs = QTabWidget()
        body.addWidget(self.tabs, 1)
        self.buttons_page = QStackedWidget()
        self.grid = ButtonGrid()
        self.grid.clicked.connect(self._edit_button)
        self.buttons_page.addWidget(self.grid)
        self.diagram = None
        self.layer = False                      # showing the second (hold) layer?
        buttons_tab = QWidget()
        bl = QVBoxLayout(buttons_tab)
        lr = QHBoxLayout()
        lr.addWidget(QLabel("Hold button"))
        self.hold = QComboBox()
        self.hold.setToolTip("While this button is held, the other buttons and the wheel switch\n"
                             "to a second set of bindings.")
        self.hold.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.hold.currentIndexChanged.connect(self._hold_changed)
        lr.addWidget(self.hold)
        lr.addSpacing(12)
        self.layer_group = QButtonGroup(self)
        for i, text in enumerate(("Normal", "While holding")):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setChecked(i == 0)
            self.layer_group.addButton(b, i)
            lr.addWidget(b)
        self.layer_group.idClicked.connect(self._layer_changed)
        self.layer_hint = QLabel("")
        self.layer_hint.setWordWrap(True)
        lr.addWidget(self.layer_hint, 1)
        bl.addLayout(lr)
        bl.addWidget(self.buttons_page, 1)
        self.tabs.addTab(buttons_tab, QIcon.fromTheme("input-mouse"), "Buttons")
        self.dpi = DpiEditor()
        self.dpi.changed.connect(self._dirty)
        self.tabs.addTab(self.dpi, QIcon.fromTheme("speedometer"), "DPI && polling")
        self.rules = RulesEditor()
        self.rules.changed.connect(self._dirty)
        self.tabs.addTab(self.rules, QIcon.fromTheme("applications-games"), "Games / apps")

        bottom = QHBoxLayout()
        self.notify_battery = QCheckBox("Low battery alerts")
        self.notify_battery.setToolTip("Desktop notification when the battery drops to 15% and 5%")
        self.notify_battery.toggled.connect(self._notify_toggled)
        bottom.addWidget(self.notify_battery)
        bottom.addSpacing(12)
        bottom.addWidget(QLabel("Volume keys control"))
        self.volume_device = QComboBox()
        self.volume_device.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.volume_device.setToolTip("Which output the volume keys change: the mouse's volume bindings\n"
                                      "and keyboard volume keys/knobs. Default output = Plasma's behaviour,\n"
                                      "which follows whatever device was used last.")
        self.volume_device.currentIndexChanged.connect(self._volume_device_changed)
        bottom.addWidget(self.volume_device)
        self.volume_kb = QPushButton("Keyboards…")
        self.volume_kb.setToolTip("Which keyboards' volume keys or knobs also change this device")
        self.volume_kb.clicked.connect(self._pick_volume_keyboards)
        bottom.addWidget(self.volume_kb)
        bottom.addSpacing(12)
        self.hint = QLabel("")
        self.hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)    # long paths mustn't widen the window
        bottom.addWidget(self.hint, 1)
        self.revert = QPushButton(QIcon.fromTheme("edit-undo"), "Revert")
        self.revert.clicked.connect(self.load)
        self.apply = QPushButton(QIcon.fromTheme("dialog-ok-apply"), "Apply")
        self.apply.setDefault(True)
        self.apply.clicked.connect(self._apply)
        bottom.addWidget(self.revert)
        bottom.addWidget(self.apply)
        outer.addLayout(bottom)

        svc.stateChanged.connect(self._state)
        self.load()

    # -- data --
    def _try(self, fn):
        try:
            return fn()
        except RuntimeError as e:
            QMessageBox.critical(self, "ratd", str(e))

    def load(self):
        try:
            self.cfg = self.svc.config()
            self.state = self.svc.state()
        except RuntimeError as e:
            QMessageBox.critical(self, "ratd", f"Can't reach the ratd service:\n{e}")
            self.cfg = {"version": 1, "rules": [], "devices": {}}
        self.saved = copy.deepcopy(self.cfg)
        self.notify_battery.blockSignals(True)
        self.notify_battery.setChecked(self.cfg.get("notify_low_battery", True))
        self.notify_battery.blockSignals(False)
        self._fill_volume_devices()
        keep = self.dkey
        self.device.blockSignals(True)
        self.device.clear()
        for k in self.cfg["devices"]:
            self.device.addItem(k.split(":", 1)[-1], k)
        self.device.blockSignals(False)
        i = self.device.findData(keep) if keep else 0
        self.device.setCurrentIndex(max(i, 0))
        self._device_changed()
        self._dirty()

    def _section(self):
        return self.cfg["devices"].get(self.dkey) if self.dkey else None

    def _live(self):
        for d in self.state.get("devices", []):
            if d["key"] == self.dkey:
                return d
        return None

    def _button_info(self):
        """id -> label, id -> default action, locked ids: from the live device, else the model tables."""
        live = self._live()
        if live:
            return ({b["id"]: b["label"] for b in live["buttons"]},
                    {b["id"]: b["default"] for b in live["buttons"]},
                    {b["id"] for b in live["buttons"] if b.get("locked")})
        model = self.dkey.split(":", 1)[-1] if self.dkey else ""
        names = logitech.MODELS.get(model)
        if names:
            return ({n: logitech.LABELS.get(n, n) for n in names},
                    {n: logitech.DEFAULTS.get(n) for n in names}, {"left"})
        sec = self._section()
        first = next(iter(sec["profiles"].values())) if sec else {}
        names = list(first.get("buttons", {}))
        return {n: n for n in names}, {}, {"left"}

    def _device_changed(self):
        self.dkey = self.device.currentData()
        model = self.dkey.split(":", 1)[-1] if self.dkey else ""
        if self.diagram:
            self.buttons_page.removeWidget(self.diagram)
            self.diagram.deleteLater()
            self.diagram = None
        if model in DRAWINGS:
            self.diagram = MouseDiagram(DRAWINGS[model]())
            self.diagram.clicked.connect(self._edit_button)
            self.buttons_page.addWidget(self.diagram)
            self.buttons_page.setCurrentWidget(self.diagram)
        else:
            self.buttons_page.setCurrentWidget(self.grid)
        self._fill_profiles()

    def _fill_profiles(self, select=None):
        sec = self._section()
        current = select or (self.plist.currentItem().data(Qt.UserRole) if self.plist.currentItem() else None)
        self.plist.blockSignals(True)
        self.plist.clear()
        if sec:
            live = self._live()
            for name in sec["profiles"]:
                tags = []
                if name == sec["default_profile"]:
                    tags.append("default")
                if live and live.get("profile") == name:
                    tags.append("active")
                it = QListWidgetItem(name + (f"   ({', '.join(tags)})" if tags else ""))
                it.setData(Qt.UserRole, name)
                if live and live.get("profile") == name:
                    it.setIcon(QIcon.fromTheme("media-playback-start"))
                self.plist.addItem(it)
        self.plist.blockSignals(False)
        names = list(sec["profiles"]) if sec else []
        row = names.index(current) if current in names else 0
        if names:
            self.plist.setCurrentRow(row)
        self._profile_selected()

    def _pname(self):
        it = self.plist.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _profile_selected(self, *_):
        sec, name = self._section(), self._pname()
        if not sec or not name:
            return
        prof = sec["profiles"][name]
        prof.setdefault("buttons", {})
        self._show_buttons()
        live = self._live()
        limits = (live["dpi_min"], live["dpi_max"]) if live and live.get("dpi_min") else (100, 44000)
        self.dpi.set_profile(prof, limits, live.get("report_rates", []) if live else [])
        self.rules.set_data(self.cfg, list(sec["profiles"]), self.state.get("recent", []))
        self.make_default.setEnabled(name != sec["default_profile"])

    def _layer_changed(self, i):
        self.layer = i == 1
        self._show_buttons()

    def _layer_view(self, prof):
        """labels, actions, defaults for the shift layer: wheel notches added, and each
        button's 'default' is what it does normally."""
        labels, defaults, _locked = self._button_info()
        labels = dict(labels, wheel_up="Wheel up", wheel_down="Wheel down")
        normal = {b: (prof["buttons"].get(b) if prof["buttons"].get(b, {"type": "default"})["type"] != "default"
                      else defaults.get(b)) for b in labels}
        normal["wheel_up"] = {"type": "wheel", "direction": "up"}
        normal["wheel_down"] = {"type": "wheel", "direction": "down"}
        return labels, prof.get("shift", {}), normal

    def _show_buttons(self):
        sec, name = self._section(), self._pname()
        if not sec or not name:
            return
        prof = sec["profiles"][name]
        target = self.diagram or self.grid
        labels, defaults, locked = self._button_info()
        held = [b for b, a in prof["buttons"].items() if a["type"] == "shift"]
        self.hold.blockSignals(True)
        self.hold.clear()
        self.hold.addItem("None", None)
        for b, label in labels.items():
            if b not in locked:
                self.hold.addItem(label, b)
        self.hold.setCurrentIndex(max(0, self.hold.findData(held[0])) if held else 0)
        self.hold.blockSignals(False)
        second = self.layer_group.button(1)
        second.setText(f"While holding {labels.get(held[0], held[0])}" if held else "While holding …")
        second.setEnabled(bool(held))
        if self.layer and not held:
            self.layer = False
            self.layer_group.button(0).setChecked(True)
        if self.layer:
            target.set_profile(*self._layer_view(prof), default_prefix="Normal")
            self.layer_hint.setText("Buttons left on Normal keep their usual binding.")
        else:
            target.set_profile(labels, prof["buttons"], defaults)
            self.layer_hint.setText("" if held else "Pick a hold button to give the others a second set of bindings.")

    def _hold_changed(self):
        sec, name = self._section(), self._pname()
        if not sec or not name:
            return
        buttons = sec["profiles"][name]["buttons"]
        for b, a in list(buttons.items()):
            if a["type"] == "shift":
                buttons[b] = {"type": "default"}
        new = self.hold.currentData()
        if new:
            buttons[new] = {"type": "shift"}
            sec["profiles"][name].get("shift", {}).pop(new, None)   # it can't also act in its own layer
        self._show_buttons()
        self._dirty()

    # -- live state --
    def _state(self, st):
        self.state = st
        self.rules.set_recent(st.get("recent", []))
        live = self._live()
        if not live:
            self.status.setText("Mouse asleep or not connected")
        else:
            batt = f" · battery {live['battery']}%" if live.get("battery") is not None else ""
            why = "forced" if st.get("override") else (f"for {st['focus']['class']}" if st["focus"]["class"] else "default")
            rate = f" · {live['report_rate']} Hz" if live.get("report_rate") else ""
            self.status.setText(f"{live['dpi']} DPI{rate}{batt} · using <b>{live['profile']}</b> ({why})")
        if self.cfg == self.saved:                    # don't disturb unsaved edits
            self._fill_profiles()

    # -- edits --
    def _edit_button(self, bid):
        sec, name = self._section(), self._pname()
        if not sec or not name:
            return
        labels, defaults, locked = self._button_info()
        if bid in locked:
            QMessageBox.information(self, labels.get(bid, bid),
                                    "Left click can't be remapped, so the mouse can always be used "
                                    "to fix a bad setup.")
            return
        prof = sec["profiles"][name]
        if self.layer:
            if prof["buttons"].get(bid, {}).get("type") == "shift":
                QMessageBox.information(self, labels.get(bid, bid),
                                        "This is the hold button. Change it with the Hold button "
                                        "menu above.")
                return
            labels, layer, normal = self._layer_view(prof)
            dlg = BindingDialog(self, labels.get(bid, bid), layer.get(bid, {"type": "default"}),
                                normal.get(bid), self.cfg.get("media_ignore", []), layer=True)
        else:
            dlg = BindingDialog(self, labels.get(bid, bid), prof["buttons"].get(bid, {"type": "default"}),
                                defaults.get(bid), self.cfg.get("media_ignore", []))
        if dlg.exec():
            act = dlg.action()
            if not self.layer:
                prof["buttons"][bid] = act
            elif act["type"] == "default":
                prof.get("shift", {}).pop(bid, None)
                if not prof.get("shift"):
                    prof.pop("shift", None)
            else:
                prof.setdefault("shift", {})[bid] = act
            if act["type"] == "media":
                self.cfg["media_ignore"] = dlg.media_ignore()
            self._show_buttons()
            self._dirty()

    def _new(self):
        sec = self._section()
        if not sec:
            return
        name = self._ask_name("New profile", "")
        if name:
            base = sec["profiles"][sec["default_profile"]]
            labels, _d, _l = self._button_info()
            sec["profiles"][name] = {"dpi": list(base["dpi"]), "dpi_index": base.get("dpi_index", 0),
                                     "sniper_dpi": base.get("sniper_dpi", 400),
                                     "buttons": {b: {"type": "default"} for b in labels}}
            self._fill_profiles(name)
            self._dirty()

    def _duplicate(self):
        sec, cur = self._section(), self._pname()
        if not sec or not cur:
            return
        name = self._ask_name("Duplicate profile", cur + " copy")
        if name:
            sec["profiles"][name] = copy.deepcopy(sec["profiles"][cur])
            self._fill_profiles(name)
            self._dirty()

    def _rename(self):
        sec, cur = self._section(), self._pname()
        if not sec or not cur:
            return
        name = self._ask_name("Rename profile", cur)
        if name and name != cur:
            sec["profiles"] = {(name if k == cur else k): v for k, v in sec["profiles"].items()}
            if sec["default_profile"] == cur:
                sec["default_profile"] = name
            for r in self.cfg["rules"]:
                if r["profile"] == cur:
                    r["profile"] = name
            self._fill_profiles(name)
            self._dirty()

    def _delete(self):
        sec, cur = self._section(), self._pname()
        if not sec or not cur:
            return
        if cur == sec["default_profile"]:
            QMessageBox.information(self, "Delete profile", "Choose another default profile first.")
            return
        used = [r["match"] for r in self.cfg["rules"] if r["profile"] == cur]
        msg = f"Delete the profile “{cur}”?"
        if used:
            msg += "\n\nThese rules use it and will be removed too:\n" + "\n".join(used)
        if QMessageBox.question(self, "Delete profile", msg) != QMessageBox.Yes:
            return
        del sec["profiles"][cur]
        self.cfg["rules"] = [r for r in self.cfg["rules"] if r["profile"] != cur]
        self._fill_profiles()
        self._dirty()

    def _set_default(self):
        sec, cur = self._section(), self._pname()
        if sec and cur:
            sec["default_profile"] = cur
            self._fill_profiles(cur)
            self._dirty()

    def _force(self):
        cur = self._pname()
        if not cur:
            return
        if self.cfg != self.saved:
            QMessageBox.information(self, "Unsaved changes", "Apply your changes first.")
            return
        self._try(lambda: self.svc.set_profile(cur))

    def _fill_volume_devices(self):
        cur = self.cfg.get("volume_device")
        self.volume_device.blockSignals(True)
        self.volume_device.clear()
        self.volume_device.addItem("Default output", None)
        for name, desc in output_devices():
            self.volume_device.addItem(desc, name)
        if cur and self.volume_device.findData(cur) < 0:
            self.volume_device.addItem(f"{cur} (not connected)", cur)
        self.volume_device.setCurrentIndex(max(0, self.volume_device.findData(cur)) if cur else 0)
        self.volume_device.blockSignals(False)
        self.volume_kb.setEnabled(bool(cur))

    def _volume_device_changed(self):
        dev = self.volume_device.currentData()
        if dev:
            self.cfg["volume_device"] = dev
        else:
            self.cfg.pop("volume_device", None)
        self.volume_kb.setEnabled(bool(dev))
        self._dirty()

    def _pick_volume_keyboards(self):
        chosen = set(self.cfg.get("volume_keyboards", []))
        names = sorted(set(volume_keyboards()) | chosen)
        dlg = QDialog(self)
        dlg.setWindowTitle("Keyboard volume keys")
        lay = QVBoxLayout(dlg)
        intro = QLabel("Volume keys and knobs on the ticked devices change<br>"
                       f"<b>{html.escape(self.volume_device.currentText())}</b>. "
                       "Others keep Plasma's behaviour.")
        intro.setTextFormat(Qt.RichText)        # auto-detection only looks at the first line
        lay.addWidget(intro)
        boxes = []
        for n in names:
            b = QCheckBox(n if n in volume_keyboards() else f"{n} (not connected)")
            b.setChecked(n in chosen)
            boxes.append((n, b))
            lay.addWidget(b)
        if not names:
            lay.addWidget(QLabel("No keyboard with volume keys found."))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        if dlg.exec():
            picked = [n for n, b in boxes if b.isChecked()]
            if picked:
                self.cfg["volume_keyboards"] = picked
            else:
                self.cfg.pop("volume_keyboards", None)
            self._dirty()

    def _notify_toggled(self, on):
        self.cfg["notify_low_battery"] = on
        self._dirty()

    def _export(self):
        sec, cur = self._section(), self._pname()
        if not sec or not cur:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export profile",
                                              os.path.expanduser(f"~/{cur}.ratd.json"),
                                              "ratd profile (*.ratd.json *.json)")
        if not path:
            return
        data = {"ratd_profile": 1, "model": self.dkey.split(":", 1)[-1], "name": cur,
                "profile": sec["profiles"][cur],
                "rules": [r["match"] for r in self.cfg["rules"] if r["profile"] == cur]}
        try:
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
        except OSError as e:
            QMessageBox.critical(self, "Export profile", str(e))
            return
        self.hint.setText(f"Exported “{cur}” to {path}")

    def _import(self):
        sec = self._section()
        if not sec:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Import profile", os.path.expanduser("~"),
                                              "ratd profile (*.ratd.json *.json)")
        if not path:
            return
        try:
            with open(path) as f:
                data = json.load(f)
            if data.get("ratd_profile") != 1 or not isinstance(data.get("profile"), dict):
                raise ValueError("not a ratd profile export")
        except (OSError, ValueError, AttributeError) as e:
            QMessageBox.critical(self, "Import profile", f"Can't read {path}:\n{e}")
            return
        model = self.dkey.split(":", 1)[-1]
        if data.get("model") != model and QMessageBox.question(
                self, "Import profile",
                f"This profile was made for a {data.get('model')}, not a {model}. Import it anyway? "
                "Buttons this mouse doesn't have are dropped.") != QMessageBox.Yes:
            return
        labels, _d, _l = self._button_info()
        prof = copy.deepcopy(data["profile"])
        prof["buttons"] = {b: prof.get("buttons", {}).get(b, {"type": "default"}) for b in labels}
        name = str(data.get("name") or "Imported")
        if name in sec["profiles"]:
            name = self._ask_name("Import profile", name + " (imported)")
            if not name or name in sec["profiles"]:
                return
        trial = copy.deepcopy(self.cfg)
        trial["devices"][self.dkey]["profiles"][name] = prof
        known = {r["match"] for r in trial["rules"]}
        added = [m for m in data.get("rules", []) if isinstance(m, str) and m not in known]
        trial["rules"] += [{"match": m, "profile": name} for m in added]
        try:
            config.validate(trial)
        except config.ConfigError as e:
            QMessageBox.critical(self, "Import profile", f"That profile isn't valid:\n{e}")
            return
        self.cfg = trial
        self._fill_profiles(name)
        self._dirty()
        if added:
            self.hint.setText(f"Imported “{name}” with rules: {', '.join(added)}")

    def _ask_name(self, title, text):
        name, ok = QInputDialog.getText(self, title, "Profile name:", text=text)
        name = name.strip()
        if not ok or not name:
            return None
        sec = self._section()
        if name in sec["profiles"] and name != text:
            QMessageBox.warning(self, title, f"There's already a profile called “{name}”.")
            return None
        return name

    def _dirty(self):
        dirty = self.cfg != self.saved
        self.apply.setEnabled(dirty)
        self.revert.setEnabled(dirty)
        self.hint.setText("Unsaved changes" if dirty else "")

    def _apply(self):
        err = self._try(lambda: self.svc.set_config(self.cfg))
        if err is None:
            return
        if err:
            QMessageBox.warning(self, "Couldn't apply", err)
            return
        self.saved = copy.deepcopy(self.cfg)
        self._dirty()
        self.hint.setText("Applied")

    def closeEvent(self, e):
        if self.cfg != self.saved:
            r = QMessageBox.question(self, "Unsaved changes", "Apply your changes before closing?",
                                     QMessageBox.Apply | QMessageBox.Discard | QMessageBox.Cancel)
            if r == QMessageBox.Cancel:
                e.ignore()
                return
            if r == QMessageBox.Apply:
                self._apply()
        e.accept()


def run():
    app = QApplication(sys.argv)
    app.setApplicationName("ratd-gui")
    app.setDesktopFileName("ratd")
    app.aboutToQuit.connect(lambda: block_global_shortcuts(False))
    svc = Service()
    w = Window(svc)
    w.show()
    try:
        w._state(svc.state())
    except RuntimeError:
        pass
    sys.exit(app.exec())


if __name__ == "__main__":
    run()
