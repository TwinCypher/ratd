# ratd

**Gaming-mouse configuration for Linux that actually works:** button remapping, DPI stages,
and per-game profiles that switch automatically when you alt-tab into a game. It's a
replacement for Logitech G Hub (Windows-only) and for Solaar/Piper, which on Linux were
limited and hit-or-miss for this mouse.

Built for KDE Plasma 6 on Wayland. Profiles live on your PC, so you can have as many as you
like. ratd never rewrites the mouse's onboard profiles.

> **Hardware supported today:** Logitech **G502 X** (Lightspeed wireless, and wired over USB).
> Other Logitech HID++ 2.0 gaming mice will probably work, with generic button names.
> Other brands need a new backend; see [Adding your own mouse](#adding-your-own-mouse).

## What you get

- **Per-game profiles.** Rules like `overwatch*` → "Overwatch" switch profiles when that
  window is focused, and back to your default when it isn't. Works on Wayland, including
  games running under Proton (tested via Heroic).
- **Any button → anything.** A key, a key combo (Ctrl+Z), another mouse button, DPI
  up/down/cycle, a sniper button (hold for low DPI), horizontal scroll, or a shell command.
- **DPI stages per profile**, set live on the sensor.
- **Panel widget** showing the active profile, current DPI and battery, with a popup to force
  a profile.
- **Settings window** with a drawing of the mouse and callout lines to each button's binding
  (G Hub style). Click a button to change it; press the key you want to bind.
- **Low-battery notifications**, sleep/wake handling, and wired and wireless both work.
- **`ratctl`** for the terminal: `status`, `profile NAME|auto`, `dpi N`, `reload`, `watch`.

## How it works

Logitech gaming mice normally run "onboard profiles": button behaviour stored in the mouse.
G Hub on Windows instead puts the mouse in **host mode** and does the mapping itself. ratd
does the same on Linux:

1. **Host mode.** The mouse is switched to host mode (HID++ feature `0x8100`), which stops its
   onboard bindings. Every physical button then reaches Linux as its own distinct button.
2. **Kernel remap.** Single-key and mouse-button bindings are written into the kernel's
   scancode table for the mouse (`EVIOCSKEYCODE`). There is **zero added latency**, and mouse
   movement never passes through ratd.
3. **Everything else.** Combos, DPI, sniper, scroll and commands are blanked in that table and
   handled from the mouse's **Mouse Button Spy** reports (`0x8110`). Keys are sent through a
   virtual input device (uinput).
4. **Profile switching.** A small KWin script reports the focused window to the service over
   D-Bus, and the service matches it against your rules.

**Stopping the service hands the mouse back to its onboard profile.** A clean stop restores
the kernel table and switches the mouse back to onboard mode, so with ratd stopped the mouse
behaves as it would without ratd. If ratd crashes instead, the mouse stays in host mode with
the last kernel remap: single-key bindings keep working, while combos, DPI and scroll don't.
systemd restarts the service about 2 seconds later and it takes the mouse over again.

## Requirements

- Linux with KDE Plasma 6 (the widget and focus tracking are Plasma-specific; the service
  itself only needs a session D-Bus)
- Python 3.11+, `python-evdev`, `python-pyudev`, `python-dbus-next`, `pyside6`
  (Arch: `pacman -S python-evdev python-pyudev python-dbus-next pyside6`)
- **Quit Solaar and stop `ratbagd`/Piper.** They talk to the same mouse and will fight ratd.

## Install

```sh
git clone https://github.com/TwinCypher/ratd.git ~/Projects/ratd
cd ~/Projects/ratd

# 1. Device access for your login session (hidraw for Logitech, plus uinput)
sudo install -m644 udev/70-ratd.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger

# 2. The service (the unit expects the checkout at ~/Projects/ratd; edit PYTHONPATH if not)
mkdir -p ~/.config/systemd/user
ln -s "$PWD/systemd/ratd.service" ~/.config/systemd/user/
systemctl --user enable --now ratd

# 3. Commands and app-menu entry
ln -s "$PWD/bin/ratctl" "$PWD/bin/ratd-gui" ~/.local/bin/
cp share/ratd.desktop ~/.local/share/applications/

# 4. Per-game switching (KWin script) and the panel widget
kpackagetool6 --type KWin/Script -i kwin/ratd-focus
kwriteconfig6 --file kwinrc --group Plugins --key ratd-focusEnabled true
qdbus6 org.kde.KWin /KWin reconfigure
kpackagetool6 --type Plasma/Applet -i plasmoid/org.ratd.status
```

Then add the **"Mouse (ratd)"** widget to a panel (right-click panel → Add Widgets), and open
**Mouse Settings** from the app launcher.

`ratctl status` should show your mouse. If it says "asleep", move the mouse; wireless mice
sleep after a while, and ratd takes them over again on the first movement.

## Configuration

Everything is in `~/.config/ratd/config.json`, which the settings window edits for you. A
profile looks like this:

```json
"Overwatch": {
  "dpi": [1100, 1600, 2000], "dpi_index": 1,
  "buttons": {
    "sniper":     {"type": "key", "key": "KEY_V"},
    "tilt_left":  {"type": "key", "key": "KEY_KP3"},
    "top":        {"type": "dpi", "action": "cycle"},
    "forward":    {"type": "keys", "keys": ["KEY_LEFTCTRL", "KEY_Z"]}
  }
}
```

Action types:

| Type | Fields |
|---|---|
| `default` | none |
| `none` | none |
| `key` | `key`: any `KEY_*` or `BTN_*` name |
| `keys` | `keys`: a list of names |
| `dpi` | `action`: `cycle` / `up` / `down` / `shift` (sniper) |
| `scroll` | `direction`: `left` / `right` |
| `command` | `command` |

Rules are global, `{"match": "overwatch*", "profile": "Overwatch"}`, and match the window
class with `*` wildcards. **Mouse Settings → Games / apps → Add from recent windows** fills
these in for you. After editing the file by hand, run `ratctl reload`.

Left click can't be remapped, so a bad config can never leave you without a working mouse.

## Adding your own mouse

The service is split so that a mouse is one backend file. Two cases:

**Another Logitech HID++ 2.0 mouse** (most G-series from the last several years):

1. Run ratd and check `ratctl status`. If your mouse shows up, the generic backend already
   drives it, with buttons named `button1…buttonN`.
2. Find which spy bit is which button. Run `RATD_LOG=DEBUG python3 -m ratd` with the service
   stopped, then press each button and read the log.
3. Add your model to `MODELS` (and `DEFAULTS`/`LABELS`) in `ratd/backends/logitech.py`.
4. Optionally, add a drawing to `DRAWINGS` in `ratd/gui.py` (`G502XDrawing` is the template:
   shapes, anchor points, and which side each callout goes on).

**Requirements on the mouse:** features `0x8100` (onboard profiles, for host mode), `0x8110`
(Mouse Button Spy) and `0x2202` (extended DPI). A HID++ feature dump tells you whether you
have them.

**A different brand:** write a backend with the same shape as `LogitechMouse`:
`start()`, `stop()`, `check()`, `apply_profile()`, `set_dpi()` and `state()`, and register its
discovery in `daemon.py`. The protocol usually comes from an existing open-source driver
(OpenRGB, libratbag), or from USB captures of the vendor's Windows software in a VM
(Wireshark + USBPcap). The design of in-kernel keymap plus a handler for everything else
carries over to any mouse whose buttons can be made to report individually.

## Layout

| Path | What |
|---|---|
| `ratd/daemon.py` | the service: discovery, profile switching, D-Bus `org.ratd` |
| `ratd/backends/logitech.py` | HID++ backend and per-model button tables |
| `ratd/hidpp.py` | async HID++ 2.0 transport over `/dev/hidraw` |
| `ratd/keymap.py` | kernel scancode remapping and the uinput device |
| `ratd/config.py` | config schema and validation |
| `ratd/gui.py` | settings window |
| `ratd/ctl.py` | `ratctl` |
| `kwin/ratd-focus/` | KWin script: focused-window reporting |
| `plasmoid/org.ratd.status/` | Plasma panel widget |
| `systemd/`, `udev/`, `bin/`, `share/` | service unit, access rule, launchers, desktop entry |

## Caveats

- Tested on one G502 X, on Arch with Plasma 6.8 and Wayland. Treat other setups as untested.
- Not affiliated with or endorsed by Logitech. The protocol details come from open-source
  work (Solaar, libratbag) and from probing the mouse.
- Mouse Button Spy (`0x8110`) is undocumented. It works on the G502 X; behaviour on other
  models is unverified.

## License

MIT, see [LICENSE](LICENSE).
