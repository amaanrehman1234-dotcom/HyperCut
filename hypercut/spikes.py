"""Chat spike detection: 10 s buckets against a rolling median."""
import statistics
import threading
from collections import deque


class SpikeCounter:
    """Counts chat messages per bucket; flags a bucket far above the rolling baseline."""

    def __init__(self, multiplier=2.5, min_msgs=10, cooldown=30, window=30, warmup=3):
        self.multiplier, self.min_msgs, self.cooldown, self.warmup = multiplier, min_msgs, cooldown, warmup
        self.history = deque(maxlen=window)
        self.count = self.last = 0
        self.baseline = 0.0
        self.last_spike = float("-inf")
        self.lock = threading.Lock()

    def add(self, n=1):
        """Count n more messages in the current bucket (called from the chat thread)."""
        with self.lock:
            self.count += n

    def roll(self, now):
        """Close the current bucket. Returns True if it is a spike."""
        with self.lock:
            n, self.count = self.count, 0
        h = self.history
        self.baseline = statistics.median(h) if h else 0.0
        warm = len(h) >= self.warmup
        h.append(n)
        self.last = n
        big = n >= max(self.min_msgs, self.multiplier * self.baseline)
        return warm and big and self.claim(now, self.cooldown)

    def claim(self, now, cooldown):
        """Atomically take the clip slot: True (and last_spike = now) if cooldown has passed. Spikes and votes share it."""
        with self.lock:
            if now - self.last_spike < cooldown:
                return False
            self.last_spike = now
            return True
