"""Media buttons sent straight to an MPRIS player over D-Bus.

Plain media keys go to whichever player Plasma thinks is active, which is often a
browser tab. Here we pick the player ourselves, skipping the ones the user ignores.
"""
import logging

from dbus_next import Message, MessageType, Variant

log = logging.getLogger(__name__)

PREFIX = "org.mpris.MediaPlayer2."
OBJ = "/org/mpris/MediaPlayer2"
PLAYER = "org.mpris.MediaPlayer2.Player"
METHODS = {"playpause": "PlayPause", "next": "Next", "previous": "Previous", "stop": "Stop"}
RANK = {"Playing": 0, "Paused": 1}          # anything else (Stopped, unknown) ranks last


class Media:
    def __init__(self, bus, ignore):
        self.bus = bus
        self.ignore = ignore                # fn() -> list of app names to skip
        self.last = None                    # bus name we controlled last

    async def _call(self, dest, path, iface, member, signature="", body=()):
        reply = await self.bus.call(Message(destination=dest, path=path, interface=iface,
                                            member=member, signature=signature, body=list(body)))
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{dest} {member}: {reply.error_name}")
        return reply.body

    async def _prop(self, name, iface, prop):
        try:
            v = (await self._call(name, OBJ, "org.freedesktop.DBus.Properties", "Get", "ss",
                                  [iface, prop]))[0]
            return v.value if isinstance(v, Variant) else v
        except RuntimeError:
            return ""

    async def players(self):
        """[(bus name, identity, playback status)] for players not on the ignore list."""
        names = (await self._call("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                  "org.freedesktop.DBus", "ListNames"))[0]
        skip = [s.strip().lower() for s in self.ignore() if s.strip()]
        out = []
        for n in sorted(x for x in names if x.startswith(PREFIX)):
            ident = await self._prop(n, "org.mpris.MediaPlayer2", "Identity")
            entry = await self._prop(n, "org.mpris.MediaPlayer2", "DesktopEntry")
            # "firefox" in org.mpris.MediaPlayer2.firefox.instance_1_655
            app = n[len(PREFIX):].split(".")[0]
            hay = f"{app} {ident} {entry}".lower()
            if any(s in hay for s in skip):
                continue
            out.append((n, ident or app, await self._prop(n, PLAYER, "PlaybackStatus")))
        return out

    async def control(self, action):
        try:
            players = await self.players()
            if not players:
                log.info("media %s: no player to control", action)
                return
            # Playing beats paused; among equals, the one we used last.
            name, ident, _ = min(players, key=lambda p: (RANK.get(p[2], 2), p[0] != self.last))
            await self._call(name, OBJ, PLAYER, METHODS[action])
            self.last = name
            log.debug("media %s -> %s", action, ident)
        except Exception as e:      # a misbehaving player must not take the service down
            log.warning("media %s failed: %s", action, e)
