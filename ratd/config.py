"""Profiles and focus rules, stored as JSON in ~/.config/ratd/config.json.

Layout:
{
  "version": 1,
  "rules": [{"match": "overwatch.exe", "profile": "Overwatch"}],   # window class glob
  "devices": {
    "<device key>": {
      "default_profile": "Desktop",
      "profiles": {
        "Desktop": {"dpi": [800, 1600, 3200], "dpi_index": 1, "sniper_dpi": 400,
                    "buttons": {"back": {"type": "default"}, ...}}
      }
    }
  }
}

Button actions:
  {"type": "default"}                          the button's normal function
  {"type": "none"}                             disabled
  {"type": "key",  "key": "KEY_V"}             one key or mouse button (BTN_*), remapped in-kernel
  {"type": "keys", "keys": ["KEY_LEFTCTRL", "KEY_Z"]}   combo, sent via uinput
  {"type": "dpi",  "action": "cycle"|"up"|"down"|"shift"}
  {"type": "scroll", "direction": "left"|"right"}    horizontal scroll, repeats while held
  {"type": "command", "command": "notify-send hi"}   run on press
"""
import copy
import fnmatch
import json
import os
import tempfile

from . import keymap

PATH = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "ratd", "config.json")
ACTION_TYPES = {"default", "none", "key", "keys", "dpi", "scroll", "command"}
DPI_ACTIONS = {"cycle", "up", "down", "shift"}
LOCKED_BUTTONS = {"left"}          # never remappable, so the mouse can't be made unusable


class ConfigError(ValueError):
    pass


def empty():
    return {"version": 1, "rules": [], "devices": {}}


def load():
    try:
        with open(PATH) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return empty()
    validate(cfg)
    return cfg


def save(cfg):
    validate(cfg)
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(PATH), prefix=".config.")
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.replace(tmp, PATH)


def validate(cfg):
    if not isinstance(cfg, dict) or cfg.get("version") != 1:
        raise ConfigError("config must be an object with version 1")
    for i, rule in enumerate(cfg.get("rules", [])):
        if not isinstance(rule.get("match"), str) or not isinstance(rule.get("profile"), str):
            raise ConfigError(f"rule {i}: needs string 'match' and 'profile'")
    for dkey, dev in cfg.get("devices", {}).items():
        profiles = dev.get("profiles", {})
        if dev.get("default_profile") not in profiles:
            raise ConfigError(f"{dkey}: default_profile must name one of its profiles")
        for pname, p in profiles.items():
            where = f"{dkey} / {pname}"
            dpi = p.get("dpi")
            if not dpi or not all(isinstance(d, int) and 50 <= d <= 50000 for d in dpi):
                raise ConfigError(f"{where}: 'dpi' must be a non-empty list of DPI values")
            if not 0 <= p.get("dpi_index", 0) < len(dpi):
                raise ConfigError(f"{where}: dpi_index out of range")
            if "sniper_dpi" in p and not (isinstance(p["sniper_dpi"], int) and 50 <= p["sniper_dpi"] <= 50000):
                raise ConfigError(f"{where}: bad sniper_dpi")
            for bname, act in p.get("buttons", {}).items():
                _validate_action(f"{where} / {bname}", bname, act)


def _validate_action(where, bname, act):
    t = act.get("type") if isinstance(act, dict) else None
    if t not in ACTION_TYPES:
        raise ConfigError(f"{where}: unknown action type {t!r}")
    if bname in LOCKED_BUTTONS and t != "default":
        raise ConfigError(f"{where}: this button can't be remapped")
    try:
        if t == "key":
            keymap.keycode(act["key"])
        elif t == "keys":
            if not act.get("keys"):
                raise ConfigError(f"{where}: 'keys' is empty")
            for k in act["keys"]:
                keymap.keycode(k)
    except KeyError as e:
        raise ConfigError(f"{where}: unknown key {e}") from None
    if t == "dpi" and act.get("action") not in DPI_ACTIONS:
        raise ConfigError(f"{where}: dpi action must be one of {sorted(DPI_ACTIONS)}")
    if t == "scroll" and act.get("direction") not in ("left", "right"):
        raise ConfigError(f"{where}: scroll direction must be 'left' or 'right'")
    if t == "command" and not str(act.get("command", "")).strip():
        raise ConfigError(f"{where}: empty command")


def match_rule(cfg, window_class, caption=""):
    """First rule whose glob matches the window class (or caption), case-insensitive."""
    wc, cap = (window_class or "").lower(), (caption or "").lower()
    for rule in cfg.get("rules", []):
        pat = rule["match"].lower()
        if fnmatch.fnmatchcase(wc, pat) or (rule.get("caption") and fnmatch.fnmatchcase(cap, pat)):
            return rule["profile"]
    return None


def device_section(cfg, key, defaults):
    """Return the device's config section, creating it from `defaults` if missing."""
    devs = cfg.setdefault("devices", {})
    if key not in devs:
        devs[key] = copy.deepcopy(defaults)
        return devs[key], True
    return devs[key], False
