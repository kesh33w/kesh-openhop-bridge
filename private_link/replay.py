"""Bounded process-local loop suppression, with monotonic expiry.

Restart intentionally forgets reservations. No database, disk I/O, worker thread,
clock high-water mark, or startup quarantine is involved.
"""

import re
import time


class MemoryReplayStore:
    """Use on one event loop. Reservation has no await point and is atomic there."""

    def __init__(self, *, capacity=1024, max_ttl_ms=62000, clock=time.monotonic):
        for value, low, high in ((capacity, 1, 10000), (max_ttl_ms, 100, 62000)):
            if type(value) is not int or not low <= value <= high:
                raise ValueError("Invalid replay-store bound")
        self.capacity = capacity
        self.max_ttl_ms = max_ttl_ms
        self.clock = clock
        self._seen = {}
        self._closed = False
        self.last_rejection = None

    @property
    def healthy(self):
        return not self._closed

    async def reserve(self, packet_id, expiry_ms, now_ms):
        if self._closed:
            self.last_rejection = "closed"
            return False
        if (
            not isinstance(packet_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", packet_id) is None
            or type(now_ms) is not int
            or type(expiry_ms) is not int
            or not 0 <= now_ms < expiry_ms <= (1 << 63) - 1
            or expiry_ms - now_ms > self.max_ttl_ms
        ):
            self.last_rejection = "invalid"
            return False
        now = self.clock()
        self._seen = {key: end for key, end in self._seen.items() if end > now}
        if packet_id in self._seen:
            self.last_rejection = "duplicate"
            return False
        if len(self._seen) >= self.capacity:
            self.last_rejection = "full"
            return False
        self._seen[packet_id] = now + (expiry_ms - now_ms) / 1000
        self.last_rejection = None
        return True

    async def close(self):
        self._closed = True
        self._seen.clear()
