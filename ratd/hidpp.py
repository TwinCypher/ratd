"""Async HID++ 2.0 transport over /dev/hidraw.

One HidppNode per hidraw node. A receiver node carries traffic for several
device indices, so requests and notifications are routed by device index.
"""
import asyncio
import errno
import logging
import os
import struct

from .errors import DeviceError

log = logging.getLogger(__name__)

SHORT, LONG = 0x10, 0x11
SWID = 0x0E                  # our software id; notifications from the device use 0
ROOT, FEATURE_SET = 0x0000, 0x0001
ERR_HIDPP10, ERR_HIDPP20 = 0x8F, 0xFF
CONNECTION_NOTIFY = 0x41     # HID++ 1.0 receiver notification: device link up/down


class HidppError(DeviceError):
    def __init__(self, code, feature, fn):
        super().__init__(f"HID++ error 0x{code:02x} (feature index 0x{feature:02x} fn {fn})")
        self.code = code


class HidppNode:
    """A hidraw node speaking HID++. Requests are serialised; one in flight at a time."""

    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self._lock = asyncio.Lock()
        self._pending = None          # (devidx, feature_index, fnsw, future)
        self._listeners = {}          # devidx -> callback(report bytes)
        self._closed = False
        asyncio.get_running_loop().add_reader(self.fd, self._readable)

    def close(self):
        if self._closed:
            return
        self._closed = True
        asyncio.get_running_loop().remove_reader(self.fd)
        os.close(self.fd)
        if self._pending and not self._pending[3].done():
            self._pending[3].set_exception(ConnectionError("node closed"))

    def listen(self, devidx, callback):
        self._listeners[devidx] = callback

    def _readable(self):
        try:
            r = os.read(self.fd, 64)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EINTR):
                return
            log.warning("%s: read failed (%s), closing", self.path, e)
            self.close()
            for cb in list(self._listeners.values()):
                cb(None)
            return
        if len(r) < 7 or r[0] not in (SHORT, LONG):
            return
        p = self._pending
        if p and r[1] == p[0] and not p[3].done():
            if r[2] == p[1] and r[3] == p[2]:
                p[3].set_result(r[4:])
                return
            if r[2] in (ERR_HIDPP20, ERR_HIDPP10) and r[3] == p[1] and r[4] == p[2]:
                p[3].set_exception(HidppError(r[5], p[1], p[2] >> 4))
                return
        cb = self._listeners.get(r[1])
        if cb:
            cb(r)

    async def call(self, devidx, feature_index, fn, params=b"", timeout=1.0):
        fnsw = (fn << 4) | SWID
        msg = bytes([LONG, devidx, feature_index, fnsw]) + bytes(params).ljust(16, b"\0")[:16]
        async with self._lock:
            if self._closed:
                raise ConnectionError("node closed")
            fut = asyncio.get_running_loop().create_future()
            self._pending = (devidx, feature_index, fnsw, fut)
            try:
                os.write(self.fd, msg)
                return await asyncio.wait_for(fut, timeout)
            finally:
                self._pending = None


class HidppDevice:
    """One HID++ 2.0 device (a device index on a node), with a feature-index cache."""

    def __init__(self, node, devidx):
        self.node, self.idx = node, devidx
        self._features = {}

    async def ping(self, timeout=0.5):
        r = await self.node.call(self.idx, 0, 1, b"\0\0\x5a", timeout)
        return r[0], r[1]             # protocol major, minor

    async def feature_index(self, feature_id):
        if feature_id not in self._features:
            r = await self.node.call(self.idx, 0, 0, struct.pack(">H", feature_id))
            self._features[feature_id] = r[0] or None
        return self._features[feature_id]

    async def call(self, feature_id, fn, params=b"", timeout=1.0):
        fi = await self.feature_index(feature_id)
        if fi is None:
            raise HidppError(0x02, 0, fn)     # 0x02 = invalid argument / unsupported
        return await self.node.call(self.idx, fi, fn, params, timeout)

    async def name(self):
        n = (await self.call(0x0005, 0))[0]
        out = b""
        while len(out) < n:
            out += (await self.call(0x0005, 1, bytes([len(out)])))[: n - len(out)]
        return out.decode(errors="replace")

    def feature_id_for(self, index):
        for fid, fi in self._features.items():
            if fi == index:
                return fid
        return None
